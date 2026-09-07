"""Tests for the abuse/overload failsafes and basic hardening added to
app.py: CSRF protection, rate limiting, the request-size cap, and the
security response headers.

Like test_proxy_fix.py, this imports the real create_app() (not a bare
Flask app built around main_bp) because all of this is wired up inside
create_app() itself -- so it needs FLASK_SECRET_KEY set before import, for
the same reason test_proxy_fix.py does (app.py does `app = create_app()`
at module scope).
"""

import os

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-for-security-hardening-test")

import re
import unittest

from app import create_app
from login_lockout import MAX_FAILED_ATTEMPTS, format_duration, reset_all as reset_lockouts
from rate_limit import limiter


class FakeSupabaseService:
    def __init__(self):
        self.sign_in_calls = []

    def sign_in(self, email, password):
        self.sign_in_calls.append((email, password))
        raise Exception("Invalid login credentials.")


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token hidden field found in the page"
    return match.group(1)


class SecurityHardeningTestCase(unittest.TestCase):
    """Base class: every test here shares the same setUp, since the
    rate limiter's storage is a module-level singleton shared across every
    create_app() call in this process (see rate_limit.py) rather than
    being scoped per Flask app -- without resetting it, an earlier test's
    requests could push a later, unrelated test over a limit."""

    def setUp(self):
        limiter.reset()
        reset_lockouts()


class CSRFProtectionTests(SecurityHardeningTestCase):
    def test_post_without_csrf_token_is_rejected(self):
        app = create_app()
        app.config["SUPABASE_SERVICE"] = FakeSupabaseService()
        client = app.test_client()

        response = client.post("/login", data={"email": "a@example.com", "password": "wrong"})

        self.assertEqual(response.status_code, 400)

    def test_post_with_a_valid_csrf_token_passes_the_check(self):
        """This only proves CSRF validation *passes* -- the login attempt
        itself still fails (wrong credentials), which is exactly the point:
        a 200 back on the login page (re-rendered with a flashed error)
        means the request got *past* CSRF and into real route logic,
        where a 400 would mean CSRF rejected it before that ever ran."""
        app = create_app()
        app.config["SUPABASE_SERVICE"] = FakeSupabaseService()
        client = app.test_client()

        get_response = client.get("/login")
        token = _extract_csrf_token(get_response.get_data(as_text=True))

        response = client.post(
            "/login",
            data={"email": "a@example.com", "password": "wrong", "csrf_token": token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Login failed", response.get_data(as_text=True))

    def test_a_forged_csrf_token_is_rejected(self):
        app = create_app()
        app.config["SUPABASE_SERVICE"] = FakeSupabaseService()
        client = app.test_client()

        client.get("/login")  # establishes a session with a real token in it
        response = client.post(
            "/login",
            data={"email": "a@example.com", "password": "wrong", "csrf_token": "not-the-real-token"},
        )

        self.assertEqual(response.status_code, 400)


class LogoutIsPostOnlyTests(SecurityHardeningTestCase):
    """/logout used to be a plain GET route -- a page embedding
    <img src="/logout"> could force-log-out any visitor who loaded it,
    since browsers send GET requests for those with no user interaction
    at all. Now it's POST-only (and therefore CSRF-protected like every
    other state-changing route), so only an actual form submission from
    this app's own page can trigger it."""

    def test_get_is_not_allowed(self):
        app = create_app()
        client = app.test_client()

        response = client.get("/logout")

        self.assertEqual(response.status_code, 405)

    def test_post_with_a_valid_csrf_token_clears_the_session(self):
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "user-1"
            session["user_email"] = "tenant@example.com"

        get_response = client.get("/settings")
        token = _extract_csrf_token(get_response.get_data(as_text=True))

        response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_post_without_a_csrf_token_is_rejected(self):
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "user-1"

        response = client.post("/logout")

        self.assertEqual(response.status_code, 400)


class SecurityHeaderTests(SecurityHardeningTestCase):
    def test_standard_hardening_headers_are_present(self):
        app = create_app()
        client = app.test_client()

        response = client.get("/login")

        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            response.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin"
        )
        self.assertIn("default-src 'self'", response.headers.get("Content-Security-Policy", ""))

    def test_hsts_is_only_sent_over_an_actually_secure_request(self):
        app = create_app()
        client = app.test_client()

        plain_response = client.get("/login")
        self.assertNotIn("Strict-Transport-Security", plain_response.headers)

        # ProxyFix (already wired for the reset-password link fix) is what
        # makes request.is_secure trust this header from Render's edge --
        # same header, reused here to exercise the HSTS branch.
        secure_response = client.get("/login", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("max-age=", secure_response.headers.get("Strict-Transport-Security", ""))


class RequestSizeLimitTests(SecurityHardeningTestCase):
    def test_oversized_request_body_is_rejected(self):
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        client = app.test_client()

        oversized_body = "a" * (3 * 1024 * 1024)  # 3 MB, over the 2 MB cap
        response = client.post(
            "/login",
            data={"email": "a@example.com", "password": oversized_body},
        )

        self.assertEqual(response.status_code, 413)


class FakeSupabaseServiceForChat:
    def build_user_scoped_client(self, access_token):
        return object()

    def ensure_conversation_for_user(self, user_client, conversation_id, user_id):
        return None

    def insert_message(self, user_client, message):
        pass

    def fetch_messages_for_conversation(self, user_client, conversation_id):
        return []


class FakeAIServiceForChat:
    def is_ready(self):
        return True

    def generate_reply(self, history):
        return "a reply"


class ChatMessageLengthLimitTests(SecurityHardeningTestCase):
    def test_overlong_message_is_rejected_before_calling_the_ai_service(self):
        from routes import MAX_MESSAGE_LENGTH

        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SUPABASE_SERVICE"] = FakeSupabaseServiceForChat()
        app.config["AI_SERVICE"] = FakeAIServiceForChat()
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "user-1"
            session["access_token"] = "fake-token"

        response = client.post(
            "/chat/message",
            json={"conversation_id": "c1", "content": "x" * (MAX_MESSAGE_LENGTH + 1)},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("too long", response.get_json()["error"])

    def test_message_at_the_limit_is_accepted(self):
        from routes import MAX_MESSAGE_LENGTH

        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SUPABASE_SERVICE"] = FakeSupabaseServiceForChat()
        app.config["AI_SERVICE"] = FakeAIServiceForChat()
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "user-1"
            session["access_token"] = "fake-token"

        response = client.post(
            "/chat/message",
            json={"conversation_id": "c1", "content": "x" * MAX_MESSAGE_LENGTH},
        )

        self.assertEqual(response.status_code, 200)


class LoginLockoutRouteTests(SecurityHardeningTestCase):
    """Route-level wiring for login_lockout.py -- see
    tests/test_login_lockout.py for the module's own unit tests. Presets
    the "already locked out" state directly through the module rather
    than sending MAX_FAILED_ATTEMPTS real requests, since the login
    route's own 10-per-minute rate limit (routes.py) would otherwise
    intercept those requests first at the exact same threshold."""

    def test_a_locked_out_caller_is_refused_without_calling_supabase(self):
        from login_lockout import record_failure

        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        service = FakeSupabaseService()
        app.config["SUPABASE_SERVICE"] = service
        client = app.test_client()

        # The login route keys lockouts the same way as rate limiting: the
        # session's user_id if signed in, otherwise the caller's IP --
        # which for an anonymous test-client request is 127.0.0.1.
        for _ in range(MAX_FAILED_ATTEMPTS):
            record_failure("127.0.0.1")

        response = client.post("/login", data={"email": "a@example.com", "password": "wrong"})
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Too many failed login attempts", body)
        self.assertIn(format_duration(3600), body)  # the base, first-lockout duration
        self.assertEqual(service.sign_in_calls, [])

    def test_a_successful_login_clears_the_failure_streak(self):
        from login_lockout import record_failure, record_success, seconds_until_unlocked

        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            record_failure("127.0.0.1")

        # A real sign-in would clear the streak via record_success(); this
        # test only needs to confirm the wiring calls it, so it reaches
        # into the module directly rather than standing up a full fake
        # auth_response object.
        record_success("127.0.0.1")

        self.assertEqual(seconds_until_unlocked("127.0.0.1"), 0)


class RateLimitTests(SecurityHardeningTestCase):
    def test_login_is_rate_limited_after_repeated_attempts(self):
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SUPABASE_SERVICE"] = FakeSupabaseService()
        client = app.test_client()

        # The route is limited to 10 POSTs per minute (see routes.py).
        statuses = [
            client.post("/login", data={"email": "a@example.com", "password": "wrong"}).status_code
            for _ in range(11)
        ]

        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)

    def test_rate_limit_key_prefers_the_signed_in_user_over_the_ip(self):
        app = create_app()
        with app.test_request_context("/"):
            from flask import session

            from rate_limit import rate_limit_key

            session["user_id"] = "user-42"
            self.assertEqual(rate_limit_key(), "user-42")

    def test_rate_limit_key_falls_back_to_the_remote_address_when_anonymous(self):
        app = create_app()
        with app.test_request_context("/", environ_overrides={"REMOTE_ADDR": "203.0.113.5"}):
            from rate_limit import rate_limit_key

            self.assertEqual(rate_limit_key(), "203.0.113.5")


if __name__ == "__main__":
    unittest.main()
