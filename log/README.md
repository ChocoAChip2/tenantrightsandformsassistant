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
- `2026-09-06-minimalist-redesign.txt` — Chat page moved from a dark ChatGPT-styled theme to a minimalist, single-accent-color design shared with the login/signup redesign; light/dark/system theme support; NYC HPD / NY State HCR reference links; new Settings page (appearance, account email/password change, chat-history download); civic-blue palette; reconciled with main's intake/PDF-download flow.
- `2026-09-06-chat-ui-clarity-and-rate-limit.txt` — Archive/Delete/Restore buttons now spelled out instead of icon glyphs; a "thinking" indicator shows while waiting on a reply; AIService now falls back to the next model on a 429/RESOURCE_EXHAUSTED instead of failing outright, fixing "Sorry, I couldn't get a response right now" appearing after a handful of messages.
- `2026-09-07-forgot-password-and-input-polish.txt` — Forgot/reset-password flow via Supabase recovery links; an eye-emoji show/hide toggle on every password field; higher-contrast suggestion-chip buttons on the post-login chat landing page.
- `2026-09-07-proxyfix-for-reset-links.txt` — Fixed the password-reset email link landing on localhost: app.py now trusts Render's X-Forwarded-Proto/-Host headers (via ProxyFix) so url_for(_external=True) generates the real https:// URL instead of an http:// one that didn't match Supabase's redirect allowlist.
- `2026-09-07-sidebar-menu-reorg-and-ux.txt` — Fixed the Settings/Log-out popup rendering permanently open (a `[hidden]`-vs-`display` CSS cascade bug, also fixed identically in reset_password.html) and, once fixed, made it structurally impossible for the popup to cover the conversation list or an open Archived folder (column-reverse in-flow layout instead of a floating overlay); moved per-row Archive/Delete/Restore behind a "..." menu; added client-side spam-prevention for "+ New chat"; folder emoji on the Archived section; Enter-to-send/Shift+Enter-for-newline in the message box.
