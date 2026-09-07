# NYC Tenant Assistant

NYC Tenant Assistant is a Flask web app with Supabase authentication and Gemini chat.  
Users sign up and log in first, then access the protected chat page.

## Render + Supabase confirmation

Render and Supabase work together in this project when these are set correctly:

- `SUPABASE_URL` (from your Supabase project settings)
- `SUPABASE_KEY` (usually the Supabase anon key for this flow)
- `GEMINI_API_KEY` (used to generate chat responses)
- `FLASK_SECRET_KEY` (any strong random string — **required, no default**; see below)
- `ALERT_WEBHOOK_URL` (optional; see [Error alerting](#error-alerting))

Render start command:

```bash
python app.py
```

Optional (if you install Gunicorn): `gunicorn wsgi:app`

If `SUPABASE_URL`/`SUPABASE_KEY`/`GEMINI_API_KEY` are missing, the app still
starts, but auth and chat actions fail with clear messages. `FLASK_SECRET_KEY`
is different: the app **will not start** without it (see below).

### `FLASK_SECRET_KEY` is required

`FLASK_SECRET_KEY` signs Flask's session cookie, which carries a user's
Supabase auth tokens. It used to default to the literal string
`"change-me-in-render"` if the environment variable was unset — a constant
published in this public repo, so any deployment that forgot to override it
could have its session cookies forged by anyone who read the source. There is
no default anymore: `config.load_settings()` now raises a `RuntimeError` at
startup if `FLASK_SECRET_KEY` is unset or blank, with a message that tells you
how to generate one:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set the result as `FLASK_SECRET_KEY` in Render (and locally, for `python app.py`
to run at all).

### Error alerting

`routes.py` calls `logger.exception(...)` on several failure paths (a broken
chat turn, a Supabase client that won't build, a conversation that can't be
created). That output previously only reached stdout/stderr — invisible in
production unless someone happened to be reading Render logs at the right
moment.

Set `ALERT_WEBHOOK_URL` to a Slack or Discord incoming webhook URL (or any
endpoint that accepts a JSON POST with a `text` field) and every `ERROR`-level
log record — including every `logger.exception(...)` call — is also posted
there. This is wired up in `alerting.py` and attached to the root logger in
`app.py`, so it needs no per-call-site changes. It's stdlib-only
(`urllib.request`), so it added no new dependency.

If `ALERT_WEBHOOK_URL` is unset, alerting is a no-op and behavior is unchanged
from before this branch — errors only appear in logs.

## Project structure (file purpose and relationships)

```text
.
├── app.py                # Application factory + Flask app bootstrap
├── ai_service.py         # Gemini client setup + response generation
├── alerting.py           # Webhook logging handler (ALERT_WEBHOOK_URL)
├── config.py             # Environment configuration loader
├── crypto_service.py     # Encryption at rest (versioned + rotatable; see docs/data-encryption.md)
├── markdown_service.py   # Renders the assistant's Markdown safely (escape-first)
├── login_lockout.py      # Failed-login lockout with exponential backoff
├── rate_limit.py         # Shared Flask-Limiter instance (own module: avoids a circular import)
├── supabase_service.py   # Supabase client setup + auth service methods
├── routes.py             # Signup/login/chat/logout HTTP routes (uses services)
├── wsgi.py               # WSGI entrypoint for Render/Gunicorn (imports app)
├── test.py               # Backward-compatible legacy entrypoint (imports app)
├── requirements.txt      # Python dependencies
├── docs/                 # data-encryption.md (threat model + key rotation), frontend/ (template rationale)
├── tests/                # Unit tests (python -m unittest discover -s tests)
├── supabase/migrations/  # SQL applied to the Supabase project (account deletion + its pg_cron purge)
├── .github/workflows/
│   └── keepalive.yml     # Supabase keep-alive cron (see below)
└── templates/
    ├── signup.html       # Signup page
    ├── login.html        # Login page
    └── chat.html         # Post-login chat placeholder
```

### How files connect

1. `app.py` calls `load_settings()` from `config.py`.
2. `app.py` creates `SupabaseService` from `supabase_service.py`.
3. `app.py` creates `AIService` from `ai_service.py`.
4. `app.py` calls `alerting.configure_alerting()` so `logger.exception(...)` calls anywhere in the app reach `ALERT_WEBHOOK_URL`, if set.
5. `app.py` stores shared services in app config and registers routes from `routes.py`.
6. `routes.py` handles auth and chat requests, rendering templates from `templates/`.
7. `wsgi.py` exposes the Flask app object for Render/Gunicorn.

## Local run

```bash
pip install -r requirements.txt
export SUPABASE_URL="..."
export SUPABASE_KEY="..."
export GEMINI_API_KEY="..."
export FLASK_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```

## Supabase keep-alive workflow

`.github/workflows/keepalive.yml` runs every 6 days (cron `0 12 */6 * *`) and
issues one authenticated, read-only `select id from conversations limit 1`
against Supabase's REST API. This exists because Supabase pauses free-tier
projects after 7 days without database activity — it has already happened
once and took the app down without anyone noticing. 6 days leaves a 1-day
safety margin.

It queries a real table rather than the bare API root, because a bare ping
isn't documented as counting toward Supabase's "user database activity"
check — see the comments at the top of the workflow file for the reasoning
and links. RLS blocks the query from returning any rows (it carries no user
JWT), which is expected: the point is that Postgres gets queried, not what it
returns.

**Setup:** add `SUPABASE_URL` and `SUPABASE_KEY` as **GitHub repository
secrets** (Settings → Secrets and variables → Actions) — the same values used
for Render, but GitHub Actions has its own separate secret store, so they
need to be added there too.

**Known limitation:** GitHub automatically disables scheduled workflows after
60 days with no repository activity (any push or PR, on any branch, resets
this). If that happens, the workflow stops firing silently and the Supabase
project will pause again. There's no way to prevent this from inside the
workflow — if the app goes down after a long quiet period, check the Actions
tab for a "this scheduled workflow is disabled" banner before looking
anywhere else.
