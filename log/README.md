# Deployment Logs

This folder stores deployment logs as separate `.txt` files.

## Available logs

- `2026-02-28-projectparadigm.txt` — Deployment summary for the ProjectParadigm update.
- `2026-07-26-chat-persistence.txt` — Chat history persistence + named conversations, plus next-roadmap todos (PDF handling, penal code reference, contextual advice).
- `2026-08-30-fix-chat-mobile-layout.txt` — Chat page made usable below 700px: the fixed 260px sidebar now stacks into a scrollable conversation strip. Desktop unchanged.
- `2026-08-30-supabase-keepalive.txt` — Supabase keep-alive GitHub Actions workflow, webhook error alerting, and removal of the hardcoded FLASK_SECRET_KEY default.
- `2026-09-06-fix-ra81-template-path.txt` — Fixed FormService's PDF template path (was "templates/RA-81.pdf", the real file is "templates/ra-81-fillable.pdf"), which would have crashed the intake-completion PDF download in production.
- `2026-09-06-retry-on-server-error.txt` — AIService now retries a transient Gemini 503 (ServerError) and falls back across models instead of failing on the first one; root cause of the "Sorry, I couldn't get a response right now" reports.
- `2026-09-06-signup-duplicate-check.txt` — Signup no longer creates a duplicate account for an existing email; login/signup reorganized with a shared visual design, later harmonized to a civic-blue palette.
