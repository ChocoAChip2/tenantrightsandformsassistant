"""Configuration helpers for environment variables used across the app."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Typed container for values loaded from the deployment environment."""

    supabase_url: str | None
    supabase_key: str | None
    gemini_api_key: str | None
    flask_secret_key: str
    port: int
    alert_webhook_url: str | None


def load_settings() -> Settings:
    """Read environment variables once and return them as a Settings object."""

    # app.py and supabase_service.py both rely on these values, so this function
    # keeps the environment-to-Python mapping in one place.

    # FLASK_SECRET_KEY has no default on purpose. It used to fall back to the
    # literal string "change-me-in-render" -- a constant published in this
    # public repo, so anyone could forge session cookies for any deployment
    # that forgot to override it. Failing loudly at startup is cheaper than a
    # silent security hole.
    raw_secret_key = os.environ.get("FLASK_SECRET_KEY")
    flask_secret_key = raw_secret_key.strip() if raw_secret_key else ""
    if not flask_secret_key:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set (or is blank). Generate one, e.g. "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"`, "
            "and set it as an environment variable before starting the app."
        )

    return Settings(
        supabase_url=os.environ.get("SUPABASE_URL"),
        supabase_key=os.environ.get("SUPABASE_KEY"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY"),
        flask_secret_key=flask_secret_key,
        port=int(os.environ.get("PORT", 5000)),
        # Optional: a Slack/Discord-compatible incoming webhook URL. When
        # unset, alerting.configure_alerting() is a no-op and errors only
        # reach the process logs, same as before this branch.
        alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL"),
    )
