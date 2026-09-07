"""Renders the small subset of Markdown the assistant actually emits.

Gemini writes in Markdown whether or not you ask it to -- "**HPD**",
numbered steps, the occasional link. Those were being shown to tenants
literally, asterisks and all, because every bubble was rendered as plain
text. This converts them to HTML.

SECURITY: the output of this module is injected into the page with
Jinja's |safe, so it is the one place in this app where a mistake becomes
XSS. The defense is ordering, not filtering: EVERY input is HTML-escaped
first, before a single formatting rule runs. After that step no `<`, `>`
or `&` from the model (or from a tenant's own message, or from anything a
tenant pasted in and the model echoed back) can survive as markup -- the
only tags in the result are ones this file adds itself. There is no
allow-list of "safe" tags to get wrong, because no foreign tag ever
exists in the first place.

Link hrefs are the exception that still needs care, since an href is
attacker-controlled text inside a tag we do emit. Three things guard it:
only http and https URLs become links at all (so javascript: and data:
stay literal text); the URL patterns exclude quote characters; and the
initial escape uses quote=True. That last one is load-bearing -- with
quote=False a URL containing a double quote closed the href attribute
early and let the rest of the payload become new attributes, which is a
working XSS. Do not relax it.

Deliberately NOT supported: raw HTML passthrough, images, tables,
blockquotes. The assistant does not produce them for this use case, and
every construct added here is more surface for the escaping to be wrong
about.
"""

import html
import re

_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\*\w])\*(?=\S)([^\*]+?)(?<=\S)\*(?![\*\w])")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)\"\'<>]+)\)")
_BARE_URL = re.compile(r"(?<![\"'>=])(https?://[^\s<]+[^\s<.,;:!?)\]])")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_UNORDERED = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.*)$")


def _inline(text: str) -> str:
    """Inline formatting. `text` is already HTML-escaped."""
    text = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = _MD_LINK.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        text,
    )
    text = _BARE_URL.sub(
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        text,
    )
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    return text


def render_markdown(text: str | None) -> str:
    """Turn the assistant's Markdown into HTML safe to inject.

    Escaping happens once, here, before anything else touches the string.
    """
    if not text:
        return ""

    escaped = html.escape(str(text), quote=True)
    out: list[str] = []
    list_stack: list[str] = []
    paragraph: list[str] = []

    def close_paragraph():
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def close_lists():
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    for raw_line in escaped.split("\n"):
        line = raw_line.rstrip()

        if not line.strip():
            close_paragraph()
            close_lists()
            continue

        heading = _HEADING.match(line)
        if heading:
            close_paragraph()
            close_lists()
            # Clamped to h4-h6: these render inside a chat bubble, not as
            # page structure, so an h1 would be absurdly large and would
            # also fight the page's real heading hierarchy.
            level = min(6, 3 + len(heading.group(1)))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue

        ordered = _ORDERED.match(line)
        if ordered:
            close_paragraph()
            if list_stack and list_stack[-1] != "ol":
                close_lists()
            if not list_stack:
                list_stack.append("ol")
                out.append("<ol>")
            out.append(f"<li>{_inline(ordered.group(2).strip())}</li>")
            continue

        unordered = _UNORDERED.match(line)
        if unordered:
            close_paragraph()
            if list_stack and list_stack[-1] != "ul":
                close_lists()
            if not list_stack:
                list_stack.append("ul")
                out.append("<ul>")
            out.append(f"<li>{_inline(unordered.group(1).strip())}</li>")
            continue

        close_lists()
        paragraph.append(line.strip())

    close_paragraph()
    close_lists()
    return "".join(out)
