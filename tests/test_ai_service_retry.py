"""Tests for AIService.generate_reply's handling of transient Gemini errors.

Root-caused a real production symptom: the chat page would show "Sorry, I
couldn't get a response right now" on what turned out to be a single 503
from Gemini. The SDK raises a 503 as ServerError, not ClientError -- and
the retry loop only ever caught ClientError, so a ServerError skipped the
fallback-model loop entirely on the very first transient hiccup. These
tests drive AIService.generate_reply directly with a mocked genai.Client,
so no real Gemini call is made and no real time.sleep delay happens
(time.sleep is patched out).
"""

import unittest
from unittest import mock

from google.genai.errors import ClientError, ServerError

from ai_service import AIService, FALLBACK_MODELS


def _server_error(status="UNAVAILABLE"):
    return ServerError(503, {"error": {"message": "The model is overloaded.", "status": status}})


def _client_error(code=404, status="NOT_FOUND", message="model not found"):
    return ClientError(code, {"error": {"message": message, "status": status}})


def _fake_response(text):
    return mock.MagicMock(text=text)


class GenerateReplySuccessAfterRetryTests(unittest.TestCase):
    @mock.patch("ai_service.time.sleep")
    def test_retries_same_model_after_a_single_503_then_succeeds(self, mock_sleep):
        client = mock.MagicMock()
        client.models.generate_content.side_effect = [
            _server_error(),
            _fake_response("Here is the housing law answer."),
        ]
        service = AIService(client=client)

        reply = service.generate_reply([{"role": "user", "content": "What are my rights?"}])

        self.assertEqual(reply, "Here is the housing law answer.")
        self.assertEqual(client.models.generate_content.call_count, 2)
        # Both calls were for the same (first) fallback model -- a transient
        # error should not immediately burn through to the next model.
        self.assertEqual(
            client.models.generate_content.call_args_list[0].kwargs["model"],
            client.models.generate_content.call_args_list[1].kwargs["model"],
        )
        mock_sleep.assert_called_once()

    @mock.patch("ai_service.time.sleep")
    def test_falls_back_to_next_model_after_exhausting_retries_on_first(self, mock_sleep):
        client = mock.MagicMock()
        client.models.generate_content.side_effect = [
            _server_error(),  # first model, attempt 1
            _server_error(),  # first model, attempt 2 (retry) -- still failing
            _fake_response("Answer from the fallback model."),  # second model succeeds
        ]
        service = AIService(client=client)

        reply = service.generate_reply([{"role": "user", "content": "What are my rights?"}])

        self.assertEqual(reply, "Answer from the fallback model.")
        self.assertEqual(client.models.generate_content.call_count, 3)
        models_tried = [call.kwargs["model"] for call in client.models.generate_content.call_args_list]
        self.assertEqual(models_tried, [FALLBACK_MODELS[0], FALLBACK_MODELS[0], FALLBACK_MODELS[1]])


class GenerateReplyExhaustedTests(unittest.TestCase):
    @mock.patch("ai_service.time.sleep")
    def test_raises_runtime_error_after_every_model_and_retry_fails(self, mock_sleep):
        client = mock.MagicMock()
        # 2 fallback models x (1 initial attempt + 1 retry) = 4 total calls, all 503.
        client.models.generate_content.side_effect = [_server_error() for _ in range(4)]
        service = AIService(client=client)

        with self.assertRaises(RuntimeError) as ctx:
            service.generate_reply([{"role": "user", "content": "What are my rights?"}])

        self.assertIn("temporarily unavailable", str(ctx.exception))
        self.assertEqual(client.models.generate_content.call_count, 4)


class ModelNotFoundStillSkipsToNextModelTests(unittest.TestCase):
    def test_404_moves_to_next_model_without_retrying_the_same_one(self):
        client = mock.MagicMock()
        client.models.generate_content.side_effect = [
            _client_error(),  # first model doesn't exist for this key
            _fake_response("Answer from the model that does exist."),
        ]
        service = AIService(client=client)

        reply = service.generate_reply([{"role": "user", "content": "What are my rights?"}])

        self.assertEqual(reply, "Answer from the model that does exist.")
        # No retry on the same (nonexistent) model -- straight to the next one.
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_non_404_client_error_is_raised_immediately_without_retry(self):
        client = mock.MagicMock()
        client.models.generate_content.side_effect = _client_error(
            code=400, status="INVALID_ARGUMENT", message="Request contained an invalid argument."
        )
        service = AIService(client=client)

        with self.assertRaises(ClientError):
            service.generate_reply([{"role": "user", "content": "What are my rights?"}])

        self.assertEqual(client.models.generate_content.call_count, 1)


if __name__ == "__main__":
    unittest.main()
