"""Tests for the ProxyFix wiring in app.py.

Render (like Heroku/Railway/Fly) terminates TLS at its own edge and forwards
plain HTTP to this process, adding X-Forwarded-Proto/-Host/-For headers to
describe what the request actually looked like from the outside. Without
ProxyFix, Flask ignores those headers, so request.scheme reports "http" even
though the visitor is on https -- which made url_for(..., _external=True)
generate an http:// link for the password-reset email. Supabase's
redirect_to allowlist match is scheme-sensitive, so that mismatched link
never matched what's configured in the Supabase dashboard, and Supabase
silently fell back to the project's Site URL (localhost) instead of sending
the visitor back to this app. This is the deployed-behind-a-proxy half of
that bug; the other half is a Supabase dashboard setting that can't be
fixed from code (see the log file for this branch).

app.py calls create_app() at import time (`app = create_app()` at module
scope), which requires FLASK_SECRET_KEY to already be set in the real
environment -- so, unlike every other test file, this one has to set that
env var *before* importing app, or the bare `import app` line itself raises.
No other test file imports app.py for exactly this reason; they build a
throwaway Flask app around main_bp instead. This file needs the real
create_app() specifically to test the ProxyFix wiring it does.
"""

import os

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-for-proxy-fix-test")

import unittest

from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app


class FakeSupabaseService:
    def __init__(self):
        self.reset_email_calls = []

    def send_password_reset_email(self, email, redirect_to=None):
        self.reset_email_calls.append({"email": email, "redirect_to": redirect_to})


class ProxyFixWiringTests(unittest.TestCase):
    def test_wsgi_app_is_wrapped_in_proxyfix(self):
        app = create_app()
        self.assertIsInstance(app.wsgi_app, ProxyFix)

    def test_forwarded_proto_and_host_headers_produce_a_matching_https_link(self):
        """This is the actual regression: without ProxyFix, the generated
        redirect_to would be http://localhost/reset-password (Werkzeug's
        test-client default host, standing in for whatever internal
        host/scheme Flask sees without trusting the forwarded headers)
        instead of the real public https:// URL Supabase's allowlist
        expects an exact match against."""
        app = create_app()
        service = FakeSupabaseService()
        app.config["SUPABASE_SERVICE"] = service
        client = app.test_client()

        # These are the headers Render's edge proxy adds to every request.
        client.post(
            "/forgot-password",
            data={"email": "tenant@example.com"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "nyc-tenant-assistant.onrender.com",
            },
        )

        self.assertEqual(len(service.reset_email_calls), 1)
        self.assertEqual(
            service.reset_email_calls[0]["redirect_to"],
            "https://nyc-tenant-assistant.onrender.com/reset-password",
        )

    def test_without_forwarded_headers_falls_back_to_the_direct_request_scheme(self):
        """Sanity check that ProxyFix only overrides scheme/host when the
        forwarded headers are actually present -- a plain http test request
        with no forwarding headers should still resolve normally instead of
        the middleware inventing an https URL out of nowhere."""
        app = create_app()
        service = FakeSupabaseService()
        app.config["SUPABASE_SERVICE"] = service
        client = app.test_client()

        client.post("/forgot-password", data={"email": "tenant@example.com"})

        self.assertEqual(len(service.reset_email_calls), 1)
        self.assertTrue(service.reset_email_calls[0]["redirect_to"].startswith("http://"))
        self.assertTrue(service.reset_email_calls[0]["redirect_to"].endswith("/reset-password"))


if __name__ == "__main__":
    unittest.main()
