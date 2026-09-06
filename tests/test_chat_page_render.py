"""Smoke tests for the redesigned templates/chat.html.

This branch is a visual-only change (no route or service logic touched),
so the main risk is a Jinja/template regression rather than a behavioral
one. These render the real /chat route through Flask's test client with
fake services -- no real Supabase or Gemini call is made.
"""

import os
import unittest

import flask

from routes import main_bp

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def __init__(self, conversations=None, archived_conversations=None, messages=None):
        self._conversations = conversations or []
        self._archived_conversations = archived_conversations or []
        self._messages = messages or []

    def build_user_scoped_client(self, access_token):
        return object()  # opaque stand-in; never touched again in these tests

    def list_conversations(self, user_client, archived=False):
        return self._archived_conversations if archived else self._conversations

    def ensure_conversation_for_user(self, user_client, conversation_id, user_id):
        return None

    def fetch_messages_for_conversation(self, user_client, conversation_id):
        return self._messages


class FakeAIService:
    def __init__(self, ready=True):
        self._ready = ready

    def is_ready(self):
        return self._ready


def _build_test_app(supabase_service, ai_service):
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = supabase_service
    app.config["AI_SERVICE"] = ai_service
    app.register_blueprint(main_bp)
    return app


def _logged_in_session(client):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["user_email"] = "tenant@example.com"
        session["access_token"] = "fake-token"


class ChatPageRenderTests(unittest.TestCase):
    def test_active_conversation_renders_message_history(self):
        service = FakeSupabaseService(
            conversations=[{"id": "c1", "title": "Broken heat", "created_at": "", "updated_at": ""}],
            messages=[
                {"role": "user", "content": "My landlord won't fix the heat", "created_at": ""},
                {"role": "assistant", "content": "Here is what NYC law says...", "created_at": ""},
            ],
        )
        app = _build_test_app(service, FakeAIService(ready=True))
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/chat?conversation_id=c1")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Broken heat", body)
        self.assertIn("My landlord won&#39;t fix the heat", body)
        self.assertIn('class="message user"', body)
        self.assertIn('class="message assistant"', body)
        # No GEMINI_API_KEY warning banner when the AI service is ready.
        self.assertNotIn("GEMINI_API_KEY is not configured", body)

    def test_no_active_conversation_shows_dynamic_greeting(self):
        app = _build_test_app(FakeSupabaseService(), FakeAIService(ready=True))
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/chat")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        # Server-rendered fallback greeting (JS overwrites this client-side
        # with a real time-of-day greeting once it runs).
        self.assertIn("Welcome, Tenant", body)
        self.assertIn('id="greeting-text"', body)
        self.assertIn('id="greeting-date"', body)

    def test_archived_conversations_render_in_collapsible_section(self):
        service = FakeSupabaseService(
            conversations=[{"id": "c1", "title": "Active chat", "archived_at": None}],
            archived_conversations=[{"id": "c2", "title": "Old chat", "archived_at": "2026-01-01T00:00:00Z"}],
        )
        app = _build_test_app(service, FakeAIService(ready=True))
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/chat")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Active chat", body)
        self.assertIn("Old chat", body)
        self.assertIn("Archived (1)", body)

    def test_sidebar_footer_user_menu_present(self):
        app = _build_test_app(FakeSupabaseService(), FakeAIService(ready=True))
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/chat")

        body = response.get_data(as_text=True)
        self.assertIn('id="user-menu-trigger"', body)
        self.assertIn("tenant@example.com", body)
        self.assertIn("Settings", body)
        self.assertIn("Log out", body)

    def test_ai_not_ready_shows_warning_banner(self):
        app = _build_test_app(FakeSupabaseService(), FakeAIService(ready=False))
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/chat")

        self.assertIn("GEMINI_API_KEY is not configured", response.get_data(as_text=True))

    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService(), FakeAIService())
        client = app.test_client()

        response = client.get("/chat", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))


if __name__ == "__main__":
    unittest.main()
