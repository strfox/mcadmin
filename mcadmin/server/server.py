# mcadmin/server/server.py

import threading
import atexit
import glob
import logging
import os
import signal
import time
import collections
from subprocess import Popen, PIPE

import requests

from mcadmin.server import server_repo

LOGGER = logging.getLogger(__name__)
SERVER_DIR = 'server_files'
MAX_DOWNLOAD_ATTEMPTS = 2
SIGTERM_WAIT_SECONDS = 30
CONSOLE_OUTPUT_MAX_LINES = 100

# Console output buffer that will be sent to the client when they open the console page
console_output = collections.deque(maxlen=CONSOLE_OUTPUT_MAX_LINES)

# Java server process handle
proc = None  # type: Popen
proc_lock = threading.RLock()

# Thread that updates console_output deque with new lines
console_thread = None  # type: threading.Thread

# This condition will be notified every time the server
# status change from ON to OFF or vice-versa.
SERVER_STATUS_CHANGE = threading.Condition()

# This condition will be notified whenever there is a console output
CONSOLE_OUTPUT_COND = threading.Condition()

# Create server files directory if it does not exist.
if not os.path.exists(SERVER_DIR):
    os.mkdir(SERVER_DIR)


class TooManyMatchesError(Exception):
    """
    For when a lookup returns more than one match when just one is expected.
    """
    pass


class ServerAlreadyRunningError(Exception):
    """
    Raised when trying to start the server when it is already running.
    """
    pass


class ServerNotRunningError(Exception):
    """
    Raised when an action that requires a running server is performed without the server running.
    """
    pass


def is_server_running():
    """
    :returns: true if the server is running.

    Implementation notes:
        The server is considered to be running if:
            - `proc` references a process
            - There is no return code from `proc.poll()`

        In case `proc` references a process, yet it has a return code, that means that the server must have crashed.
    """
    with proc_lock:
        if proc is None:
            return False
        else:
            return_code = proc.poll()
            if return_code is None:
                return True
            else:
                LOGGER.warning(
                    'Server may have crashed! Reference to process still exists, but the process ended '
                    'with return code %d.' % return_code)
                return False


def _notify_status_change():
    """
    Notifies all threads waiting on the SERVER_STATUS_CHANGE Condition.
    """
    with SERVER_STATUS_CHANGE:
        SERVER_STATUS_CHANGE.notify_all()


def stop():
    """
    Stops the server.

    It will first try to stop the server gracefully with a SIGTERM, but if the server does not close within
    SIGTERM_WAIT_SECONDS seconds, the server process will be forcefully terminated.

    This method notifies a status change.

    :raises ServerNotRunningError: if the server is not running
    """
    global proc
    global console_thread

    with proc_lock:
        if not is_server_running():
            raise ServerNotRunningError('Server is not running: no process reference.')
        return_code = proc.poll()
        if return_code is not None:
            raise ServerNotRunningError('Server is not running: no return code.')

        LOGGER.info('Waiting at most %s seconds for server to shut down...' % SIGTERM_WAIT_SECONDS)
        proc.send_signal(signal.SIGTERM)
        proc.wait(SIGTERM_WAIT_SECONDS)

        return_code = proc.poll()

        if return_code is None:
            LOGGER.warning('Server SIGTERM timed out; terminating forcefully.')
            proc.terminate()

    LOGGER.info('Server process closed.')

    # Wait for console_thread to finish
    LOGGER.info('Waiting for console thread to finish...')
    console_thread.join()
    console_thread = None
    LOGGER.info('Console thread done.')

    with proc_lock:
        proc = None

    _notify_status_change()


def _on_program_exit():
    """
    Close the server before exiting the Python interpreter.
    """
    if is_server_running():
        LOGGER.info('Python is exiting: terminating server process.')
        stop()


# Register the _on_program_exit function to be ran before the Python interpreter quits.
atexit.register(_on_program_exit)


def _locate_server_file_name():
    """
    Locates the server executable .jar file to be ran by MCAdmin.

    The method will look for any files inside the server directory that are named `minecraft_server-<version>.jar`.

    :raise FileNotFoundError: if no server executables were found.
    :raise TooManyMatchesError: if more than one server executable was found.
    :return: The filename of the server executable.
    """
    matches = glob.glob(os.path.join(SERVER_DIR, 'minecraft_server-*.jar'))
    if len(matches) == 0:
        raise FileNotFoundError(
            'Did not find a server file in directory %s/ (abspath: %s)' % (SERVER_DIR, os.path.abspath(SERVER_DIR)))
    if len(matches) > 1:
        raise TooManyMatchesError('Found more than one server executable in %s: %s' % (SERVER_DIR, str(matches)))
    return os.path.basename(matches[0])


def _download_latest_vanilla_server():
    """
    Downloads the latest vanilla server from the internet and writes the file to SERVER_DIR.
    The filename will be the full name of the version.

    :raises IOError: If download failed after MAX_DOWNLOAD_ATTEMPTS attempts
    """
    version, full_name, link = server_repo.latest_stable_ver()

    for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
        LOGGER.info('Downloading vanilla %s server executable from %s...' % (version, link))
        try:
            response = requests.get(link)
            write_to = os.path.join(os.path.abspath(SERVER_DIR), full_name)

            LOGGER.info('Done. Writing to %s ...' % write_to)

            with open(write_to, 'wb') as f:
                f.write(response.content)
            return full_name
        except IOError as e:
            LOGGER.error('Could not download server executable. Error: [%s] %s' % (
                str(e), '... Retrying' if attempt + 1 < MAX_DOWNLOAD_ATTEMPTS else ''))

    raise IOError('Failed to download server executable after %s attempts.' % MAX_DOWNLOAD_ATTEMPTS)


def _agree_eula():
    """
    Creates an `eula.txt` file inside the server directory.
    Writes the text required to agree to the Mojang EULA to the file.
    """
    eula_path = os.path.join(SERVER_DIR, 'eula.txt')
    with open(eula_path, 'w') as f:
        f.write(
            '#By changing the setting below to TRUE you are indicating your agreement to our EULA '
            '(https://account.mojang.com/documents/minecraft_eula)\n'
            '#Mon Mar 20 21:15:37 PDT 2017\n'
            'eula=true\n')


def start(server_jar_name=None, jvm_params=''):
    """
    Starts the server.

    The latest server file will be downloaded automatically if `server_jar_name` was not specified and a server executable
    does not exist inside the server directory.

    This will also start the console thread.
    This method notified a status change.

    :param server_jar_name: The filename of the server to use.
                            If not specified, it will find the server file automatically.
    :param jvm_params:      Parameters used when starting the JVM. Nothing by default.

    :raise ServerAlreadyRunningError: If the server is already running.
    :raise FileNotFoundError:         If `server_jar_name` param was specified but a file by that name was not found
                                      inside the server directory.
    """
    global proc

    with proc_lock:
        if is_server_running():
            raise ServerAlreadyRunningError('Server is already running')

        # if a server jar name was specified,
        # it will be used instead of the latest version
        if server_jar_name:
            path = os.path.join(SERVER_DIR, server_jar_name)
            if not os.path.exists(path):
                raise FileNotFoundError('File %s not found' % os.path.abspath(path))
        else:
            # server file not specified
            # download latest stable version
            try:
                server_jar_name = _locate_server_file_name()
            except FileNotFoundError:
                LOGGER.warning('No server executable found; will attempt to download latest vanilla server.')
                server_jar_name = _download_latest_vanilla_server()
        assert server_jar_name

        # eula has to be agreed to otherwise server won't start
        _agree_eula()

        command = 'java %s -jar %s nogui' % (jvm_params, server_jar_name)
        proc = Popen(command, stdout=PIPE, stdin=PIPE, stderr=PIPE, cwd=SERVER_DIR)

        _start_console_thread()
        _notify_status_change()


def _start_console_thread():
    """
    Starts the console thread and assigns it to the global `console_thread` variable.

    :raise ValueError: if a thread is already referenced by `console_thread`.
    """
    global console_thread

    if console_thread is not None:
        raise ValueError('Thread already exists')

    console_thread = threading.Thread(target=_console_worker)
    console_thread.start()


def _console_worker():
    """
    Should run in a separate thread.

    Will read the output from the server process constantly until the server is stopped. It will add the output lines
    to the `console_output` deque and notify CONSOLE_OUTPUT_COND that the console was updated.
    """
    while is_server_running():
        # This ugly hack is required because I needed an atomic comparison, so that the code wouldn't try to access the
        # `proc` variable if it had changed by that point.
        # On top of that, a lock for `proc` should not be acquired here because `proc.stdout.readline()` is blocking.

        # Line being set to none indicates that the process is closed.
        line = proc.stdout.readline() \
            if proc is not None and proc.poll() is None \
            else None
        if line is None:
            break

        if line != b'':  # Sometimes it reads this and I don't want it
            encoded = line.decode('utf-8')
            console_output.append(encoded)
            LOGGER.debug(encoded)

            with CONSOLE_OUTPUT_COND:
                CONSOLE_OUTPUT_COND.notify_all()


def input_line(text):
    """
    Sends an input to the server process.

    :param text: Input to send
    :raise ServerNotRunningError: if the server is not running
    """
    with proc_lock:
        _require_server()
        if isinstance(text, str):
            text = text.encode()
        proc.stdin.write(text)


def _require_server():
    """
    :raise ServerNotRunningError: if the server is not running
    """
    if not is_server_running():
        raise ServerNotRunningError('Server needs to be running to do this')


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    start()
    print('sleep')
    time.sleep(30)
    print('end sleep')
    stop()

    import shutil

    shutil.rmtree(SERVER_DIR)
    os.remove(server_repo.FILENAME)

    assert proc is None
