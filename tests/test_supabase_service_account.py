"""Unit tests for SupabaseService.update_account and
fetch_all_conversations_with_messages -- the account-settings and
chat-history-download helpers added alongside the settings page.

These mock the Supabase SDK client entirely; no real network call is made.
"""

import unittest
from unittest import mock

from supabase_service import SupabaseService


class UpdateAccountTests(unittest.TestCase):
    def _service_with_fake_client(self):
        fake_client = mock.MagicMock()
        fake_client.supabase_url = "https://example.supabase.co"
        fake_client.supabase_key = "anon-key"
        return SupabaseService(client=fake_client), fake_client

    def test_raises_when_supabase_not_configured(self):
        service = SupabaseService(client=None, initialization_error="missing keys")

        with self.assertRaises(RuntimeError):
            service.update_account("tok-a", "tok-r", email="new@example.com")

    def test_raises_when_no_attributes_given(self):
        service, _ = self._service_with_fake_client()

        with self.assertRaises(ValueError):
            service.update_account("tok-a", "tok-r")

    @mock.patch("supabase_service.create_client")
    def test_sets_session_then_updates_email_and_password(self, mock_create_client):
        service, _ = self._service_with_fake_client()
        account_client = mock.MagicMock()
        mock_create_client.return_value = account_client

        service.update_account("tok-a", "tok-r", email="new@example.com", password="newpassword1")

        account_client.auth.set_session.assert_called_once_with("tok-a", "tok-r")
        account_client.auth.update_user.assert_called_once_with(
            {"email": "new@example.com", "password": "newpassword1"}
        )

    @mock.patch("supabase_service.create_client")
    def test_password_only_update_omits_email_key(self, mock_create_client):
        service, _ = self._service_with_fake_client()
        account_client = mock.MagicMock()
        mock_create_client.return_value = account_client

        service.update_account("tok-a", "tok-r", password="newpassword1")

        account_client.auth.update_user.assert_called_once_with({"password": "newpassword1"})


class FetchAllConversationsWithMessagesTests(unittest.TestCase):
    def test_attaches_messages_to_each_active_and_archived_conversation(self):
        service = SupabaseService(client=mock.MagicMock())
        service.list_conversations = mock.MagicMock(
            side_effect=lambda user_client, archived=False: (
                [{"id": "c2", "title": "Deposit"}] if archived else [{"id": "c1", "title": "Broken heat"}]
            )
        )
        service.fetch_messages_for_conversation = mock.MagicMock(
            side_effect=lambda user_client, conversation_id: [{"role": "user", "content": conversation_id}]
        )

        result = service.fetch_all_conversations_with_messages(user_client=object())

        # Both the active conversation (c1) and the archived one (c2) are
        # included -- archiving something shouldn't drop it from the export.
        self.assertEqual(result[0]["messages"], [{"role": "user", "content": "c1"}])
        self.assertEqual(result[1]["messages"], [{"role": "user", "content": "c2"}])


if __name__ == "__main__":
    unittest.main()
