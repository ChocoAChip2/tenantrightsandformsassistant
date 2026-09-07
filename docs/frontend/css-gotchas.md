# CSS gotchas

Rules in these templates that look redundant, look like leftovers, or look
like they could be simplified — and are none of those things. Each one is
here because removing it caused a real, shipped bug.

---

## `[hidden] { display: none !important; }`

**In:** `chat.html`, `settings.html`, `reset_password.html`

**Do not remove.** This has broken three times in this codebase.

The native `hidden` attribute works because of a browser default rule,
`[hidden] { display: none }`. Any author rule that sets `display` on the
same element beats it — **regardless of selector specificity**, because the
browser default lives in the user-agent origin, which loses to author
styles outright. So an element that is both `hidden` and matched by, say,
`form { display: grid }` renders fully visible.

Every occurrence so far:

| Page | The author rule that won | What shipped |
| --- | --- | --- |
| `chat.html` | `.user-menu { display: flex }` | Settings/Log-out popup rendered open on every page load |
| `reset_password.html` | `form { display: grid }` | Reset form shown even on an invalid/expired link, next to the "invalid link" notice |
| `settings.html` | `form.stack { display: grid }`, `button { display: inline-flex }` | Would have rendered the delete-account "are you sure" form permanently open beside the button meant to reveal it |

Any new page that sets `display` on a class *and* toggles `hidden` on those
elements from JS needs this rule too. There are regression tests asserting
its presence in `tests/test_chat_page_ux_polish.py`,
`tests/test_password_reset.py` and `tests/test_account_deletion.py`.

---

## `.bubble { min-width: 0; overflow-wrap: anywhere; }`

**In:** `chat.html`

Both halves are required; either alone is not enough.

`white-space: pre-wrap` only wraps at spaces and newlines, so one long
unbroken run of characters — a long URL, a pasted hash or token — has
nothing to wrap at and renders wider than its bubble.

`overflow-wrap: anywhere` fixes that *only if* `min-width: 0` is also set.
`.bubble` sits inside a flex column (`.message`), and a flex item's default
`min-width` is `auto`, meaning "at least as wide as my unbroken content" —
which silently overrides `overflow-wrap` and reproduces the overflow.

---

## `.user-menu` must stay in flow — never `position: absolute`

**In:** `chat.html`

The account menu (Settings / Log out) is *not* a floating popup, and making
it one reintroduces a bug: with enough conversations in the sidebar, an
absolutely-positioned popup covers the last conversations and the Archived
section.

Instead `.sidebar-footer` is `display: flex; flex-direction: column-reverse`.
The trigger button is first in the markup and the popup second, so reversing
the visual order puts the trigger at the bottom with the popup opening above
it — while both stay genuinely in flow. Opening the popup therefore grows
the footer, and because `.sidebar-scroll` is the `flex: 1` sibling in the
same fixed-height column, the scroll area shrinks to make room. The popup
*cannot* cover the list, at any list length or scroll position.

`tests/test_chat_page_ux_polish.py::AccountMenuLayoutInvariantTests` asserts
`position: absolute` never comes back.

---

## `.user-menu form { display: contents; }`

**In:** `chat.html`

"Log out" is a `<form method="POST">`, not a link (see
[chat-page.md](chat-page.md#log-out-is-a-post)). `display: contents` removes
the form's own box from layout so its `<button>` participates directly in
`.user-menu`'s flex layout — lining up with the Settings link beside it
instead of the form adding a box that breaks the shared gap and alignment.

---

## `#send-btn` is styled by id on purpose

**In:** `chat.html`

There is a page-wide `button { min-width: 76px; height: 44px; … }` rule
sized for the primary buttons. The send button is a 34px circle, so it has
to beat that rule. Using the id selector gives it the specificity to win
**without `!important`**. Rewriting these as `.send-btn` would silently
restore the 76×44 sizing and break the composer pill.

The same applies to the `.row-menu-panel` buttons, which explicitly reset
`min-width`/`height` for the same reason — without those resets, menu items
inherit the big-button sizing and the panel looks broken.

---

## `.char-count` uses `visibility`, not `display`

**In:** `chat.html`

The character counter is hidden with `visibility: hidden` so it keeps
occupying its line. With `display: none` the counter would appear the moment
you cross the threshold and shove the whole composer up by a line
mid-sentence.

---

## `header h1 a` needs to out-specify `header a`

**In:** `chat.html`

The brand title is a link to `/chat`. `header a` styles the reference links
(smaller, muted). Without the extra specificity of `header h1 a`, the brand
title would pick up that nav-link color and size instead of reading as a
plain page title.
