"""Flask application bootstrap.

This file creates the Flask app, loads environment settings, wires in the
Supabase service, and registers the routes defined in routes.py.
"""

import os

from flask import Flask, request
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from ai_service import AIService
from alerting import configure_alerting
from config import load_settings
from rate_limit import limiter
from routes import main_bp
from supabase_service import SupabaseService


def create_app() -> Flask:
    """Build the Flask app and connect the services used by the route layer."""

    # Load environment-driven settings first so every later step uses one source
    # of truth for secrets, ports, and Supabase connection details.
    settings = load_settings()

    # Create the Flask application object that the rest of the project shares.
    app = Flask(__name__)
    app.secret_key = settings.flask_secret_key

    # Render (like Heroku/Railway/Fly) terminates TLS at its own edge proxy
    # and forwards plain HTTP to this process, adding X-Forwarded-Proto/
    # -Host/-For headers to say what the request actually looked like from
    # the outside. Flask/Werkzeug ignore those by default (trusting them
    # blindly would be a spoofing risk for an app that's reachable directly),
    # but this app is only ever reached through Render's proxy in
    # production, so trusting exactly one hop of them is safe and is the
    # standard fix for this deployment shape. Without it, request.scheme
    # reports "http" even though the visitor is on https, which made
    # url_for(..., _external=True) generate an http:// reset-password link
    # -- Supabase's redirect_to allowlist match is scheme-sensitive, so that
    # link never matched what was added to the allowlist and Supabase fell
    # back to the project's Site URL (localhost) instead.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # --- Abuse/overload failsafes and basic hardening -----------------
    #
    # None of this existed before: every POST route trusted the session
    # cookie alone (no CSRF token), nothing capped how fast a client could
    # hit the AI endpoint or the login form, and a client could send an
    # arbitrarily large request body. This block closes those gaps with
    # standard, low-risk defaults rather than anything bespoke.

    # A blanket cap on request body size -- this app has no file uploads,
    # so there's no legitimate reason for an inbound request to be large;
    # Flask returns 413 before the request even reaches route code for
    # anything over this, which is cheap insurance against someone trying
    # to tie up a worker with a multi-hundred-MB POST body.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB

    # Cross-Site Request Forgery protection for every state-changing
    # route (archive/delete/rename a conversation, change account
    # email/password, send a chat message, etc.) -- previously nothing
    # checked that a POST actually originated from this app's own pages,
    # so a malicious page could have silently submitted one of those
    # forms on a logged-in visitor's behalf using their session cookie.
    # WTF_CSRF_TIME_LIMIT is set to None (never expire on time alone,
    # only when the session itself does) rather than Flask-WTF's default
    # one-hour token lifetime -- a visitor who leaves e.g. the "forgot
    # password" page open for a while shouldn't have their submission
    # silently rejected.
    app.config["WTF_CSRF_TIME_LIMIT"] = None
    CSRFProtect(app)

    # Rate limiting: a blanket default (see rate_limit.py) plus tighter
    # limits on specific expensive or sensitive routes (login, signup,
    # the AI chat endpoint, etc.), applied via @limiter.limit(...) in
    # routes.py.
    limiter.init_app(app)

    # Session cookie hardening. SameSite=Lax is safe to set unconditionally
    # (it only affects cross-site requests, which this app has no
    # legitimate use for) and adds defense-in-depth against CSRF alongside
    # the token check above. Secure is gated on Render's own RENDER=true
    # environment variable (set automatically on every Render service)
    # rather than always-on, because always-on would stop the session
    # cookie from being sent at all during local development over plain
    # http, breaking `python app.py` locally; on Render, every request
    # really is HTTPS by the time it reaches this app (see the ProxyFix
    # comment above), so it's safe to require there.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("RENDER") == "true"

    @app.after_request
    def _add_security_headers(response):
        """A handful of standard response headers that cost nothing and
        close off whole categories of attack. Not included: a strict
        Content-Security-Policy without 'unsafe-inline' -- every template
        in this app uses inline <script>/<style> blocks rather than
        external files with nonces, so a strict CSP would break every
        page's JS/CSS today. The policy below still meaningfully narrows
        what a successful injection could do (no external scripts, no
        framing, no plugins) without that larger template rewrite.
        """

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'",
        )
        # Only meaningful (and only sent) over an actual HTTPS request --
        # ProxyFix (above) makes request.is_secure report this correctly
        # in production. Telling a browser to force HTTPS for the next two
        # years is not something to do accidentally over a plain-http local
        # dev request.
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response

    # Build the Supabase service here and store it on the app so routes.py can
    # retrieve the shared service for signup and login requests.
    supabase_service = SupabaseService.from_settings(settings)
    app.config["SUPABASE_SERVICE"] = supabase_service

    # Build the AI service and store it on the app for chat message generation.
    ai_service = AIService.from_settings(settings)
    app.config["AI_SERVICE"] = ai_service

    # Print startup status so it is obvious whether auth-related routes will be
    # ready when the server begins handling requests.
    if not supabase_service.is_ready():
        print(f"❌ {supabase_service.initialization_error}", flush=True)
    else:
        print("✅ Supabase client ready.", flush=True)

    if not ai_service.is_ready():
        print(f"❌ {ai_service.initialization_error}", flush=True)
    else:
        print("✅ Gemini client ready.", flush=True)

    # Wire logger.exception(...) calls (routes.py has several) to a webhook
    # so a failed chat turn reaches a human instead of only appearing in logs
    # no one is watching.
    if configure_alerting(settings.alert_webhook_url):
        print("✅ Error alerting is configured.", flush=True)
    else:
        print("⚠️  ALERT_WEBHOOK_URL is not set -- errors will only appear in logs.", flush=True)

    # Register the blueprint from routes.py so Flask knows about each page URL.
    app.register_blueprint(main_bp)
    return app


# Create the shared app object for local runs, tests, and WSGI servers.
app = create_app()


if __name__ == "__main__":
    # When the file is run directly, start the development server on the
    # configured host/port using the same settings loader as create_app().
    app_settings = load_settings()
    app.run(host="0.0.0.0", port=app_settings.port)
