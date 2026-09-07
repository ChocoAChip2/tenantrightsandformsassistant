"""Regression tests for the intake-clerk -> PDF-download flow in
routes.py:chat_message.

This guards against a real bug that shipped to main: FormService was
called with template_path="templates/RA-81.pdf", but the PDF actually
committed to the repo is templates/ra-81-fillable.pdf. On a case-sensitive
filesystem (which is what Render runs) that mismatch means pypdf's
PdfReader raises FileNotFoundError the moment a user finishes the intake
flow -- the one time this code path matters in production.

No real Gemini or Supabase call is made; FormService.fill_tenant_form and
flask.send_file are both mocked so this never touches pypdf or the disk.
"""

import os
import unittest
from unittest import mock

import flask

from routes import main_bp
from tests.csrf_test_support import disable_csrf

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPECTED_TEMPLATE_PATH = "templates/ra-81-fillable.pdf"


class FakeSupabaseService:
    def build_user_scoped_client(self, access_token):
        return object()

    def ensure_conversation_for_user(self, user_client, conversation_id, user_id):
        return None

    def fetch_messages_for_conversation(self, user_client, conversation_id):
        return [{"role": "user", "content": "My info is ready", "created_at": ""}]

    def insert_message(self, user_client, message):
        return None


class FakeAIService:
    def __init__(self, reply):
        self._reply = reply

    def generate_reply(self, messages):
        return self._reply


def _build_test_app(ai_reply):
    app = flask.Flask(__name__, template_folder=os.path.join(_REPO_ROOT, "templates"))
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = FakeSupabaseService()
    app.config["AI_SERVICE"] = FakeAIService(ai_reply)
    app.register_blueprint(main_bp)
    disable_csrf(app)
    return app


def _logged_in_session(client):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["user_email"] = "tenant@example.com"
        session["access_token"] = "fake-token"


class TemplateFileTests(unittest.TestCase):
    def test_the_referenced_pdf_template_actually_exists(self):
        """The exact file FormService.fill_tenant_form is called with must exist on disk."""

        full_path = os.path.join(_REPO_ROOT, _EXPECTED_TEMPLATE_PATH)
        self.assertTrue(
            os.path.isfile(full_path),
            f"Expected the RA-81 PDF template at {full_path} -- if it moved or was "
            "renamed, update _EXPECTED_TEMPLATE_PATH here *and* the template_path "
            "argument in routes.py:chat_message together.",
        )


class CompletionJsonTriggersPdfDownloadTests(unittest.TestCase):
    def test_completion_json_calls_form_service_with_the_real_template_path(self):
        completion_reply = '{"status": "complete", "name": "A Tenant", "address": "123 Main St", "complaint": "No heat"}'
        app = _build_test_app(completion_reply)
        client = app.test_client()
        _logged_in_session(client)

        with mock.patch("routes.FormService.fill_tenant_form", return_value="/tmp/fake.pdf") as fake_fill, \
             mock.patch("routes.send_file", return_value="pdf-response") as fake_send_file:
            response = client.post(
                "/chat/message",
                json={"conversation_id": "c1", "content": "My info is ready"},
            )

        fake_fill.assert_called_once()
        self.assertEqual(fake_fill.call_args.kwargs["template_path"], _EXPECTED_TEMPLATE_PATH)
        fake_send_file.assert_called_once()
        self.assertEqual(response.get_data(as_text=True), "pdf-response")

    def test_non_json_reply_is_returned_as_a_normal_chat_message(self):
        app = _build_test_app("This is a normal housing-law answer, not JSON.")
        client = app.test_client()
        _logged_in_session(client)

        with mock.patch("routes.FormService.fill_tenant_form") as fake_fill:
            response = client.post(
                "/chat/message",
                json={"conversation_id": "c1", "content": "What are my rights?"},
            )

        fake_fill.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reply"], "This is a normal housing-law answer, not JSON.")


if __name__ == "__main__":
    unittest.main()
