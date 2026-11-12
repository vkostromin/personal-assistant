# Personal Assistant (Lola) — Architecture & Plan

## Overview

AI personal assistant that monitors Gmail + Google Chat, drafts responses, tracks promises ("what I promised to do"), and sends twice-daily summaries via Telegram. Runs on Google Cloud Run, free tier.

Multi-user: one Cloud Run service + one Telegram bot serve many users. Each user links their own Google
account(s) from Telegram (web OAuth flow) and gets an independent digest, drafts and promises.

## Architecture

```
Cloud Scheduler  →  single job, hourly: 0 * * * * UTC
       │
       ▼
Cloud Run Service (FastAPI) ─── POST /run
       │
       └── for every active user whose local 08:00 / 20:00 slot is due (5 in parallel):
             sync drafts with Gmail (sent / deleted there → status updated)
             for each of the user's Google accounts (open items given to the LLM as context):
               ├── Gmail inbox → emails since watermark (≤100; first run: 1 week, ≤200; own key: no cap),
               │                 oldest first, batches of 20 → LLM → drafts + items + updates;
               │                 watermark (internalDate + ids at that ms) saved after every batch
               ├── Gmail sent  → same, separate watermark → my promises + waiting-for + updates
               └── Chat        → messages (mine marked "Me") → LLM → items + updates
             └── summary (LLM, led by the ranked Focus list) + Focus message with buttons
       └── reminders: items/drafts with next_remind_at <= now (not in quiet hours) → cards with buttons
       └── accounts with `backlog=true` (run stopped early) → continued quietly every hour, no summary
             └── Telegram Bot API → that user's own digest to that user's chat

Interactive (any time):
  Telegram message → POST /webhook → user lookup (users/{telegram id}) → command → respond
  Plain / forwarded text → pending answer (rewrite instructions, deadline) or [Add as task] [Ask Lola]
  Button press (callback_query) → ownership check → action (actions.py) → card edited in place

Account linking:
  /connect → button → GET /oauth/start (confirmation page) → Google consent
           → GET /oauth/callback → code exchange (PKCE) → email from ID token → accounts/{email}

Cloud Run URL: see SERVICE_URL in .env
```

## Key Decisions

| Decision | Choice |
|----------|--------|
| Deployment | Cloud Run service (scales to zero, min 0, max 3, concurrency 10, timeout 300s) |
| LLM | First configured of `LLM_PROVIDER_ORDER` (gemini → deepseek → openai); recommended Gemini 2.5 Flash-Lite. All via OpenAI-compatible chat completions |
| LLM limits | Shared key: `USER_TOKEN_LIMIT` tokens per user per 12h (default 150k ≈ 100 emails), window restarts at each run; admins & own keys unlimited |
| Own keys | `/setkey provider key [model]` — validated, encrypted in `llm_keys/{user}`, message deleted |
| LLM tokens | `max_tokens: 8000` (headroom for reasoning models) |
| State store | Firestore (free tier: 1 GiB, 50K reads/day) |
| Secrets | Plain env vars (no Secret Manager) — set via Cloud Build substitutions |
| Notifications | Telegram bot (token in `.env`) |
| Gmail integration | Direct API — fetch max 20 emails, body truncated at 2000 chars, create drafts, never auto-send |
| Chat integration | Google Chat API — read-only |
| Email signature | Per user via `/signature`; server default `SIGNATURE` ("Lola, Your AI Assistant") |
| Users | `users/{telegram_user_id}`; first `/start` on a fresh deployment = admin, others need a single-use invite |
| Identity | Telegram `from.id`; private chats only |
| Accounts | `accounts/{email}` owned by exactly one user (`user_id`); a user can link several |
| OAuth | Web-server flow from Telegram (`/connect`), PKCE, single-use 15-min state, email from ID token |
| Region | us-central1 |

## Schedule

- One Cloud Scheduler job: `lola-run`, cron `0 * * * *` UTC, attempt deadline 300s → `POST /run`
- Each user has a timezone (`/timezone`, default `DEFAULT_TIMEZONE`); summaries at `SUMMARY_HOURS`
  (08:00, 20:00) local time. A user is due when the latest local slot > `users.last_run_slot`
  (claimed atomically, so overlapping runs can't double-process). Missed slots are caught up at the next
  hourly run; users who never ran are picked up within 3h of a slot. `/run?force=true` runs everyone.

## Telegram Bot Commands

| Command | Who | Description |
|---------|-----|-------------|
| `/start [invite]` | anyone | First user → admin; otherwise redeem invite; registered → help |
| `/connect` | user | Link a Google account (personal single-use sign-in link) |
| `/accounts` | user | Linked accounts, providers, last check, reconnect status |
| `/disconnect <email>` | user | Revoke + unlink an account, delete its drafts/promises |
| `/status` | user | Pending drafts, open promises, last check time |
| `/drafts` | user | List own pending email drafts with index numbers |
| `/rewrite N <context>` | user | Update own draft #N with new info via LLM |
| `/raw <text>` | user | Analyze arbitrary text for action items/promises |
| `/chat <question>` | user | Ask Lola about your current state |
| `/help` | user | Show all commands |
| `/draft N` | user | Full text of draft N |
| `/sent N…` | user | Mark draft(s) as sent (`1 3`, `2-4`, `all`) |
| `/deletedraft N…` | user | Delete draft(s) in Lola and in Gmail |
| `/unlink [email] <gmail\|chat>` | user | Stop using Gmail or Chat of an account |
| `/timezone [zone]` | user | Show / set timezone (IANA, city, or `+7`) |
| `/deleteme [YES]` | user | Delete own account and all data (admin must transfer first) |
| `/llm` | user | Provider in use, usage / limit |
| `/setkey <provider> <key> [model]` | user | Use own gemini/deepseek/openai key without limits |
| `/removekey` | user | Back to the shared provider |
| `/invite [note]` | admin | Create single-use invite link `t.me/<bot>?start=<code>` (72h) |
| `/invites`, `/revoke <code>` | admin | List / cancel active invites |
| `/users` | admin | List users |
| `/disable <id>`, `/enable <id>` | admin | Block / unblock a user (data kept) |
| `/remove <id>` | admin | Delete user + accounts (tokens revoked) + drafts + promises |
| `/setlimit <id> <tokens\|default\|unlimited>` | admin | Per-user token limit override |
| `/transferadmin <id>` | admin | Hand the admin role to another user |
| `/who [name]` | user | Everything open with a person (or list of people) |
| `/review` | user | Decide on old / forgotten items one by one |

## Data Flow: Scheduled Run

1. List active users (`users` where status=active)
2. For each user (bounded parallelism, one failure never affects others; no new users/accounts are
   started after the 240s soft deadline — they're processed next run):
   - Restart the user's 12h usage window; pick own key or the shared provider
   - For each of the user's enabled accounts (skip `needs_reauth`):
     - **Gmail**: fetch since `provider_last_run.gmail` (new account: last 12h) → LLM → Gmail drafts → save with `user_id`
     - **Chat**: list spaces → fetch since `provider_last_run.chat` → LLM → promises / missed questions
     - Watermark advanced per provider only on success (a Chat failure doesn't re-draft old email)
     - `invalid_grant` on token refresh → `needs_reauth=true`, user told to `/connect`
   - Limit reached mid-run → remaining analysis/drafting skipped, watermark not advanced (picked up next run)
   - LLM composes the user's digest (always allowed; plain-text fallback if the LLM fails) → user's chat

## Items (promises / waiting-for / replies / to-dos)

`items/{id}`: user_id, kind (promise|waiting|reply|todo), title, counterparty, due_at, priority, status
(open|done|cancelled), source (email|sent|chat|manual), account, thread_id, in_reply_to, subject, space, link,
snoozed_until, next_remind_at, remind_stage, last_reminded_at, fingerprint, resolution, created_at, closed_at.

- Extraction: LLM gets local "now" + the open items (id | kind | title | who | due), returns new items and
  `updates` (done / cancelled / new due) — updates are only applied to ids it was shown and the user owns.
  New items are deduplicated by fingerprint (kind + normalized title + counterparty).
- Waiting items without a deadline get `WAITING_DEFAULT_HOURS` (48h).
- Reminder policy (items.py): promise/todo/reply — heads-up 2h before due → at due → daily 09:00 while
  overdue (3×); waiting — at due → daily 09:00 (3×); snooze → once at snooze end; no due → no reminders.
  Times shifted out of quiet hours; at most 5 cards per user per hour. Query: `next_remind_at <= now`
  (single-field index), closed items get `next_remind_at = null`.
- Focus score: priority (40/20/5) + kind (reply/promise 15, todo 10) + due (overdue 60+, <24h 45, <72h 20)
  + age (≤10); drafts +20; waiting only when overdue; snoozed excluded.

## Buttons (callback data `<d|i|t>:<action>:<id>[:<arg>]`)
- item: done, cancel, snz[:2|4|8|tm], due[:today|tm|3d|mon|none|other], fu (follow-up draft), open, back,
  crep (Chat reply: next text = reply) → preview → csend (post as the user in the thread) / cx; csug (LLM drafts it)
- review (`r`): done, cancel, again (restart reminders), due[:choice], skip, stop, back — then next old item
- draft: send → sendok (confirmation), rw (rewrite; next text = instructions), snz, del, sent, open, back
- text: add (LLM → task), ask (answer question) for the last plain text the user sent
- Every press checks that the record belongs to the presser; pending free-text actions expire after 15 min.

## Draft lifecycle

`pending` → `sent` | `discarded` (or deleted entirely).
- Created by the run as a Gmail draft (threadId + In-Reply-To/References from the real email).
- Sync (each run + `/drafts` + before `/rewrite`): `drafts.list` per account; a tracked draft missing in
  Gmail is `sent` if the thread has a SENT message newer than the draft (no thread: `in:sent` search by
  recipient + subject), else `discarded`.
- Manual: `/sent` → `sent` (Gmail untouched); `/deletedraft` → Gmail `drafts.delete` + record deleted.

## Data Flow: `/rewrite N <context>`
0. Sync this draft with Gmail — if it was sent/deleted there, update status and stop
1. Check usage limit → LLM rewrites draft N
2. Gmail `drafts.update` on the stored `gmail_draft_id` (new draft only if it was sent/deleted in Gmail)
3. Firestore draft updated (text + `gmail_draft_id`) only after Gmail succeeded

## Firestore Collections

### `users/{telegram_user_id}`
- chat_id, name, username, role (admin/user), status (active/disabled), invited_by, token_limit (null=default, 0=unlimited), timezone, last_run_slot, created_at

### `accounts/{email}`
- user_id, refresh_token (Fernet-encrypted), providers, enabled, needs_reauth, last_run, provider_last_run{gmail,chat}

### `drafts/{uuid}`
- user_id, account, to, subject, body, thread_id, original_email_id, in_reply_to, gmail_draft_id, created_at, status (pending/sent/discarded), status_changed_at, status_source (gmail/user)

### `promises/{uuid}`
- user_id, account, text, source (email/chat), source_detail, created_at, status (open/done)

### `invites/{code}`
- created_by, created_at, expires_at, note, used_by, used_at

### `oauth_states/{state}`
- user_id, code_verifier, redirect_uri, expires_at (optional Firestore TTL on `expires_at`)

### `usage/{telegram_user_id}`
- window_start, tokens (shared key), own_tokens (own key, informational)

### `llm_keys/{telegram_user_id}`
- provider, model, api_key (Fernet-encrypted), created_at

### `settings/app`
- admin_user_id, schema_version

### Legacy (read once by the startup migration)
- `settings/global.telegram_chat_id` → becomes admin; `state/global.last_run` → per-account last_run

## LLM Prompts (5 templates in `src/prompts.py`)

1. **analyze_emails** — Given emails, identify action items, promises, and draft replies
2. **analyze_chat** — Given chat messages, find missed questions and promises
3. **rewrite_draft** — Rewrite an existing draft with new context
4. **answer_question** — Answer user questions about current state
5. **compose_summary** — Compile daily digest in Telegram Markdown

## Cost Estimate

| Service | Cost | Notes |
|---------|------|-------|
| Cloud Run (2 runs/day + Telegram) | ~$0 | Free tier: 2M requests/month |
| Cloud Scheduler (1 job) | ~$0 | Free tier: 3 jobs/month |
| Firestore (~100 reads/day) | ~$0 | Free tier: 50K reads/day |
| Artifact Registry (1 image ~200 MB) | ~$0 | Free tier: 0.5 GB |
| LLM, Gemini 2.5 Flash-Lite, shared key | ≤ ~$1.8/user/month | Worst case: every user uses the full 150k-token limit twice a day; typical use is far lower |
| LLM, users with own key | $0 for the admin | Paid by the user |
| Telegram Bot API | Free | — |
| **Total** | **≈ LLM cost** | Infrastructure stays in free tiers for a small group |

## Deployment Steps (first time)

See README.md → Setup. Summary:
1. Telegram bot via @BotFather; GCP project + APIs; Firestore; Artifact Registry `assistant`
2. OAuth consent screen (Internal, or External + test users)
3. `make deploy` → get the service URL
4. Create **Web application** OAuth client with redirect `<URL>/oauth/callback`; set `GOOGLE_CLIENT_*` and `PUBLIC_URL`; `make deploy`
5. `make setup-telegram`, create scheduler job (`--attempt-deadline=300s`), optional `make setup-firestore-ttl`
6. Send `/start` to the bot → you are the admin → connect your Google account → `/invite` others

## Redeployment

```bash
make deploy
# Env var only change:
gcloud run services update personal-assistant --region=us-central1 --update-env-vars=KEY=value --project=$PROJECT_ID
```

## Switching LLM Provider

Set the key of the provider you want (`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`) and redeploy.
The first provider in `LLM_PROVIDER_ORDER` with a key is used, e.g. to move from DeepSeek to Gemini
just add `GEMINI_API_KEY` (Gemini comes first by default).

## Adding Users / Accounts

- Another Google account for yourself: `/connect` in Telegram
- Another person: `/invite` → send them the link → they `/start` and `/connect`

## File Structure

```
src/
├── main.py            # FastAPI app: /webhook, /run, /oauth/start, /oauth/callback, startup migration
├── bot.py             # Telegram commands: registration, invites, admin, assistant
├── runner.py          # Scheduled per-user processing (due users only)
├── schedule.py        # Timezones, 08:00/20:00 slots, due logic
├── items.py           # Reminder policy, quiet hours, due parsing, focus ranking
├── actions.py         # Item/draft state changes (done, snooze, due, send, rewrite, follow-up)
├── cards.py           # Telegram cards + inline keyboards
├── callbacks.py       # Button presses, free-text follow-ups
├── reminders.py       # Hourly reminder dispatch
├── drafts_sync.py     # Draft status sync with Gmail
├── oauth.py           # Google web OAuth (PKCE, ID token, revoke)
├── config.py          # Pydantic Settings (incl. deepseek_api_url)
├── models.py          # Pydantic models
├── gmail_client.py    # Gmail API (fetch max 20, create drafts)
├── chat_client.py     # Google Chat API (list spaces, messages)
├── llm_client.py      # OpenAI-compatible LLM client + prompts usage (LLM object with quota hooks)
├── providers.py       # Provider registry, server provider selection
├── quota.py           # Per-user limits, own keys, usage windows
├── telegram_client.py # Telegram Bot API
├── state.py           # Firestore store (users, accounts, invites, oauth states, migration)
└── prompts.py         # LLM prompt templates
tests/                 # pytest, in-memory Firestore fake (no GCP needed)
.env                   # Local secrets (not committed)
.env.example           # Template
.gcloudignore          # Excludes .venv, .env, credentials from Cloud Build uploads
Dockerfile             # python:3.12-slim, uvicorn on port 8080
cloudbuild.yaml        # Build + push + deploy pipeline
Makefile               # deploy, setup-scheduler, setup-telegram, setup-firestore-ttl, test
```
