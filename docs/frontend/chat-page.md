# `chat.html`

The main app page: sidebar (conversation list + account menu) on the left,
conversation or greeting on the right. Layout gotchas live in
[css-gotchas.md](css-gotchas.md); this covers behavior.

---

## Sending a message

`#chat-form` posts to `/chat/message` via `fetch()`/JSON, not a form submit,
so the page never reloads mid-conversation. Because there's no form to carry
a hidden field, the CSRF token is read from `<meta name="csrf-token">` into
an `X-CSRFToken` header.

- **Enter sends, Shift+Enter inserts a newline.** Only plain Enter is
  intercepted — Shift+Enter is a textarea's normal behavior and is left
  alone.
- **The textarea auto-grows.** `resize: none` removes the browser's native
  drag-to-resize corner handle, and an `input` handler sets
  `style.height = "auto"` then `= scrollHeight + "px"`. It grows to
  `max-height: 200px`, after which `overflow-y: auto` takes over.
- **A "thinking" indicator** (three pulsing dots) is rendered the instant a
  message is sent and removed the instant a reply or error arrives, so there
  is never a silent gap where it looks like the click didn't register.

### Character counter

The cap is `MAX_MESSAGE_LENGTH` in `routes.py`, passed into the template and
rendered as the textarea's `maxlength`. The JS reads the number **back off
`messageInput.maxLength`** rather than having it interpolated into the
script a second time — one source of truth, and a missing template variable
can't produce a JS syntax error in the page's main script block.
`maxLength` is `-1` when the attribute is absent, which disables the counter
rather than showing nonsense.

It appears with **250 characters left** and turns amber at **100 left**, as
a fixed remaining count rather than a percentage of the cap: "250 left"
means the same thing regardless of what the cap is, whereas a percentage
silently changes how much warning people get every time the cap moves. It
counts *down*, because at the point it becomes visible, remaining is the
number that matters.

`maxlength` alone truncates an over-long paste silently, with no explanation
— the counter is what makes the ceiling visible before someone hits it. The
server check in `chat_message()` is the one that actually enforces it.

---

## Renaming a conversation

Two entry points, one flow: the **Rename** item in a row's `…` menu, and
**double-clicking the conversation name**. Both swap `.conversation-link`
for that row's `.rename-form` — a real `<form method="POST">`, like
Archive/Delete, so Enter submits it normally with no fetch/JSON involved.
Escape, or clicking/tabbing away (`focusout`, captured), cancels back to the
link without saving.

### The double-click / navigation race

`.conversation-link` is a real `<a href>`. A browser fires a click's own
default navigation **before** it ever dispatches `dblclick`, so calling
`preventDefault()` in a `dblclick` handler is too late — the first click has
already navigated away.

So every click on the link is intercepted and held for **220ms**:

- a second click inside that window → cancel the pending navigation, start the rename
- no second click → perform the navigation, just 220ms later

`event.detail > 1` identifies the second click of a double-click so it
doesn't schedule a second navigation, but `preventDefault()` still runs for
it — returning early *before* preventing the default would let the second
click navigate.

The 220ms delay on ordinary single-click navigation is the deliberate cost
of supporting double-click-to-rename on what is otherwise a plain link.

---

## The `…` row menu

Archive/Delete (Restore/Delete when archived) live behind one `…` trigger
per row rather than a row of buttons — one consistent affordance instead of
several to parse. The trigger is hidden until the row is hovered/focused or
its own menu is open.

One delegated document-level handler serves every row, since the sidebar can
hold any number of conversations. Only one menu is open at a time; a click
outside any trigger/panel closes whatever is open, as does Escape.
Opening a rename closes the `…` panel first, or the two sit on top of each
other.

---

## Suggestion chips auto-send

The greeting page's chips ("My landlord won't fix something", …) each carry
a `data-prompt`. Clicking one both **starts a conversation and asks the
question**, which takes two steps because there is no conversation to send a
message into until the server creates one:

1. The click stashes `data-prompt` in `sessionStorage` under
   `pendingChatPrompt`, then lets the chip's normal
   `POST /conversations` → redirect proceed.
2. On the new page, if the chat box is empty *and* a pending prompt exists,
   the script fills the composer and calls `chatForm.requestSubmit()` — the
   same path Enter or a click takes.

The empty-chat-box check is a safety net as much as a condition: it means
this can only ever fire into a conversation with no messages, never into one
someone is partway through, even if a stale value somehow survived in
storage. Every storage access is wrapped in `try/catch` — with storage
unavailable (private browsing, blocked site data) the chip degrades to a
plain "start a new chat" button.

---

## Duplicate-submit prevention

`+ New chat` and the chips are real page POST/redirects, not AJAX, so a slow
connection leaves a window where a second click fires a second request and
creates a second conversation. Every `[data-new-chat-form]` disables its
button on submit to close that window. The backstop for whatever still slips
through is server-side: `create_conversation()` sweeps the user's
message-less conversations before creating a new one.

`+ New chat` takes no title — new conversations are created as "New
conversation" and renamed afterward via the flow above.

---

## Log out is a POST

The account menu's "Log out" is a `<form method="POST">` with a CSRF token,
not an `<a href>`. A GET logout route can be triggered by any page that
embeds `<img src="/logout">`, with no interaction at all. See the docstring
on the `/logout` route. `display: contents` on that form keeps it laid out
like the Settings link beside it.

---

## Time-of-day greeting

The empty state renders a plain server-side fallback (so it's never blank
before JS runs), which the script immediately replaces with a greeting based
on the visitor's **local** clock — "Good morning" / "Good afternoon" /
"Good evening" / "Working late" — plus their name and today's date. The
server has no way to know the visitor's timezone; the browser does.

---

## Motion

Deliberately small and always tied to something the person just did:

- New messages fade and rise in. **Only messages added during the session**
  animate — `is-new` is applied in `renderMessage()` and never rendered
  server-side, so opening a long conversation doesn't make the whole history
  flutter into place.
- New messages scroll into view smoothly; the initial page-load scroll stays
  an instant jump.
- Rows, chips, the `…` trigger and the account button transition their
  colors; the send button scales down while pressed, chips shift a pixel. A
  click with no feedback until the network answers feels broken.

All of it is off under `prefers-reduced-motion: reduce`, including the
smooth scroll, which the JS checks via `matchMedia` because a CSS media
query can't reach `scrollTo`'s `behavior` option.

---

## Mobile (below 700px)

The sidebar becomes a short horizontal strip above the chat so the
conversation keeps most of a phone screen. Per-row archive/delete is hidden
there — the cramped chip strip has no room for a comfortable tap target.
Those actions stay available from the Archived section on desktop, or from
the conversation once opened.
