"""Regression tests for a batch of chat.html UX fixes:

- The native `hidden` attribute must actually hide things: `.user-menu`
  (and, in reset_password.html, `#reset-form`) used to render permanently
  visible because an author CSS rule with `display: X` on the same element
  always beats the browser's built-in `[hidden] { display: none }` default,
  regardless of selector specificity. That looked like "Settings/Log out is
  blocking the bottom of my conversation list" when it was really "the
  account popup never actually closes".
- Per-conversation Archive/Delete/Restore actions were reorganized behind a
  single "..." (row-menu-trigger) button instead of always-visible buttons.
- The "+ New chat" form and the greeting page's suggestion chips disable
  their submit button once clicked, so a slow response (or an impatient
  double click) can't create a pile of empty conversations.
- The account popup (#user-menu) is laid out in-flow inside a
  column-reverse .sidebar-footer instead of floating via
  position:absolute, so opening it shrinks the scrollable conversation
  list (via flexbox) rather than covering the last rows -- including
  archived ones -- with the popup.
- The Archived section is visually grouped as a folder (folder emoji in
  its <summary>).

These are template-only assertions (string/structure checks on the
rendered HTML), which is what a static analyzer or grep-based review can
verify. The actual pixel-level "does opening the menu still cover a row"
behavior was verified interactively with Playwright while making the
change; that's not repeated here since this suite has no headless browser
step, but the CSS/markup invariants it depends on (column-reverse layout,
no position:absolute on #user-menu, the [hidden] override rule) are
locked in below so a future edit can't silently reintroduce the bug.
"""

import os
import unittest

import flask

from routes import main_bp
from tests.csrf_test_support import disable_csrf

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


class FakeSupabaseService:
    def __init__(self, conversations=None, archived_conversations=None):
        self._conversations = conversations or []
        self._archived_conversations = archived_conversations or []

    def build_user_scoped_client(self, access_token):
        return object()

    def list_conversations(self, user_client, archived=False):
        return self._archived_conversations if archived else self._conversations

    def ensure_conversation_for_user(self, user_client, conversation_id, user_id):
        return None

    def fetch_messages_for_conversation(self, user_client, conversation_id):
        return []


class FakeAIService:
    def is_ready(self):
        return True


def _build_test_app(service):
    app = flask.Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.config["SUPABASE_SERVICE"] = service
    app.config["AI_SERVICE"] = FakeAIService()
    app.register_blueprint(main_bp)
    disable_csrf(app)
    return app


def _logged_in_session(client):
    with client.session_transaction() as session:
        session["user_id"] = "user-1"
        session["user_email"] = "tenant@example.com"
        session["access_token"] = "fake-token"


class RowMenuMarkupTests(unittest.TestCase):
    def test_active_conversation_row_has_trigger_and_archive_delete_menu(self):
        service = FakeSupabaseService(
            conversations=[{"id": "c1", "title": "Broken heat", "created_at": "", "updated_at": ""}]
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat?conversation_id=c1").get_data(as_text=True)

        self.assertIn('data-row-menu-trigger', body)
        self.assertIn('class="row-menu-panel"', body)
        self.assertIn('/conversations/c1/archive', body)
        self.assertIn('/conversations/c1/delete', body)
        self.assertIn('role="menuitem"', body)
        # The old always-visible icon buttons must be gone.
        self.assertNotIn("icon-btn", body)
        self.assertNotIn("conversation-actions", body)

    def test_archived_conversation_row_offers_restore_and_delete(self):
        service = FakeSupabaseService(
            archived_conversations=[{"id": "a1", "title": "Old chat", "created_at": "", "updated_at": ""}]
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat").get_data(as_text=True)

        self.assertIn('/conversations/a1/unarchive', body)
        self.assertIn('/conversations/a1/delete', body)

    def test_archived_summary_shows_folder_emoji(self):
        service = FakeSupabaseService(
            archived_conversations=[{"id": "a1", "title": "Old chat", "created_at": "", "updated_at": ""}]
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat").get_data(as_text=True)

        self.assertIn("&#128193;", body)  # folder emoji, as the HTML entity the template emits
        self.assertIn("Archived (1)", body)


class SpamPreventionMarkupTests(unittest.TestCase):
    def test_new_chat_and_suggestion_forms_are_marked_for_disable_on_submit(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat").get_data(as_text=True)

        self.assertGreaterEqual(body.count("data-new-chat-form"), 1)
        self.assertIn("disabled = true", body)


class KeyboardHandlingTests(unittest.TestCase):
    def test_message_input_handles_shift_enter_for_newline(self):
        service = FakeSupabaseService(
            conversations=[{"id": "c1", "title": "Broken heat", "created_at": "", "updated_at": ""}]
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat?conversation_id=c1").get_data(as_text=True)

        self.assertIn("shiftKey", body)
        self.assertIn("requestSubmit", body)


class AccountMenuLayoutInvariantTests(unittest.TestCase):
    """Locks in the fix for "Settings/Log out blocks the conversation
    list / hides archived chats": the popup must be laid out in-flow
    (column-reverse footer) rather than an absolutely-positioned overlay,
    so it can never paint on top of the scrollable conversation list.
    """

    def test_hidden_attribute_override_rule_present(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat").get_data(as_text=True)

        self.assertIn("[hidden] { display: none !important; }", body)

    def test_account_menu_is_in_flow_not_absolutely_positioned(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat").get_data(as_text=True)

        self.assertIn("flex-direction: column-reverse", body)
        # The old floating-popup rule must not have crept back in.
        user_menu_rule_start = body.index(".user-menu {")
        user_menu_rule = body[user_menu_rule_start:body.index("}", user_menu_rule_start)]
        self.assertNotIn("position: absolute", user_menu_rule)


class HeaderHomeLinkTests(unittest.TestCase):
    def test_brand_title_links_back_to_chat(self):
        app = _build_test_app(FakeSupabaseService())
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat").get_data(as_text=True)

        self.assertIn('<h1><a href="/chat">NYC Tenant Assistant</a></h1>', body)


class MessageBubbleOverflowTests(unittest.TestCase):
    """A single long unbroken run of characters (a long URL, a pasted
    token/hash) has no space to wrap at under `white-space: pre-wrap`
    alone, so it used to render wider than the bubble and spill past its
    edge. This locks in the fix (overflow-wrap: anywhere, plus min-width:
    0 to actually let it take effect inside a flex column)."""

    def test_bubble_has_overflow_wrap_and_flex_min_width_reset(self):
        service = FakeSupabaseService(
            conversations=[{"id": "c1", "title": "Broken heat", "created_at": "", "updated_at": ""}]
        )
        app = _build_test_app(service)
        client = app.test_client()
        _logged_in_session(client)

        body = client.get("/chat?conversation_id=c1").get_data(as_text=True)

        self.assertIn(".bubble { min-width: 0; overflow-wrap: anywhere; }", body)


if __name__ == "__main__":
    unittest.main()
