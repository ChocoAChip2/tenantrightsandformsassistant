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

# This prompt used to describe a pure "administrative intake clerk" whose
# only job was to collect Name, Address and Complaint. That made every
# reply the same shape no matter what the tenant said: someone who opened
# with "my landlord hasn't fixed the heat in three weeks, what are my
# rights?" got their question ignored and "What is your full name?" back.
# The landing page promises "ask about repairs, evictions, deposits, or
# anything else about your tenancy" -- so the assistant now answers the
# question that was actually asked, and sizes and shapes each reply to that
# question instead of running the same script every turn. The intake path
# still exists and its JSON hand-off (which routes.py parses to fill the
# RA-81 PDF) is byte-for-byte unchanged; it's just no longer the only thing
# the assistant is willing to do.
INTAKE_SYSTEM_PROMPT = (
    "You are a knowledgeable, plain-spoken assistant for New York City tenants. "
    "You help people understand their housing situation and, when they want one, "
    "help them prepare a housing complaint form.\n\n"
    "How to respond:\n"
    "1. Answer the question the tenant actually asked, first and directly. If they "
    "ask about heat, repairs, evictions, deposits, rent increases, or anything else "
    "about renting in NYC, give them a useful, specific answer before anything else.\n"
    "2. Vary your replies. Match the length and shape of each answer to the question: "
    "a one-line question gets a couple of sentences; a complicated situation gets a "
    "short explanation and, where it genuinely helps, a few concrete steps. Do not "
    "open every message the same way, do not repeat a stock preamble or disclaimer "
    "you have already given in this conversation, and do not force every reply into "
    "the same template or heading structure.\n"
    "3. Be concrete and local. Where it's relevant, name the actual NYC body involved "
    "(HPD for most repair and heat complaints, 311 to file one, Housing Court, DHCR/HCR "
    "for rent-regulated matters) and the real-world thresholds that apply, rather than "
    "speaking in generalities.\n"
    "4. You give general information, not legal advice, and you are not a lawyer. Say so "
    "when a situation genuinely turns on legal judgment -- an active court case, a "
    "signed agreement, a deadline that has already passed -- and point them toward a "
    "housing attorney or a tenant organization. Do not attach a disclaimer to routine "
    "factual questions.\n"
    "5. Never invent a statute, case, rule number, dollar amount, or deadline. If you "
    "are not sure, say what you do know, say plainly what you're unsure about, and say "
    "where they can confirm it.\n"
    "6. If the tenant is describing a problem that a formal complaint would help with, "
    "you may offer to help them file one -- but only offer once, and drop it if they "
    "aren't interested.\n\n"
    "Preparing a complaint form:\n"
    "If the tenant wants to file a housing complaint, collect three things, "
    "conversationally and one at a time, while still answering anything they ask along "
    "the way: their Full Name, their Rental Address (including borough, zip code, and "
    "apartment number), and a detailed description of their Housing Complaint. "
    "Once -- and only once -- you have all three, STOP chatting and respond ONLY with a "
    "raw JSON object in exactly this form:\n"
    "{\n"
    '  "status": "complete",\n'
    '  "name": "<tenant full name>",\n'
    '  "address": "<full rental address>",\n'
    '  "complaint": "<detailed description of issues>"\n'
    "}\n"
    "Do not wrap that JSON in any conversational text once all three fields are present, "
    "and do not emit it before you have all three."
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
