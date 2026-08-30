"""Tests for alerting.py.

urllib.request.urlopen is always mocked -- these must never make a real
network call, and there is no live webhook to hit anyway.
"""

import json
import logging
import unittest
from unittest import mock

from alerting import WebhookAlertHandler, configure_alerting


class WebhookAlertHandlerTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("tests.alerting")
        self.logger.setLevel(logging.DEBUG)

    def test_error_level_record_is_posted_to_webhook(self):
        handler = WebhookAlertHandler("https://hooks.example.com/webhook")
        handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
        self.logger.addHandler(handler)
        try:
            with mock.patch("alerting.urllib.request.urlopen") as mock_urlopen:
                self.logger.error("chat turn failed")

            mock_urlopen.assert_called_once()
            request = mock_urlopen.call_args[0][0]
            self.assertEqual(request.full_url, "https://hooks.example.com/webhook")
            self.assertEqual(request.get_header("Content-type"), "application/json")

            payload = json.loads(request.data.decode("utf-8"))
            self.assertIn("chat turn failed", payload["text"])
        finally:
            self.logger.removeHandler(handler)

    def test_exception_call_includes_traceback_in_payload(self):
        # This is the exact call shape routes.py uses: logger.exception(...)
        # inside an except block.
        handler = WebhookAlertHandler("https://hooks.example.com/webhook")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        try:
            with mock.patch("alerting.urllib.request.urlopen") as mock_urlopen:
                try:
                    raise ValueError("boom")
                except ValueError:
                    self.logger.exception("Failed to generate AI response.")

            payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
            self.assertIn("Failed to generate AI response.", payload["text"])
            self.assertIn("ValueError: boom", payload["text"])
        finally:
            self.logger.removeHandler(handler)

    def test_records_below_error_level_are_not_posted(self):
        handler = WebhookAlertHandler("https://hooks.example.com/webhook")
        self.logger.addHandler(handler)
        try:
            with mock.patch("alerting.urllib.request.urlopen") as mock_urlopen:
                self.logger.info("just fyi")
                self.logger.warning("worth a look, not urgent")
            mock_urlopen.assert_not_called()
        finally:
            self.logger.removeHandler(handler)

    def test_webhook_failure_is_swallowed_not_raised(self):
        handler = WebhookAlertHandler("https://hooks.example.com/webhook")
        self.logger.addHandler(handler)
        try:
            with mock.patch("alerting.urllib.request.urlopen", side_effect=OSError("network down")):
                with mock.patch.object(handler, "handleError") as mock_handle_error:
                    # Must not raise: a dead webhook must never break the
                    # request that logged the original error.
                    self.logger.error("still logged locally even if the webhook is down")
            mock_handle_error.assert_called_once()
        finally:
            self.logger.removeHandler(handler)


class ConfigureAlertingTests(unittest.TestCase):
    def test_unset_url_is_a_no_op(self):
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        try:
            self.assertFalse(configure_alerting(None))
            self.assertFalse(configure_alerting(""))
            self.assertEqual(root.handlers, handlers_before)
        finally:
            root.handlers = handlers_before

    def test_configured_url_attaches_one_handler(self):
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        try:
            result = configure_alerting("https://hooks.example.com/webhook")
            self.assertTrue(result)
            self.assertEqual(len(root.handlers), len(handlers_before) + 1)
            self.assertIsInstance(root.handlers[-1], WebhookAlertHandler)
        finally:
            root.handlers = handlers_before


if __name__ == "__main__":
    unittest.main()
