"""Gemini client wrapper used by chat routes."""

import time
from dataclasses import dataclass
from google import genai
from google.genai.errors import ClientError, ServerError

from config import Settings

FALLBACK_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

# Gemini returns a 503 (ServerError, not ClientError) fairly often under load,
# especially on the free tier -- it's transient, not a sign the model/request
# is bad. Before this, a single 503 skipped straight past the fallback-model
# loop entirely (the `except ClientError` below never matches a ServerError)
# and surfaced as "Sorry, I couldn't get a response right now" on the very
# first hiccup. Retrying briefly, then trying the next fallback model, gives
# a transient overload a real chance to clear before giving up.
_SERVER_ERROR_RETRIES_PER_MODEL = 1
_SERVER_ERROR_RETRY_DELAY_SECONDS = 1.5

INTAKE_SYSTEM_PROMPT = (
    "You are an administrative intake clerk helping NYC tenants prepare housing complaint forms. "
    "Your objective is to collect three pieces of information: the tenant's Full Name, Rental Address "
    "(including borough, zip code, and apartment number), and a detailed description of their Housing Complaint.\n\n"
    "Conversation rules:\n"
    "1. Ask questions conversationally, one at a time, to gather missing details.\n"
    "2. Do NOT provide legal counsel; keep the focus on collecting administrative facts.\n"
    "3. While collecting information, reply in standard plain text.\n"
    "4. Once—and only once—you have collected ALL three pieces of information (Name, Address, Complaint), "
    "STOP chatting and respond ONLY with a raw JSON object formatted as follows:\n"
    "{\n"
    '  "status": "complete",\n'
    '  "name": "<tenant full name>",\n'
    '  "address": "<full rental address>",\n'
    '  "complaint": "<detailed description of issues>"\n'
    "}\n"
    "Do not wrap the JSON in conversational text once all three fields are present."
)


def _is_model_not_found_error(error: ClientError) -> bool:
    """Handle the SDK's varying 404 representations across environments."""
    status = getattr(error, "status", None)
    if status == 404:
        return True
    if status is not None and str(status).upper() == "NOT_FOUND":
        return True
    return "not found" in str(error).lower()


def _is_rate_limited_error(error: ClientError) -> bool:
    """A 429 (Gemini reports it as RESOURCE_EXHAUSTED) means this specific
    model's quota is used up for the current window -- not that the request
    itself is bad. This is the other real-world cause of "Sorry, I couldn't
    get a response right now" surfacing after a handful of messages: the
    free tier's per-minute request/token quota for one model gets used up
    quickly once a conversation's history (resent in full on every turn)
    grows, and a 429 was previously re-raised immediately with no retry or
    fallback at all, identically to a genuinely bad request. Retrying the
    same model within the same web request won't help -- the window won't
    reset that fast -- but the next model in FALLBACK_MODELS has its own,
    separate quota bucket, so move on to it right away instead.
    """
    code = getattr(error, "code", None)
    if code == 429:
        return True
    status = getattr(error, "status", None)
    if status is not None and str(status).upper() in {"RESOURCE_EXHAUSTED", "TOO_MANY_REQUESTS"}:
        return True
    message = str(error).lower()
    return "resource_exhausted" in message or "quota" in message


@dataclass
class AIService:
    client: genai.Client | None
    initialization_error: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "AIService":
        if not settings.gemini_api_key:
            return cls(client=None, initialization_error="Gemini API key is missing.")

        try:
            client = genai.Client(api_key=settings.gemini_api_key)
            return cls(client=client)
        except Exception as exc:
            return cls(client=None, initialization_error=f"Failed to create Gemini client: {exc}")

    def is_ready(self) -> bool:
        return self.client is not None

    def generate_reply(self, messages: list[dict[str, str]]) -> str:
        if not self.client:
            raise RuntimeError("Gemini is not configured yet.")

        contents = []
        system_instructions = [INTAKE_SYSTEM_PROMPT]

        for message in messages:
            role = message.get("role", "").strip().lower()
            content = message.get("content", "").strip()

            if not content or role not in {"system", "user", "assistant"}:
                continue

            if role == "system":
                system_instructions.append(content)
                continue

            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": content}],
            })

        if not contents:
            raise ValueError("No messages were provided for content generation.")

        config = {
            "system_instruction": "\n\n".join(system_instructions)
        }

        last_error = None
        for model_name in FALLBACK_MODELS:
            for attempt in range(_SERVER_ERROR_RETRIES_PER_MODEL + 1):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=config,
                    )
                    return (response.text or "").strip() or "I could not generate a response."
                except ClientError as exc:
                    if _is_model_not_found_error(exc):
                        last_error = exc
                        break  # this model doesn't exist for this key -- retrying it won't help
                    if _is_rate_limited_error(exc):
                        last_error = exc
                        break  # this model's quota is exhausted -- try the next model, don't hammer it
                    raise
                except ServerError as exc:
                    # Transient (503 "overloaded", 500, etc.) -- worth a short
                    # retry on this same model before moving on.
                    last_error = exc
                    if attempt < _SERVER_ERROR_RETRIES_PER_MODEL:
                        time.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
                        continue
                    break  # out of retries for this model; fall through to the next fallback model

        attempted_models = ", ".join(FALLBACK_MODELS)
        raise RuntimeError(
            f"Gemini is temporarily unavailable after retrying {attempted_models}."
        ) from last_error
