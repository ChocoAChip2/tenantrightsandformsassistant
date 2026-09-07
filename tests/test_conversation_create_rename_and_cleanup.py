"""Tests for creating a conversation (including the empty-conversation
sweep that runs first), and for the new rename-conversation feature.

Route tests use a fake SupabaseService. Service tests mock the Supabase
SDK client directly, following the same mock-chain style as
test_conversation_archive_delete.py.
"""

import os
import unittest
from unittest import mock

import flask

from routes import main_bp
from tests.csrf_test_support import disable_csrf
from supabase_service import SupabaseService

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def __init__(self):
        self.swept_for = []
        self.create_calls = []
        self.rename_calls = []
        self.fail_sweep = False
        self.fail_create = False
        self.fail_rename_with = None
        self.deny_ensure = False

    def build_user_scoped_client(self, access_token):
        return object()

    def list_conversations(self, user_client, archived=False):
        # Only exercised when a test follows a redirect back to /chat.
        return []

    def ensure_conversation_for_user(self, user_client, conversation_id, user_id):
        # Only exercised when a test follows a redirect back to
        # /chat?conversation_id=... ; these tests aren't about that lookup,
        # so it always succeeds unless a test opts in to the denied case
        # (used to prove another user's conversation is never fetched).
        if self.deny_ensure:
            raise ValueError("Conversation not found for this user.")
        return None

    def fetch_messages_for_conversation(self, user_client, conversation_id):
        # A distinctive sentinel: if this ever ends up in a rendered page
        # despite ensure_conversation_for_user denying access above, that's
        # exactly the leak ChatConversationAccessScopeTests below checks
        # for.
        return [{"role": "assistant", "content": "SHOULD-NEVER-LEAK", "created_at": ""}]

    def delete_empty_conversations(self, user_client, user_id):
        self.swept_for.append(user_id)
        if self.fail_sweep:
            raise Exception("Sweep failed.")

    def create_conversation(self, user_client, user_id, title):
        self.create_calls.append((user_id, title))
        if self.fail_create:
            raise Exception("Create failed.")
        return "new-conversation-id"

    def rename_conversation(self, user_client, conversation_id, user_id, title):
        self.rename_calls.append((conversation_id, user_id, title))
        if self.fail_rename_with:
            raise self.fail_rename_with


class FakeAIService:
    def is_ready(self):
        return True


def _build_test_app(service):
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = service
    app.config["AI_SERVICE"] = FakeAIService()
    app.register_blueprint(main_bp)
    disable_csrf(app)
    return app


def _logged_in_session(client):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["user_email"] = "tenant@example.com"
        session["access_token"] = "fake-token"


class CreateConversationRouteTests(unittest.TestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/conversations", data={}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_sweeps_empty_conversations_before_creating_the_new_one(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations", data={"title": "Broken heat"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(service.swept_for, ["user-1"])
        self.assertEqual(service.create_calls, [("user-1", "Broken heat")])
        self.assertTrue(response.headers["Location"].endswith("conversation_id=new-conversation-id"))

    def test_blank_title_defaults_to_new_conversation(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        client.post("/conversations", data={}, follow_redirects=False)

        self.assertEqual(service.create_calls, [("user-1", "New conversation")])

    def test_sweep_failure_does_not_block_creating_the_new_conversation(self):
        """The sweep is best-effort cleanup, not a precondition -- a bug in
        it (or a transient Supabase error) must not stop someone from
        starting a new conversation."""
        service = FakeSupabaseService()
        service.fail_sweep = True
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations", data={"title": "Broken heat"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(service.create_calls, [("user-1", "Broken heat")])


class RenameConversationRouteTests(unittest.TestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/conversations/c1/rename", data={"title": "New name"}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_renames_and_redirects_back_into_the_conversation(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/conversations/c1/rename", data={"title": "  Broken heat  "}, follow_redirects=False
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("conversation_id=c1", response.headers["Location"])
        self.assertEqual(service.rename_calls, [("c1", "user-1", "Broken heat")])

    def test_blank_title_is_rejected_without_calling_the_service(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations/c1/rename", data={"title": "   "}, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("cannot be empty", response.get_data(as_text=True))
        self.assertEqual(service.rename_calls, [])

    def test_title_is_truncated_to_the_max_length_before_saving(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        client.post("/conversations/c1/rename", data={"title": "x" * 500})

        self.assertEqual(len(service.rename_calls), 1)
        self.assertEqual(len(service.rename_calls[0][2]), 120)

    def test_missing_conversation_shows_flash_without_crashing(self):
        service = FakeSupabaseService()
        service.fail_rename_with = ValueError("not found")
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/conversations/does-not-exist/rename", data={"title": "New name"}, follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("could not be found", response.get_data(as_text=True))


class ChatConversationAccessScopeTests(unittest.TestCase):
    """Nothing stops a visitor from typing any conversation_id into the
    /chat URL -- their own, a typo, or one that belongs to a different
    account entirely. The route's only defense is that
    ensure_conversation_for_user() is scoped to `.eq("id", ...).eq("user_id",
    ...)` (see supabase_service.py) and is always called before
    fetch_messages_for_conversation(); these tests pin that ordering down
    at the route level, using a fake service that would otherwise happily
    hand back a giveaway "SHOULD-NEVER-LEAK" message if the guard were
    ever removed or reordered."""

    def test_a_conversation_id_that_does_not_belong_to_this_user_shows_no_messages(self):
        service = FakeSupabaseService()
        service.deny_ensure = True
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/chat?conversation_id=someone-elses-conversation")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("could not be found", body)
        self.assertNotIn("SHOULD-NEVER-LEAK", body)

    def test_sending_a_message_into_a_conversation_that_does_not_belong_to_this_user_is_rejected(self):
        service = FakeSupabaseService()
        service.deny_ensure = True
        app = _build_test_app(service)
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/chat/message",
            json={"conversation_id": "someone-elses-conversation", "content": "hello"},
        )

        self.assertEqual(response.status_code, 404)


class DeleteEmptyConversationsServiceTests(unittest.TestCase):
    def _make_client(self):
        user_client = mock.MagicMock()
        conversations_table = mock.MagicMock()
        messages_table = mock.MagicMock()
        user_client.table.side_effect = lambda name: {
            "conversations": conversations_table,
            "messages": messages_table,
        }[name]
        return user_client, conversations_table, messages_table

    def test_deletes_conversations_with_no_messages(self):
        user_client, conversations_table, messages_table = self._make_client()
        conversations_table.select.return_value.eq.return_value.execute.return_value = mock.MagicMock(
            data=[{"id": "c1"}, {"id": "c2"}]
        )
        messages_table.select.return_value.in_.return_value.execute.return_value = mock.MagicMock(
            data=[{"conversation_id": "c1"}]
        )
        conversations_table.delete.return_value.in_.return_value.execute.return_value = mock.MagicMock(
            data=[{"id": "c2"}]
        )
        service = SupabaseService(client=mock.MagicMock())

        service.delete_empty_conversations(user_client, "user-1")

        conversations_table.delete.return_value.in_.assert_called_once_with("id", ["c2"])

    def test_does_nothing_when_every_conversation_has_messages(self):
        user_client, conversations_table, messages_table = self._make_client()
        conversations_table.select.return_value.eq.return_value.execute.return_value = mock.MagicMock(
            data=[{"id": "c1"}, {"id": "c2"}]
        )
        messages_table.select.return_value.in_.return_value.execute.return_value = mock.MagicMock(
            data=[{"conversation_id": "c1"}, {"conversation_id": "c2"}]
        )
        service = SupabaseService(client=mock.MagicMock())

        service.delete_empty_conversations(user_client, "user-1")

        conversations_table.delete.assert_not_called()

    def test_does_nothing_and_skips_the_messages_query_when_the_user_has_no_conversations(self):
        user_client, conversations_table, messages_table = self._make_client()
        conversations_table.select.return_value.eq.return_value.execute.return_value = mock.MagicMock(data=[])
        service = SupabaseService(client=mock.MagicMock())

        service.delete_empty_conversations(user_client, "user-1")

        messages_table.select.assert_not_called()
        conversations_table.delete.assert_not_called()


class RenameConversationServiceTests(unittest.TestCase):
    def test_updates_the_title(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.update.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[{"id": "c1"}])
        service = SupabaseService(client=mock.MagicMock())

        service.rename_conversation(user_client, "c1", "user-1", "Broken heat")

        user_client.table.return_value.update.assert_called_once_with({"title": "Broken heat"})

    def test_raises_value_error_when_no_row_matched(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.update.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[])
        service = SupabaseService(client=mock.MagicMock())

        with self.assertRaises(ValueError):
            service.rename_conversation(user_client, "c1", "user-1", "Broken heat")


if __name__ == "__main__":
    unittest.main()
