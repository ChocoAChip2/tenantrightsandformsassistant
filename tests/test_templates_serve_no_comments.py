"""Guards the rule that no template comment is ever served to a browser.

Everything in this app's frontend is inline in the templates -- no build
step, no bundler, no separate .css/.js files -- so any HTML/CSS/JS comment
written in a template goes straight out to anyone who opens View Source.
The rationale that would otherwise live in those comments lives in
docs/frontend/ instead, and each template carries a Jinja ({# ... #})
pointer to it, which Jinja strips before the response is written.

Without this test the templates drift back to being commented within a
couple of branches, because writing a comment next to the code is the
natural thing to do.
"""

import os
import re
import unittest

import flask

from routes import main_bp
from tests.csrf_test_support import disable_csrf

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def build_user_scoped_client(self, access_token):
        return object()

    def list_conversations(self, user_client, archived=False):
        if archived:
            return []
        return [{"id": "c1", "title": "Broken heat", "created_at": "", "updated_at": "", "archived_at": None}]

    def ensure_conversation_for_user(self, *args, **kwargs):
        return None

    def fetch_messages_for_conversation(self, *args, **kwargs):
        return [{"role": "user", "content": "hello", "created_at": ""}]

    def get_pending_account_deletion(self, *args, **kwargs):
        return None


class FakeAIService:
    def is_ready(self):
        return True


def _build_test_app():
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = FakeSupabaseService()
    app.config["AI_SERVICE"] = FakeAIService()
    app.register_blueprint(main_bp)
    disable_csrf(app)
    return app


PAGES = ["/login", "/", "/forgot-password", "/reset-password", "/settings", "/chat", "/chat?conversation_id=c1"]


class ServedPagesCarryNoCommentsTests(unittest.TestCase):
    def setUp(self):
        app = _build_test_app()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = "user-1"
            session["user_email"] = "tenant@example.com"
            session["access_token"] = "fake-token"
            session["refresh_token"] = "fake-refresh"

    def _pages(self):
        for path in PAGES:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"{path} did not render")
            yield path, response.get_data(as_text=True)

    def test_no_html_comments_are_served(self):
        for path, body in self._pages():
            self.assertNotIn("<!--", body, f"{path} serves an HTML comment")

    def test_no_jinja_comment_markers_leak_into_the_response(self):
        """Each template opens with a {# ... #} pointer to docs/frontend/.
        Jinja strips those, so seeing one in the response would mean it was
        written somewhere Jinja doesn't parse (inside a string, say)."""
        for path, body in self._pages():
            self.assertNotIn("{#", body, f"{path} leaked a Jinja comment")

    def test_no_css_comments_are_served(self):
        for path, body in self._pages():
            for block in re.findall(r"<style[^>]*>(.*?)</style>", body, re.S):
                self.assertNotIn("/*", block, f"{path} serves a CSS comment")

    def test_no_js_comments_are_served(self):
        """Only line-comment *shapes* are checked: '//' also legitimately
        appears mid-line inside URLs (the inline SVG icons are full of
        https:// in string literals), so a bare substring check would be a
        false positive."""
        for path, body in self._pages():
            for block in re.findall(r"<script[^>]*>(.*?)</script>", body, re.S):
                for line in block.split("\n"):
                    line = line.strip()
                    self.assertFalse(
                        line.startswith("//") or line.startswith("/*"),
                        f"{path} serves a JS comment: {line[:70]}",
                    )


class TemplatesPointAtTheDocsTests(unittest.TestCase):
    def test_every_template_carries_a_jinja_pointer_to_the_docs(self):
        """The docs are only useful if someone editing a template finds
        them, and the pointer can't be an HTML comment (it would be
        served), so it's a Jinja one."""
        for name in os.listdir(_TEMPLATES_DIR):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(_TEMPLATES_DIR, name), encoding="utf-8") as handle:
                source = handle.read()
            self.assertIn("docs/frontend/", source, f"{name} has no pointer to the frontend docs")


if __name__ == "__main__":
    unittest.main()
