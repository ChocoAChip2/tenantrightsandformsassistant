"""Best-effort alerting so an unhandled error reaches a human.

routes.py already calls logger.exception(...) on failures (a broken chat
turn, a Supabase client that won't build, ...), but that output only ever
went to stdout/stderr -- fine for local dev, invisible in production unless
someone happens to be tailing Render logs at the moment it happens.

This attaches a logging.Handler that POSTs a short summary to a webhook URL
whenever something logs at ERROR level or above, which is exactly what
logger.exception() does. The payload shape ({"text": "..."}) is what Slack
incoming webhooks and Discord webhooks both expect out of the box; any other
endpoint can read the same field.

Deliberately built on stdlib only (urllib.request, logging) -- no new
dependency for something this small.
"""

import json
import logging
import urllib.request

# Keep a dead or slow webhook from ever blocking the request that triggered
# the alert.
_REQUEST_TIMEOUT_SECONDS = 5


class WebhookAlertHandler(logging.Handler):
    """Logging handler that POSTs ERROR-and-above records to a webhook URL."""

    def __init__(self, webhook_url: str):
        super().__init__(level=logging.ERROR)
        self.webhook_url = webhook_url

    def emit(self, record: logging.LogRecord) -> None:
        # logging.Handler's contract is that emit() must never raise: a dead
        # webhook or a network blip must not take down the request that
        # logged the original error, and must not recurse back into this
        # same handler by logging its own failure. self.handleError() is the
        # library-sanctioned escape hatch -- by default it prints to
        # stderr, which is exactly as visible as the pre-existing behavior.
        try:
            text = self.format(record)
            body = json.dumps({"text": f"NYC Tenant Assistant error:\n{text}"}).encode("utf-8")
            request = urllib.request.Request(
                self.webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception:
            self.handleError(record)


def configure_alerting(alert_webhook_url: str | None) -> bool:
    """Attach the webhook handler to the root logger if a URL is configured.

    Returns whether alerting is active, so app.py can print a startup status
    line the same way it already does for Supabase/Gemini readiness.
    """

    if not alert_webhook_url:
        return False

    handler = WebhookAlertHandler(alert_webhook_url)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))

    # Attached to the root logger, not a specific module logger: routes.py's
    # `logging.getLogger(__name__)` (and any future module logger) propagates
    # up to root by default, so this catches logger.exception() calls from
    # anywhere in the app without every call site needing to know alerting
    # exists.
    logging.getLogger().addHandler(handler)
    return True
