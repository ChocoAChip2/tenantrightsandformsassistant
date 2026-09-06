"""Tests for config.load_settings(), focused on the FLASK_SECRET_KEY change.

No network access, no Supabase, no Gemini -- these only exercise
os.environ handling.
"""

import os
import unittest
from unittest import mock

from config import load_settings


class LoadSettingsFlaskSecretKeyTests(unittest.TestCase):
    def test_missing_flask_secret_key_raises(self):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_KEY": "anon-key",
            "GEMINI_API_KEY": "gemini-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_settings()
        # The message should actually help someone fix it, not just say "no".
        self.assertIn("FLASK_SECRET_KEY", str(ctx.exception))

    def test_blank_flask_secret_key_raises(self):
        env = {"FLASK_SECRET_KEY": "   "}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                load_settings()

    def test_old_published_default_is_no_longer_hardcoded_anywhere(self):
        # Regression guard: FLASK_SECRET_KEY must never silently resolve to
        # the old published constant, no matter what future refactors do to
        # this function.
        env = {"FLASK_SECRET_KEY": "a-real-secret-value"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertNotEqual(settings.flask_secret_key, "change-me-in-render")
        self.assertEqual(settings.flask_secret_key, "a-real-secret-value")

    def test_valid_settings_load_and_alert_webhook_url_defaults_to_none(self):
        env = {"FLASK_SECRET_KEY": "a-real-secret-value"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertIsNone(settings.alert_webhook_url)
        self.assertIsNone(settings.supabase_url)
        self.assertEqual(settings.port, 5000)

    def test_alert_webhook_url_is_read_from_environment(self):
        env = {
            "FLASK_SECRET_KEY": "a-real-secret-value",
            "ALERT_WEBHOOK_URL": "https://hooks.example.com/webhook",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.alert_webhook_url, "https://hooks.example.com/webhook")


if __name__ == "__main__":
    unittest.main()
