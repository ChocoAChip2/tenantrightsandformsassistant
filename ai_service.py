"""Gemini client wrapper used by chat routes."""

from dataclasses import dataclass
from google import genai
from google.genai.errors import ClientError

from config import Settings

FALLBACK_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

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
                    continue
                raise

        attempted_models = ", ".join(FALLBACK_MODELS)
        raise RuntimeError(
            f"No supported Gemini model is available for this API key. Tried: {attempted_models}"
        ) from last_error
