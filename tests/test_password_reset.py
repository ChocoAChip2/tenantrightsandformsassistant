"""Tests for the forgot-password / reset-password flow.

forgot_password asks Supabase to email a recovery link and always shows the
same generic confirmation, regardless of whether the email is registered or
whether Supabase itself raises -- this is deliberate (see the docstring on
SupabaseService.send_password_reset_email) so the route can't be used to
discover which emails have accounts. reset_password sets the new password
using the access/refresh tokens the recovery link's page copies into the
POST body -- there's no logged-in session at this point, so those tokens are
the only proof of identity, and reset_password reuses the same
update_account() call settings.html's account form uses.

Everything runs through Flask's test client against a fake Supabase service
-- no real Supabase call is made.
"""

import os
import unittest

import flask

from routes import main_bp

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def __init__(self):
        self.reset_email_calls = []
        self.update_account_calls = []
        self.raise_on_reset_email = False

    def send_password_reset_email(self, email, redirect_to=None):
        self.reset_email_calls.append({"email": email, "redirect_to": redirect_to})
        if self.raise_on_reset_email:
            raise Exception("Supabase is unreachable.")

    def update_account(self, access_token, refresh_token, email=None, password=None):
        self.update_account_calls.append(
            {"access_token": access_token, "refresh_token": refresh_token, "email": email, "password": password}
        )
        if access_token == "expired-token":
            raise Exception("Recovery token has expired.")
        return object()


class FakeAIService:
    def is_ready(self):
        return True


def _build_test_app(supabase_service):
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = supabase_service
    app.config["AI_SERVICE"] = FakeAIService()
    app.register_blueprint(main_bp)
    return app


class ForgotPasswordTests(unittest.TestCase):
    def test_get_renders_the_request_form(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.get("/forgot-password")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Reset your password", response.get_data(as_text=True))

    def test_blank_email_is_rejected_without_calling_supabase(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post("/forgot-password", data={}, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Please enter your email", response.get_data(as_text=True))
        self.assertEqual(service.reset_email_calls, [])

    def test_valid_email_sends_reset_link_and_redirects_to_login_with_generic_message(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/forgot-password", data={"email": "tenant@example.com"}, follow_redirects=False
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        self.assertEqual(len(service.reset_email_calls), 1)
        self.assertEqual(service.reset_email_calls[0]["email"], "tenant@example.com")
        # redirect_to must point back at this app's own reset-password route.
        self.assertTrue(service.reset_email_calls[0]["redirect_to"].endswith("/reset-password"))

        response = client.get(response.headers["Location"])
        self.assertIn(
            "If an account exists for that email", response.get_data(as_text=True)
        )

    def test_supabase_failure_still_shows_the_same_generic_message(self):
        """A raised exception (Supabase down, bad request, etc.) must not
        change what the visitor sees -- otherwise the response itself would
        leak whether the email exists."""
        service = FakeSupabaseService()
        service.raise_on_reset_email = True
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/forgot-password", data={"email": "tenant@example.com"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "If an account exists for that email", response.get_data(as_text=True)
        )


class ResetPasswordTests(unittest.TestCase):
    def test_get_renders_the_reset_form(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.get("/reset-password")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose a new password", response.get_data(as_text=True))

    def test_missing_tokens_redirects_to_forgot_password_with_error(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/reset-password",
            data={"password": "newpassword1", "confirm_password": "newpassword1"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/forgot-password"))
        self.assertEqual(service.update_account_calls, [])

    def test_mismatched_passwords_are_rejected_without_calling_supabase(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/reset-password",
            data={
                "access_token": "tok-a",
                "refresh_token": "tok-r",
                "password": "newpassword1",
                "confirm_password": "different",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("do not match", response.get_data(as_text=True))
        self.assertEqual(service.update_account_calls, [])

    def test_short_password_is_rejected_without_calling_supabase(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/reset-password",
            data={"access_token": "tok-a", "refresh_token": "tok-r", "password": "abc", "confirm_password": "abc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("at least 6 characters", response.get_data(as_text=True))
        self.assertEqual(service.update_account_calls, [])

    def test_valid_submission_resets_password_and_redirects_to_login(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/reset-password",
            data={
                "access_token": "tok-a",
                "refresh_token": "tok-r",
                "password": "newpassword1",
                "confirm_password": "newpassword1",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        self.assertEqual(len(service.update_account_calls), 1)
        call = service.update_account_calls[0]
        self.assertEqual(call["access_token"], "tok-a")
        self.assertEqual(call["refresh_token"], "tok-r")
        self.assertEqual(call["password"], "newpassword1")
        self.assertIsNone(call["email"])

        response = client.get(response.headers["Location"])
        self.assertIn("Your password has been reset", response.get_data(as_text=True))

    def test_expired_token_shows_error_and_redirects_to_forgot_password(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()

        response = client.post(
            "/reset-password",
            data={
                "access_token": "expired-token",
                "refresh_token": "tok-r",
                "password": "newpassword1",
                "confirm_password": "newpassword1",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/forgot-password"))

        response = client.get(response.headers["Location"], follow_redirects=True)
        self.assertIn("Could not reset your password", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
