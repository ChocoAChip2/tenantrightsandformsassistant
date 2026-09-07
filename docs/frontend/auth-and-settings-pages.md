# Auth pages and Settings

Covers `login.html`, `signup.html`, `forgot_password.html`,
`reset_password.html` and `settings.html`. Shared conventions (tokens,
theming, CSRF, submit-disabling) are in [README.md](README.md); rules that
look removable and aren't are in [css-gotchas.md](css-gotchas.md).

The four auth pages share a card layout and are **light-only** — dark mode
is a `chat.html` / `settings.html` feature. They each repeat the same
`:root` token block, so a palette change has to be applied to all of them by
hand.

---

## Password show/hide toggle

Present on `login`, `signup`, `settings` (two fields) and `reset_password`
(two fields), duplicated identically in each — there is no shared template.

The button sits *inside* the input's box (`.password-field { position:
relative }` + `padding-right` on the input) rather than beside it. It swaps
between two inline SVGs via `innerHTML`: an open eye when the password is
hidden, a crossed-out eye when it's visible, with `aria-label` and `title`
updated to match.

**Why SVG and not emoji.** These were 👁️ / 🙈. An emoji renders as a
fixed-color picture that ignores CSS entirely, so it looked identical — and
about equally illegible — against both a light and a dark background. The
SVGs use `stroke="currentColor"`, so the icon follows
`.password-toggle`'s own `color: var(--muted)` (and `var(--text)` on hover),
exactly like every other icon-ish thing on the page. On `settings.html`,
which has dark mode, that means the icon actually tracks the theme.

---

## `reset_password.html` — recovery tokens live in the URL fragment

Supabase appends the recovery tokens to the URL **fragment**:

```
/reset-password#access_token=…&refresh_token=…&type=recovery
```

Browsers never send a fragment to the server, so Flask cannot see it. The
page's script reads the fragment, copies the tokens into hidden form fields,
and only then reveals the form. There is no server-side session at this
point — the visitor followed an emailed link, not a login — so **those
tokens are the only proof of identity `/reset-password` has**.

If the tokens are absent (link already used, expired, or someone navigated
here directly), the script shows an "invalid or expired" notice instead of a
form that cannot work.

Once captured, the tokens are cleared from the visible URL with
`history.replaceState` — they're sensitive and would otherwise sit in
browser history.

This page is also why `[hidden] { display: none !important; }` exists; see
[css-gotchas.md](css-gotchas.md#hidden--display-none-important).

---

## `settings.html`

### Theme radios

Write to the same `localStorage.theme` key that the pre-paint scripts on
this page and `chat.html` read. "Match system" *removes* the key and the
`data-theme` attribute rather than storing a `"system"` value, so the
`prefers-color-scheme` media query takes over.

### The account form is selected by id

The "Saving…" submit handler targets `#account-form`, **not**
`form.stack` — the delete-account confirmation below is also a `.stack`
form, and a `querySelector("form.stack")` would eventually claim the wrong
one.

### Danger Zone — account deletion

Sits alone at the bottom of the page, bordered and titled in `--error`, so
it doesn't read like "Save account changes" one card up.

The flow is two deliberate confirmations:

1. **"Delete my account"** reveals the confirmation form. It deletes
   nothing.
2. The form's submit button stays **disabled** until the typed email matches
   the signed-in address (case- and whitespace-insensitive) **and** the
   acknowledgement checkbox is ticked.

Both conditions are re-checked in `request_account_deletion()`. The
client-side gating is about making the second confirmation deliberate — a
crafted POST skips it entirely, and "your account and every conversation in
it" is not something to act on from one unverified request.

Submitting schedules deletion 30 days out; nothing is deleted then either.
While a request is pending, the delete control is replaced by a banner with
the exact date and a one-click **Cancel deletion**. Cancelling has no
confirmation gate on purpose — the risky direction is scheduling a deletion,
not stopping one.

The banner's date is rendered server-side as plain UTC (so it's never blank
or wrong without JS) and then rewritten client-side into the visitor's own
locale and timezone.

The actual deletion runs as a `pg_cron` job inside Postgres, not in this
app — see `supabase/migrations/20260907_account_deletion_requests.sql` and
`log/2026-09-07-account-deletion-and-message-cap.txt`.

---

## `signup.html`

Renders an "an account already exists for this email" notice when
`routes.py` sets `existing_account_email`. Supabase does not always report a
duplicate signup directly, so the route detects it and passes it through —
otherwise someone gets told to "check your email" for a confirmation that
was never sent.
