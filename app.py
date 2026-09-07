"""Flask application bootstrap.

This file creates the Flask app, loads environment settings, wires in the
Supabase service, and registers the routes defined in routes.py.
"""

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from ai_service import AIService
from alerting import configure_alerting
from config import load_settings
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
