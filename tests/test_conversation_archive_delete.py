"""Tests for archiving, unarchiving, and deleting conversations: the
POST /conversations/<id>/archive, /unarchive, and /delete routes, plus
SupabaseService.set_conversation_archived / delete_conversation.

Route tests use a fake SupabaseService (no real Supabase call). Service
tests mock the Supabase SDK client directly.
"""

import unittest
from unittest import mock

import flask

from routes import main_bp
from supabase_service import SupabaseService
import os

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def __init__(self):
        self.archive_calls = []
        self.delete_calls = []
        self.fail_with = None

    def build_user_scoped_client(self, access_token):
        return object()

    def list_conversations(self, user_client, archived=False):
        return []

    def set_conversation_archived(self, user_client, conversation_id, user_id, archived):
        if self.fail_with:
            raise self.fail_with
        self.archive_calls.append((conversation_id, user_id, archived))

    def delete_conversation(self, user_client, conversation_id, user_id):
        if self.fail_with:
            raise self.fail_with
        self.delete_calls.append((conversation_id, user_id))


class FakeAIService:
    def is_ready(self):
        return True


def _build_test_app(service):
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = service
    app.config["AI_SERVICE"] = FakeAIService()
    app.register_blueprint(main_bp)
    return app


def _logged_in_session(client):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["user_email"] = "tenant@example.com"
        session["access_token"] = "fake-token"


class ArchiveRouteTests(unittest.TestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/conversations/c1/archive", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_archives_and_redirects_to_chat(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations/c1/archive", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/chat"))
        self.assertEqual(service.archive_calls, [("c1", "user-1", True)])

    def test_missing_conversation_shows_flash_without_crashing(self):
        service = FakeSupabaseService()
        service.fail_with = ValueError("not found")
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations/does-not-exist/archive", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("could not be found", response.get_data(as_text=True))


class UnarchiveRouteTests(unittest.TestCase):
    def test_unarchives_and_redirects_to_chat(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations/c1/unarchive", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/chat"))
        self.assertEqual(service.archive_calls, [("c1", "user-1", False)])


class DeleteRouteTests(unittest.TestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/conversations/c1/delete", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_deletes_and_redirects_to_chat(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations/c1/delete", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/chat"))
        self.assertEqual(service.delete_calls, [("c1", "user-1")])

    def test_missing_conversation_shows_flash_without_crashing(self):
        service = FakeSupabaseService()
        service.fail_with = ValueError("not found")
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/conversations/does-not-exist/delete", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("could not be found", response.get_data(as_text=True))


class SetConversationArchivedServiceTests(unittest.TestCase):
    def test_sets_archived_at_timestamp_when_archiving(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.update.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[{"id": "c1"}])
        service = SupabaseService(client=mock.MagicMock())

        service.set_conversation_archived(user_client, "c1", "user-1", archived=True)

        update_kwargs = user_client.table.return_value.update.call_args[0][0]
        self.assertIn("archived_at", update_kwargs)
        self.assertIsNotNone(update_kwargs["archived_at"])

    def test_clears_archived_at_when_unarchiving(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.update.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[{"id": "c1"}])
        service = SupabaseService(client=mock.MagicMock())

        service.set_conversation_archived(user_client, "c1", "user-1", archived=False)

        update_kwargs = user_client.table.return_value.update.call_args[0][0]
        self.assertIsNone(update_kwargs["archived_at"])

    def test_raises_value_error_when_no_row_matched(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.update.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[])
        service = SupabaseService(client=mock.MagicMock())

        with self.assertRaises(ValueError):
            service.set_conversation_archived(user_client, "c1", "user-1", archived=True)


class DeleteConversationServiceTests(unittest.TestCase):
    def test_deletes_the_conversation_row(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.delete.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[{"id": "c1"}])
        service = SupabaseService(client=mock.MagicMock())

        service.delete_conversation(user_client, "c1", "user-1")

        user_client.table.return_value.delete.assert_called_once()

    def test_raises_value_error_when_no_row_matched(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.delete.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.MagicMock(data=[])
        service = SupabaseService(client=mock.MagicMock())

        with self.assertRaises(ValueError):
            service.delete_conversation(user_client, "c1", "user-1")


class ListConversationsArchivedFilterTests(unittest.TestCase):
    def test_active_filters_out_archived(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.order.return_value
        chain.execute.return_value = mock.MagicMock(data=[{"id": "c1"}])
        service = SupabaseService(client=mock.MagicMock())

        service.list_conversations(user_client, archived=False)

        chain.is_.assert_called_once_with("archived_at", "null")

    def test_archived_filters_to_only_archived(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.order.return_value
        chain.execute.return_value = mock.MagicMock(data=[{"id": "c2"}])
        service = SupabaseService(client=mock.MagicMock())

        service.list_conversations(user_client, archived=True)

        chain.not_.is_.assert_called_once_with("archived_at", "null")


if __name__ == "__main__":
    unittest.main()
