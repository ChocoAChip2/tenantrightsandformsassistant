"""Route-level tests for POST / (signup), focused on the duplicate-account path.

Builds a minimal Flask app around routes.main_bp with a fake SupabaseService
injected via app.config, instead of importing app.py -- that avoids needing
real environment variables and keeps this fully offline.
"""

import os
import unittest

import flask

from routes import main_bp

# routes.py's render_template() calls resolve against the app's template
# folder, which Flask defaults to a "templates" directory next to wherever
# the Flask() app object is constructed -- that's this tests/ directory, not
# the project root, unless told otherwise.
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    """Stands in for supabase_service.SupabaseService in these tests."""

    def __init__(self, sign_up_result=True):
        self._sign_up_result = sign_up_result
        self.sign_up_calls = []

    def sign_up(self, email, password):
        self.sign_up_calls.append((email, password))
        return self._sign_up_result


class FakeAIService:
    def is_ready(self):
        return True


def _build_test_app(sign_up_result):
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = FakeSupabaseService(sign_up_result=sign_up_result)
    app.config["AI_SERVICE"] = FakeAIService()
    app.register_blueprint(main_bp)
    return app


class SignupDuplicateAccountRouteTests(unittest.TestCase):
    def test_new_account_redirects_to_login_with_success_flash(self):
        app = _build_test_app(sign_up_result=True)
        client = app.test_client()

        response = client.post(
            "/", data={"email": "new@example.com", "password": "hunter22"}, follow_redirects=False
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_existing_account_rerenders_signup_with_login_button_instead_of_redirecting(self):
        app = _build_test_app(sign_up_result=False)
        client = app.test_client()

        response = client.post(
            "/", data={"email": "existing@example.com", "password": "hunter22"}, follow_redirects=False
        )

        # No account was (or should have been) created, so this must not
        # behave like a successful signup: stay on the page, 200, not a
        # redirect to login.
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("existing@example.com", body)
        self.assertIn("already exists", body.lower())
        # The actual button/link that lets the visitor go to login.
        self.assertIn('href="/login"', body)

    def test_missing_fields_shows_validation_error_without_calling_sign_up(self):
        app = _build_test_app(sign_up_result=True)
        client = app.test_client()
        fake_service = app.config["SUPABASE_SERVICE"]

        response = client.post("/", data={"email": "", "password": ""}, follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_service.sign_up_calls, [])


if __name__ == "__main__":
    unittest.main()
