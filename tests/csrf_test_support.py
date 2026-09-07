"""Shared helper for the test apps that render real templates.

Every POST <form> in the app now carries {{ csrf_token() }} (see
app.py/rate_limit.py and test_security_hardening.py for the actual CSRF
protection this supports) -- that Jinja global only exists once
Flask-WTF's CSRFProtect has been initialized against the app, so a test
app built the usual way in this suite (a bare flask.Flask(...) around
main_bp, with no CSRFProtect) would hit a Jinja UndefinedError the moment
it rendered any template with a form.

None of these tests are about CSRF itself, so the fix here is two parts:
register CSRFProtect (which wires up the csrf_token() global test
templates now need), and turn enforcement back off (WTF_CSRF_ENABLED =
False) so a plain client.post({...}) with no real token still reaches the
route the way these tests expect. test_security_hardening.py and
test_proxy_fix.py, which go through the real create_app(), handle CSRF
deliberately instead of using this helper.
"""

from flask_wtf import CSRFProtect


def disable_csrf(app):
    app.config["WTF_CSRF_ENABLED"] = False
    CSRFProtect(app)
    return app
