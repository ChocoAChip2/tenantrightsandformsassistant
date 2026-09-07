"""Tests for markdown_service.render_markdown.

Its output is injected with |safe, so half of these are about what must
NOT come out. The escaping-first design means a foreign tag can never
exist in the output at all -- these pin that down, including the specific
bug found while building it: with html.escape(quote=False) a double quote
inside a URL closed the href attribute and let the rest of the payload
become new attributes.
"""

import re
import unittest

from markdown_service import render_markdown

ALLOWED_TAGS = {
    "p", "br", "strong", "em", "code", "ul", "ol", "li", "a", "h4", "h5", "h6",
}


def injected_markup(rendered: str) -> str | None:
    """Return a description of any tag/attribute we did not intend to emit."""
    for name, attrs in re.findall(r"<(/?[a-zA-Z][^\s>]*)([^>]*)>", rendered):
        if name.lstrip("/").lower() not in ALLOWED_TAGS:
            return f"unexpected tag <{name}>"
        if re.search(r"\bon\w+\s*=", attrs, re.I):
            return f"event handler on <{name}>"
        for href in re.findall(r'href="([^"]*)"', attrs):
            if not href.startswith(("http://", "https://")):
                return f"non-http href {href!r}"
    return None


class FormattingTests(unittest.TestCase):
    def test_bold_and_italic(self):
        self.assertIn("<strong>HPD</strong>", render_markdown("Contact **HPD** today"))
        self.assertIn("<em>heat</em>", render_markdown("a *heat* inspection"))

    def test_inline_code(self):
        self.assertIn("<code>RA-81</code>", render_markdown("Use the `RA-81` form"))

    def test_bullet_list(self):
        rendered = render_markdown("- call 311\n- keep a log")
        self.assertIn("<ul>", rendered)
        self.assertEqual(rendered.count("<li>"), 2)

    def test_numbered_list(self):
        rendered = render_markdown("1. call 311\n2. file online")
        self.assertIn("<ol>", rendered)
        self.assertEqual(rendered.count("<li>"), 2)

    def test_headings_are_clamped_to_small_sizes(self):
        """These render inside a chat bubble, not as page structure."""
        self.assertIn("<h4>", render_markdown("# Top level"))
        self.assertNotIn("<h1>", render_markdown("# Top level"))

    def test_markdown_links(self):
        rendered = render_markdown("[HPD portal](https://www1.nyc.gov/hpd)")
        self.assertIn('href="https://www1.nyc.gov/hpd"', rendered)
        self.assertIn('rel="noopener noreferrer"', rendered)
        self.assertIn(">HPD portal</a>", rendered)

    def test_bare_urls_become_links(self):
        self.assertIn('href="https://portal311.nyc.gov"', render_markdown("file at https://portal311.nyc.gov"))

    def test_paragraphs_and_line_breaks(self):
        rendered = render_markdown("first para\nsame para\n\nsecond para")
        self.assertEqual(rendered.count("<p>"), 2)
        self.assertIn("<br>", rendered)

    def test_empty_input(self):
        self.assertEqual(render_markdown(""), "")
        self.assertEqual(render_markdown(None), "")

    def test_plain_text_is_unchanged_apart_from_being_wrapped(self):
        self.assertEqual(render_markdown("just a sentence"), "<p>just a sentence</p>")


class EscapingTests(unittest.TestCase):
    def test_script_tags_never_survive(self):
        rendered = render_markdown("<script>alert(1)</script>")
        self.assertIsNone(injected_markup(rendered))
        self.assertNotIn("<script", rendered.lower())

    def test_an_img_onerror_payload_is_inert_text(self):
        rendered = render_markdown("**<img src=x onerror=alert(1)>**")
        self.assertIsNone(injected_markup(rendered))
        self.assertIn("&lt;img", rendered)

    def test_javascript_urls_do_not_become_links(self):
        rendered = render_markdown("[click](javascript:alert(1))")
        self.assertIsNone(injected_markup(rendered))
        self.assertNotIn("<a", rendered)

    def test_a_quote_in_a_url_cannot_break_out_of_the_href(self):
        """The real bug this module shipped with for about ten minutes:
        html.escape(quote=False) left the double quote intact, so
        href="https://ok.com" onmouseover="alert(1)" became live
        attributes."""
        for payload in (
            '[click](https://ok.com" onmouseover="alert(1))',
            'https://ok.com" onmouseover="alert(1)',
        ):
            rendered = render_markdown(payload)
            self.assertIsNone(injected_markup(rendered), payload)
            # The payload may still appear as inert text in the paragraph
            # body -- that is fine and expected. What must not happen is it
            # appearing INSIDE a tag, so check the tags specifically.
            for _, attrs in re.findall(r"<([a-zA-Z][^\s>]*)([^>]*)>", rendered):
                self.assertNotIn("onmouseover", attrs.lower(), payload)
            self.assertNotIn('href="https://ok.com"  onmouseover', rendered)

    def test_raw_anchor_tags_are_escaped_not_passed_through(self):
        rendered = render_markdown('<a href="https://evil.example">x</a>')
        self.assertIsNone(injected_markup(rendered))
        self.assertIn("&lt;a", rendered)

    def test_ampersands_in_urls_are_encoded(self):
        rendered = render_markdown("[x](https://a.example/?q=1&b=2)")
        self.assertIn("&amp;b=2", rendered)
        self.assertIsNone(injected_markup(rendered))

    def test_apostrophes_render_normally(self):
        self.assertIn("&#x27;", render_markdown("it's fine"))


if __name__ == "__main__":
    unittest.main()
