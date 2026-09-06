# Deployment Logs

This folder stores deployment logs as separate `.txt` files.

## Available logs

- `2026-02-28-projectparadigm.txt` — Deployment summary for the ProjectParadigm update.
- `2026-07-26-chat-persistence.txt` — Chat history persistence + named conversations, plus next-roadmap todos (PDF handling, penal code reference, contextual advice).
- `2026-08-30-fix-chat-mobile-layout.txt` — Chat page made usable below 700px: the fixed 260px sidebar now stacks into a scrollable conversation strip. Desktop unchanged.
- `2026-09-06-fix-ra81-template-path.txt` — Fixed FormService's PDF template path (was "templates/RA-81.pdf", the real file is "templates/ra-81-fillable.pdf"), which would have crashed the intake-completion PDF download in production.
- `2026-09-06-minimalist-redesign.txt` — Chat page moved from a dark ChatGPT-styled theme to a minimalist, single-accent-color design shared with the login/signup redesign; light/dark/system theme support; NYC HPD / NY State HCR reference links; new Settings page (appearance, account email/password change, chat-history download); civic-blue palette; reconciled with main's intake/PDF-download flow.
