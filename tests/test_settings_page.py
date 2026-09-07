"""Tests for the settings page: theme UI render, account updates, and the
chat-history download route.

Everything runs through Flask's test client against fake Supabase/AI
services -- no real Supabase or Gemini call is made.
"""

import os
import unittest

import flask

from routes import main_bp
from tests.csrf_test_support import disable_csrf

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def __init__(self, conversations=None):
        self._conversations = conversations or []
        self.update_account_calls = []

    def build_user_scoped_client(self, access_token):
        return object()  # opaque stand-in; RLS is not exercised in these tests

    def fetch_all_conversations_with_messages(self, user_client):
        return self._conversations

    def get_pending_account_deletion(self, user_client, user_id):
        # The settings route asks about a pending account deletion in order
        # to render the Danger Zone; none of the tests in this file are
        # about that, so there never is one. See tests/test_account_deletion.py.
        return None

    def update_account(self, access_token, refresh_token, email=None, password=None):
        self.update_account_calls.append(
            {"access_token": access_token, "refresh_token": refresh_token, "email": email, "password": password}
        )
        if email == "already-taken@example.com":
            raise Exception("A user with this email address has already been registered")
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
    disable_csrf(app)
    return app


def _logged_in_session(client, access_token="fake-access", refresh_token="fake-refresh"):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["user_email"] = "tenant@example.com"
        session["access_token"] = access_token
        session["refresh_token"] = refresh_token


class SettingsPageRenderTests(unittest.TestCase):
    def test_logged_in_shows_settings_sections(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/settings")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("tenant@example.com", body)
        self.assertIn("Appearance", body)
        self.assertIn("Download my chat history", body)

    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.get("/settings", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))


class UpdateAccountTests(unittest.TestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/settings/account", data={"email": "new@example.com"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_blank_form_is_rejected_without_calling_supabase(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/settings/account", data={}, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter a new email and/or password", response.get_data(as_text=True))
        self.assertEqual(service.update_account_calls, [])

    def test_mismatched_password_confirmation_is_rejected(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account",
            data={"password": "newpassword1", "confirm_password": "different"},
            follow_redirects=True,
        )

        self.assertIn("do not match", response.get_data(as_text=True))
        self.assertEqual(service.update_account_calls, [])

    def test_short_password_is_rejected(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account",
            data={"password": "abc", "confirm_password": "abc"},
            follow_redirects=True,
        )

        self.assertIn("at least 6 characters", response.get_data(as_text=True))
        self.assertEqual(service.update_account_calls, [])

    def test_valid_email_and_password_change_calls_supabase_with_session_tokens(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client, access_token="tok-a", refresh_token="tok-r")

        response = client.post(
            "/settings/account",
            data={"email": "new@example.com", "password": "newpassword1", "confirm_password": "newpassword1"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(service.update_account_calls), 1)
        call = service.update_account_calls[0]
        self.assertEqual(call["access_token"], "tok-a")
        self.assertEqual(call["refresh_token"], "tok-r")
        self.assertEqual(call["email"], "new@example.com")
        self.assertEqual(call["password"], "newpassword1")
        body = response.get_data(as_text=True)
        self.assertIn("confirmation link", body)
        self.assertIn("Password updated", body)

    def test_supabase_error_is_shown_without_crashing(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account",
            data={"email": "already-taken@example.com"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Could not update account", response.get_data(as_text=True))

    def test_missing_session_tokens_forces_relogin(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "user-1"
            session["user_email"] = "tenant@example.com"
            # No access_token/refresh_token stored.

        response = client.post("/settings/account", data={"email": "new@example.com"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))


class DownloadChatHistoryTests(unittest.TestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.get("/settings/download-logs", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_download_contains_conversation_titles_and_messages(self):
        service = FakeSupabaseService(
            conversations=[
                {
                    "id": "c1",
                    "title": "Broken heat",
                    "created_at": "2026-01-01",
                    "messages": [
                        {"role": "user", "content": "My landlord won't fix the heat"},
                        {"role": "assistant", "content": "Here is what NYC law says..."},
                    ],
                }
            ]
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/settings/download-logs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/markdown")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("nyc-tenant-assistant-chat-history.md", response.headers["Content-Disposition"])
        body = response.get_data(as_text=True)
        self.assertIn("Broken heat", body)
        self.assertIn("My landlord won't fix the heat", body)
        self.assertIn("Here is what NYC law says...", body)

    def test_download_with_no_conversations_still_succeeds(self):
        app = _build_test_app(FakeSupabaseService(conversations=[]))
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/settings/download-logs")

        self.assertEqual(response.status_code, 200)
        self.assertIn("No conversations yet", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
