"""Integration tests for encryption at the data layer.

test_crypto_service.py proves the cipher works. These prove supabase_service
actually *uses* it in both directions: that what goes to Supabase is
ciphertext, that what comes back to routes.py is plaintext, and that the
opportunistic re-wrap upgrades stale rows as they are read.
"""

import base64
import os
import unittest
from unittest import mock

import crypto_service
from supabase_service import SupabaseService

KEY_A = base64.b64encode(b"\x01" * 32).decode()
KEY_B = base64.b64encode(b"\x02" * 32).decode()


class EncryptedStorageTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(crypto_service.reload_keys)
        self.env = mock.patch.dict(
            os.environ,
            {"DATA_ENCRYPTION_KEYS": f"k1:{KEY_A}", "DATA_ENCRYPTION_ACTIVE_KEY_ID": "k1"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        crypto_service.reload_keys()
        self.service = SupabaseService(client=mock.MagicMock())


class MessagesAreEncryptedAtRestTests(EncryptedStorageTestCase):
    def test_insert_message_sends_ciphertext_to_supabase(self):
        user_client = mock.MagicMock()
        secret = "My landlord has not fixed the heat since November."

        self.service.insert_message(
            user_client,
            {"conversation_id": "c1", "user_id": "u1", "role": "user", "content": secret},
        )

        sent = user_client.table.return_value.insert.call_args[0][0]
        self.assertNotIn("landlord", sent["content"])
        self.assertTrue(sent["content"].startswith("enc:v1:k1:"))
        # The non-sensitive columns must stay queryable.
        self.assertEqual(sent["conversation_id"], "c1")
        self.assertEqual(sent["role"], "user")

    def test_insert_message_does_not_mutate_the_caller_dict(self):
        """routes.py reuses the same dict to build the response it sends
        back to the browser, so encrypting in place would show the user
        their own message as ciphertext."""
        user_client = mock.MagicMock()
        message = {"conversation_id": "c1", "user_id": "u1", "role": "user", "content": "plain"}

        self.service.insert_message(user_client, message)

        self.assertEqual(message["content"], "plain")

    def test_fetch_messages_returns_plaintext(self):
        user_client = mock.MagicMock()
        sealed = crypto_service.encrypt("the boiler is broken")
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(data=[{"id": "m1", "role": "user", "content": sealed, "created_at": ""}])

        messages = self.service.fetch_messages_for_conversation(user_client, "c1")

        self.assertEqual(messages[0]["content"], "the boiler is broken")

    def test_fetch_messages_still_reads_rows_written_before_encryption(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(data=[{"id": "m1", "role": "user", "content": "old plaintext", "created_at": ""}])

        messages = self.service.fetch_messages_for_conversation(user_client, "c1")

        self.assertEqual(messages[0]["content"], "old plaintext")


    def test_fetch_messages_does_not_leak_the_internal_id_column(self):
        """id is selected only so stale rows can be re-encrypted; it is not
        part of what routes.py and the templates consume."""
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(
            data=[{"id": "m1", "role": "user", "content": crypto_service.encrypt("hi"), "created_at": ""}]
        )

        messages = self.service.fetch_messages_for_conversation(user_client, "c1")

        self.assertNotIn("id", messages[0])
        self.assertEqual(set(messages[0]), {"role", "content", "created_at"})

    def test_plaintext_message_bodies_are_upgraded_when_read(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(
            data=[{"id": "m1", "role": "user", "content": "old plaintext body", "created_at": ""}]
        )

        messages = self.service.fetch_messages_for_conversation(user_client, "c1")

        self.assertEqual(messages[0]["content"], "old plaintext body")
        written = user_client.table.return_value.update.call_args[0][0]["content"]
        self.assertTrue(written.startswith("enc:v1:k1:"))
        self.assertEqual(crypto_service.decrypt(written), "old plaintext body")

    def test_already_current_message_bodies_are_not_rewritten(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(
            data=[{"id": "m1", "role": "user", "content": crypto_service.encrypt("current"), "created_at": ""}]
        )

        self.service.fetch_messages_for_conversation(user_client, "c1")

        user_client.table.return_value.update.assert_not_called()

    def test_message_rewrap_is_capped_per_read(self):
        """A long conversation opened for the first time after encryption
        is enabled must not fire one UPDATE per message inside a single
        page render."""
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(
            data=[{"id": f"m{i}", "role": "user", "content": f"plaintext {i}", "created_at": ""} for i in range(200)]
        )

        messages = self.service.fetch_messages_for_conversation(user_client, "c1")

        self.assertEqual(len(messages), 200)
        self.assertEqual(messages[199]["content"], "plaintext 199")
        self.assertEqual(
            user_client.table.return_value.update.call_count,
            self.service.REWRAP_BATCH_LIMIT,
        )

    def test_a_failed_message_rewrap_never_breaks_the_read(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.select.return_value.eq.return_value.order.return_value
        chain.execute.return_value = mock.Mock(
            data=[{"id": "m1", "role": "user", "content": "plaintext", "created_at": ""}]
        )
        user_client.table.return_value.update.side_effect = Exception("postgrest is down")

        with self.assertLogs("supabase_service", level="ERROR"):
            messages = self.service.fetch_messages_for_conversation(user_client, "c1")

        self.assertEqual(messages[0]["content"], "plaintext")


class ConversationTitlesAreEncryptedTests(EncryptedStorageTestCase):
    def test_create_conversation_encrypts_the_title(self):
        user_client = mock.MagicMock()
        user_client.table.return_value.insert.return_value.execute.return_value = mock.Mock(
            data=[{"id": "c1"}]
        )

        self.service.create_conversation(user_client, "u1", "Eviction notice")

        sent = user_client.table.return_value.insert.call_args[0][0]
        self.assertNotIn("Eviction", sent["title"])
        self.assertTrue(sent["title"].startswith("enc:v1:k1:"))
        self.assertEqual(sent["user_id"], "u1")

    def test_rename_conversation_encrypts_the_new_title(self):
        user_client = mock.MagicMock()
        chain = user_client.table.return_value.update.return_value.eq.return_value.eq.return_value
        chain.execute.return_value = mock.Mock(data=[{"id": "c1"}])

        self.service.rename_conversation(user_client, "c1", "u1", "Heat complaint")

        sent = user_client.table.return_value.update.call_args[0][0]
        self.assertNotIn("Heat", sent["title"])

    def test_list_conversations_returns_plaintext_titles(self):
        user_client = mock.MagicMock()
        sealed = crypto_service.encrypt("Broken heat")
        query = user_client.table.return_value.select.return_value.order.return_value
        query.is_.return_value.execute.return_value = mock.Mock(
            data=[{"id": "c1", "title": sealed, "created_at": "", "updated_at": "", "archived_at": None}]
        )

        conversations = self.service.list_conversations(user_client, archived=False)

        self.assertEqual(conversations[0]["title"], "Broken heat")


class BackgroundRewrapTests(EncryptedStorageTestCase):
    def _list_with_stored_title(self, user_client, stored_title):
        query = user_client.table.return_value.select.return_value.order.return_value
        query.is_.return_value.execute.return_value = mock.Mock(
            data=[{"id": "c1", "title": stored_title, "created_at": "", "updated_at": "", "archived_at": None}]
        )
        return self.service.list_conversations(user_client, archived=False)

    def test_a_plaintext_title_is_upgraded_when_it_is_read(self):
        """This is the background migration: no batch job, no downtime --
        rows encrypt themselves as normal use touches them."""
        user_client = mock.MagicMock()

        conversations = self._list_with_stored_title(user_client, "still plaintext")

        self.assertEqual(conversations[0]["title"], "still plaintext")
        written = user_client.table.return_value.update.call_args[0][0]["title"]
        self.assertTrue(written.startswith("enc:v1:k1:"))
        self.assertEqual(crypto_service.decrypt(written), "still plaintext")

    def test_a_title_under_a_retired_key_is_re_encrypted_under_the_active_one(self):
        old_ciphertext = crypto_service.encrypt("Broken heat")
        with mock.patch.dict(
            os.environ,
            {"DATA_ENCRYPTION_KEYS": f"k1:{KEY_A},k2:{KEY_B}", "DATA_ENCRYPTION_ACTIVE_KEY_ID": "k2"},
        ):
            crypto_service.reload_keys()
            user_client = mock.MagicMock()

            conversations = self._list_with_stored_title(user_client, old_ciphertext)

            self.assertEqual(conversations[0]["title"], "Broken heat")
            written = user_client.table.return_value.update.call_args[0][0]["title"]
            self.assertTrue(written.startswith("enc:v1:k2:"))

    def test_a_current_title_is_not_rewritten(self):
        """The upgrade must be a no-op once a row is current, or every page
        render would issue a pointless write per conversation."""
        user_client = mock.MagicMock()

        self._list_with_stored_title(user_client, crypto_service.encrypt("Broken heat"))

        user_client.table.return_value.update.assert_not_called()

    def test_a_failed_rewrap_never_breaks_the_page(self):
        """It runs inside a normal page render. A write failure must leave
        the sidebar rendering fine and the row readable as it was."""
        user_client = mock.MagicMock()
        user_client.table.return_value.update.side_effect = Exception("postgrest is down")

        with self.assertLogs("supabase_service", level="ERROR"):
            conversations = self._list_with_stored_title(user_client, "still plaintext")

        self.assertEqual(conversations[0]["title"], "still plaintext")


class EncryptionDisabledTests(unittest.TestCase):
    def test_with_no_key_configured_nothing_changes(self):
        """The deploy-safety property, at the data layer: with no key set,
        content goes to Supabase exactly as it does today."""
        with mock.patch.dict(os.environ, {"DATA_ENCRYPTION_KEYS": "", "DATA_ENCRYPTION_ACTIVE_KEY_ID": ""}):
            crypto_service.reload_keys()
            self.addCleanup(crypto_service.reload_keys)
            service = SupabaseService(client=mock.MagicMock())
            user_client = mock.MagicMock()

            service.insert_message(user_client, {"conversation_id": "c1", "role": "user", "content": "plain text"})

            self.assertEqual(user_client.table.return_value.insert.call_args[0][0]["content"], "plain text")


if __name__ == "__main__":
    unittest.main()
