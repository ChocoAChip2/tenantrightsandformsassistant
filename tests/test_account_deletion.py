"""Tests for account deletion with a 30-day grace period.

Route tests use a fake SupabaseService; service tests mock the Supabase SDK
client directly, in the same style as
test_conversation_create_rename_and_cleanup.py.

What is deliberately NOT covered here: the actual deletion. That runs
inside Postgres as a pg_cron job calling
purge_expired_account_deletions() (see
supabase/migrations/20260907_account_deletion_requests.sql) precisely so
that the web app never holds the privilege to delete an auth user -- which
also means there is nothing in this codebase for a Python test to call.
The app's half of the feature, which is everything these tests cover, is
writing and removing the request row that the purge reads.
"""

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import flask

from rate_limit import limiter
from routes import ACCOUNT_DELETION_GRACE_PERIOD_DAYS, main_bp
from supabase_service import SupabaseService
from tests.csrf_test_support import disable_csrf

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

ACCOUNT_EMAIL = "tenant@example.com"


class FakeSupabaseService:
    def __init__(self, pending=None):
        self.pending = pending
        self.requested = []
        self.cancelled = []
        self.fail_lookup = False
        self.cancel_result = True

    def build_user_scoped_client(self, access_token):
        return object()

    def get_pending_account_deletion(self, user_client, user_id):
        if self.fail_lookup:
            raise Exception("lookup exploded")
        return self.pending

    def request_account_deletion(self, user_client, user_id, grace_period_days):
        self.requested.append((user_id, grace_period_days))
        return (datetime.now(timezone.utc) + timedelta(days=grace_period_days)).isoformat()

    def cancel_account_deletion(self, user_client, user_id):
        self.cancelled.append(user_id)
        return self.cancel_result


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
        session["user_email"] = ACCOUNT_EMAIL
        session["access_token"] = "fake-token"
        session["refresh_token"] = "fake-refresh"


def _valid_confirmation(email=ACCOUNT_EMAIL):
    return {"confirm_email": email, "confirm_understood": "yes"}


class AccountDeletionTestCase(unittest.TestCase):
    """The deletion routes are rate limited (5/min and 10/min), and
    rate_limit.limiter's storage is a module-level singleton shared across
    every app built in this process -- so without resetting it, the fourth
    or fifth test to POST here starts getting 429s instead of exercising
    the route. Same reason test_security_hardening.py does this."""

    def setUp(self):
        limiter.reset()


class RequestAccountDeletionRouteTests(AccountDeletionTestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/settings/account/delete", data=_valid_confirmation())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_missing_acknowledgement_does_not_schedule_anything(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account/delete",
            data={"confirm_email": ACCOUNT_EMAIL},
            follow_redirects=True,
        )

        self.assertIn("Tick the confirmation box", response.get_data(as_text=True))
        self.assertEqual(service.requested, [])

    def test_mismatched_email_does_not_schedule_anything(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account/delete",
            data=_valid_confirmation("someone-else@example.com"),
            follow_redirects=True,
        )

        self.assertIn("does not match", response.get_data(as_text=True))
        self.assertEqual(service.requested, [])

    def test_blank_email_does_not_schedule_anything(self):
        """An empty confirm_email must not sail through by comparing equal to
        an empty session email or to itself after stripping."""
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        client.post("/settings/account/delete", data={"confirm_email": "   ", "confirm_understood": "yes"})

        self.assertEqual(service.requested, [])

    def test_both_confirmations_schedule_the_deletion(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account/delete", data=_valid_confirmation(), follow_redirects=True
        )

        self.assertEqual(service.requested, [("user-1", ACCOUNT_DELETION_GRACE_PERIOD_DAYS)])
        self.assertIn("scheduled for deletion", response.get_data(as_text=True))

    def test_email_confirmation_ignores_case_and_surrounding_whitespace(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        client.post(
            "/settings/account/delete", data=_valid_confirmation("  TENANT@Example.COM  ")
        )

        self.assertEqual(service.requested, [("user-1", ACCOUNT_DELETION_GRACE_PERIOD_DAYS)])

    def test_a_service_failure_is_reported_without_crashing(self):
        service = FakeSupabaseService()
        service.request_account_deletion = mock.Mock(side_effect=Exception("nope"))
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post(
            "/settings/account/delete", data=_valid_confirmation(), follow_redirects=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Could not schedule", response.get_data(as_text=True))


class CancelAccountDeletionRouteTests(AccountDeletionTestCase):
    def test_logged_out_redirects_to_login(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()

        response = client.post("/settings/account/delete/cancel")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_cancelling_a_pending_deletion_reports_success(self):
        service = FakeSupabaseService()
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/settings/account/delete/cancel", follow_redirects=True)

        self.assertEqual(service.cancelled, ["user-1"])
        self.assertIn("no longer scheduled for deletion", response.get_data(as_text=True))

    def test_cancelling_with_nothing_pending_says_so(self):
        service = FakeSupabaseService()
        service.cancel_result = False
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.post("/settings/account/delete/cancel", follow_redirects=True)

        self.assertIn("no pending deletion", response.get_data(as_text=True))


class SettingsDangerZoneRenderTests(AccountDeletionTestCase):
    def test_shows_the_delete_control_when_nothing_is_pending(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/settings").get_data(as_text=True)

        self.assertIn("Delete my account", body)
        self.assertIn('id="delete-account-form"', body)
        # The pending-state banner and its cancel button must be absent.
        # (Not asserting on "scheduled for deletion" -- that phrase also
        # appears in the explanatory copy for the not-yet-scheduled state.)
        self.assertNotIn("Cancel deletion, keep my account", body)

    def test_the_confirmation_form_starts_hidden(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/settings").get_data(as_text=True)

        form_tag = re.search(r'<form[^>]*id="delete-account-form"[^>]*>', body)
        self.assertIsNotNone(form_tag)
        self.assertIn("hidden", form_tag.group(0))

    def test_hidden_attribute_override_rule_is_present(self):
        """settings.html sets `display` on both form.stack and button via
        author rules, which beat the browser's built-in
        `[hidden] { display: none }` no matter the specificity -- so without
        this rule the "are you sure" form renders permanently open. Same
        cascade bug that once shipped the reset-password form visible on an
        invalid link."""
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/settings").get_data(as_text=True)

        self.assertIn("[hidden] { display: none !important; }", body)

    def test_shows_the_pending_banner_and_cancel_button_when_scheduled(self):
        purge_after = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        service = FakeSupabaseService(
            pending={"requested_at": "2026-09-07T00:00:00+00:00", "purge_after": purge_after}
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/settings").get_data(as_text=True)

        self.assertIn("scheduled for deletion", body)
        self.assertIn("Cancel deletion, keep my account", body)
        # The one-click delete control must be gone while a request is live.
        self.assertNotIn('id="delete-account-form"', body)

    def test_settings_still_renders_if_the_pending_lookup_fails(self):
        service = FakeSupabaseService()
        service.fail_lookup = True
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        response = client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Appearance", response.get_data(as_text=True))


class AccountDeletionServiceTests(AccountDeletionTestCase):
    def _make_client(self):
        user_client = mock.MagicMock()
        return user_client

    def test_request_account_deletion_upserts_a_row_with_a_future_deadline(self):
        user_client = self._make_client()
        service = SupabaseService(client=mock.MagicMock())

        before = datetime.now(timezone.utc)
        purge_after_iso = service.request_account_deletion(user_client, "user-1", 30)
        after = datetime.now(timezone.utc)

        user_client.table.assert_called_with("account_deletion_requests")
        payload = user_client.table.return_value.upsert.call_args[0][0]
        self.assertEqual(payload["user_id"], "user-1")

        purge_after = datetime.fromisoformat(purge_after_iso)
        self.assertGreaterEqual(purge_after, before + timedelta(days=30))
        self.assertLessEqual(purge_after, after + timedelta(days=30))
        self.assertEqual(payload["purge_after"], purge_after_iso)

    def test_cancel_account_deletion_reports_whether_a_row_was_removed(self):
        service = SupabaseService(client=mock.MagicMock())

        user_client = self._make_client()
        chain = user_client.table.return_value.delete.return_value.eq.return_value
        chain.execute.return_value = mock.Mock(data=[{"user_id": "user-1"}])
        self.assertTrue(service.cancel_account_deletion(user_client, "user-1"))

        user_client = self._make_client()
        chain = user_client.table.return_value.delete.return_value.eq.return_value
        chain.execute.return_value = mock.Mock(data=[])
        self.assertFalse(service.cancel_account_deletion(user_client, "user-1"))

    def test_get_pending_account_deletion_returns_the_row_or_none(self):
        service = SupabaseService(client=mock.MagicMock())
        row = {"requested_at": "2026-09-07T00:00:00+00:00", "purge_after": "2026-10-07T00:00:00+00:00"}

        user_client = self._make_client()
        chain = user_client.table.return_value.select.return_value.eq.return_value.limit.return_value
        chain.execute.return_value = mock.Mock(data=[row])
        self.assertEqual(service.get_pending_account_deletion(user_client, "user-1"), row)

        user_client = self._make_client()
        chain = user_client.table.return_value.select.return_value.eq.return_value.limit.return_value
        chain.execute.return_value = mock.Mock(data=[])
        self.assertIsNone(service.get_pending_account_deletion(user_client, "user-1"))


if __name__ == "__main__":
    unittest.main()
