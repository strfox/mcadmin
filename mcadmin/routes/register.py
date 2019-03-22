# mcadmin/routes/register.py

from flask import request, abort, render_template, redirect

from mcadmin.forms.registration_form import RegistrationForm
from mcadmin.io.registration import is_registered, save_password
from mcadmin.main import app


@app.route('/register', methods=['GET', 'POST'])
def register():
    """
    This is the page for registering an administrative password for the MCAdmin instance.

    If an administrative password has already been registered, then a HTTP 401 Unauthorized response will be sent.

    If the registration form is submitted, the entered password will be validated and then saved to disk. The user will
    then be redirected to the index page.
    """
    if is_registered():
        return abort(401)

    form = RegistrationForm()

    if form.validate_on_submit():
        password = request.form['password']
        save_password(password)
        return redirect('/')

    return render_template('registration.html', form=form)
