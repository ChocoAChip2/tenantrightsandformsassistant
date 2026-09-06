"""Tests for SupabaseService.sign_up()'s duplicate-account detection.

Supabase's own client is always a fake here (unittest.mock) -- these must
never contact the real Supabase API.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from supabase_service import SupabaseService


def _service_with_fake_client():
    fake_client = mock.MagicMock()
    service = SupabaseService(client=fake_client)
    return service, fake_client


class SignUpDuplicateDetectionTests(unittest.TestCase):
    def test_new_signup_with_nonempty_identities_returns_true(self):
        service, fake_client = _service_with_fake_client()
        fake_client.auth.sign_up.return_value = SimpleNamespace(
            user=SimpleNamespace(identities=[SimpleNamespace(id="identity-1")])
        )

        self.assertTrue(service.sign_up(email="new@example.com", password="hunter22"))

    def test_obfuscated_response_with_empty_identities_returns_false(self):
        # This is Supabase's documented behavior for an existing *confirmed*
        # email when "Confirm email" is enabled: a look-alike success
        # response instead of an error, to avoid leaking which emails are
        # registered.
        service, fake_client = _service_with_fake_client()
        fake_client.auth.sign_up.return_value = SimpleNamespace(
            user=SimpleNamespace(identities=[])
        )

        self.assertFalse(service.sign_up(email="existing@example.com", password="hunter22"))

    def test_already_registered_exception_returns_false(self):
        # This is the behavior when "Confirm email" is disabled: sign_up()
        # raises instead of returning an obfuscated response.
        service, fake_client = _service_with_fake_client()
        fake_client.auth.sign_up.side_effect = Exception("User already registered")

        self.assertFalse(service.sign_up(email="existing@example.com", password="hunter22"))

    def test_already_exists_exception_wording_also_returns_false(self):
        service, fake_client = _service_with_fake_client()
        fake_client.auth.sign_up.side_effect = Exception("A user with this email already exists")

        self.assertFalse(service.sign_up(email="existing@example.com", password="hunter22"))

    def test_unrelated_exception_is_reraised(self):
        service, fake_client = _service_with_fake_client()
        fake_client.auth.sign_up.side_effect = Exception("Password should be at least 6 characters")

        with self.assertRaises(Exception) as ctx:
            service.sign_up(email="new@example.com", password="short")
        self.assertIn("at least 6 characters", str(ctx.exception))

    def test_response_missing_identities_field_defaults_to_created(self):
        # If a future SDK version omits the field entirely rather than
        # returning [], don't misclassify a real signup as a duplicate.
        service, fake_client = _service_with_fake_client()
        fake_client.auth.sign_up.return_value = SimpleNamespace(user=SimpleNamespace())

        self.assertTrue(service.sign_up(email="new@example.com", password="hunter22"))

    def test_unconfigured_client_raises(self):
        service = SupabaseService(client=None, initialization_error="Supabase keys are missing.")
        with self.assertRaises(RuntimeError):
            service.sign_up(email="new@example.com", password="hunter22")


if __name__ == "__main__":
    unittest.main()
