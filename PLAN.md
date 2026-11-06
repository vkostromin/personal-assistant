# Personal Assistant (Lola) — Architecture & Plan

## Overview

AI personal assistant that monitors Gmail + Google Chat, drafts responses, tracks promises ("what I promised to do"), and sends twice-daily summaries via Telegram. Runs on Google Cloud Run, free tier.

Multi-user: one Cloud Run service + one Telegram bot serve many users. Each user links their own Google
account(s) from Telegram (web OAuth flow) and gets an independent digest, drafts and promises.

## Architecture

```
Cloud Scheduler (08:00 / 20:00 UTC+2)  →  single job: 0 6,18 * * * UTC
       │
       ▼
Cloud Run Service (FastAPI) ─── POST /run
       │
       └── for every active user (3 in parallel):
             for each of the user's Google accounts:
               ├── Gmail API → fetch up to 20 emails since account watermark → LLM → drafts
               └── Chat API  → messages from all spaces since watermark → LLM
             └── Telegram Bot API → that user's own digest to that user's chat

Interactive (any time):
  Telegram message → POST /webhook → user lookup (users/{telegram id}) → command → respond

Account linking:
  /connect → button → GET /oauth/start (confirmation page) → Google consent
           → GET /oauth/callback → code exchange (PKCE) → email from ID token → accounts/{email}

Cloud Run URL: see SERVICE_URL in .env
```

## Key Decisions

| Decision | Choice |
|----------|--------|
| Deployment | Cloud Run service (scales to zero, min 0, max 3, concurrency 10, timeout 300s) |
| LLM | Shared: first configured of Gemini (`gemini-2.5-flash-lite`) → DeepSeek → OpenAI; per-user own key via `/setkey` |
| LLM limits | Shared key, non-admin: 100 emails + 150k tokens per 12h window (reset each run); over the token limit drafts, /rewrite, /chat, /raw pause |
| LLM calls | OpenAI-compatible chat API for all providers; emails analysed in batches of 20; `max_tokens` 8000 |
| State store | Firestore (free tier: 1 GiB, 50K reads/day) |
| Secrets | Plain env vars (no Secret Manager) — set via Cloud Build substitutions |
| Notifications | Telegram bot (token in `.env`) |
| Gmail integration | Direct API — fetch max 20 emails, body truncated at 2000 chars, create drafts, never auto-send |
| Chat integration | Google Chat API — read-only |
| Email signature | Configurable via `SIGNATURE` (default "Lola, Your AI Assistant") |
| Users | `users/{telegram_user_id}`; first `/start` on a fresh deployment = admin, others need a single-use invite |
| Identity | Telegram `from.id`; private chats only |
| Accounts | `accounts/{email}` owned by exactly one user (`user_id`); a user can link several |
| OAuth | Web-server flow from Telegram (`/connect`), PKCE, single-use 15-min state, email from ID token |
| Region | us-central1 |

## Schedule

- One Cloud Scheduler job: `lola-run`, cron `0 6,18 * * *` UTC → fires at 08:00 and 20:00 UTC+2
- Calls `POST /run` on the Cloud Run service

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
| `/usage` | user | AI provider, usage in the current window, reset time |
| `/setkey <provider> <key> [model]` | user | Own Gemini/DeepSeek/OpenAI key (validated, encrypted, message deleted) → no limits |
| `/removekey` | user | Back to the shared key |
| `/help` | user | Show all commands |
| `/invite [note]` | admin | Create single-use invite link `t.me/<bot>?start=<code>` (72h) |
| `/invites`, `/revoke <code>` | admin | List / cancel active invites |
| `/users` | admin | List users |
| `/disable <id>`, `/enable <id>` | admin | Block / unblock a user (data kept) |
| `/remove <id>` | admin | Delete user + accounts (tokens revoked) + drafts + promises |

## Data Flow: Scheduled Run

1. List active users (`users` where status=active; optional `?shard=i&shards=n`)
2. For each user (bounded parallelism, one failure never affects others):
   - Start a new usage window; pick the LLM (own key or shared) and the limits
   - For each of the user's enabled accounts (skip `needs_reauth`):
     - **Gmail**: newest emails since `provider_last_run.gmail` (new account: last 12h), up to the email cap →
       LLM in batches of 20 → Gmail drafts (paused once the token budget is spent) → save with `user_id`
     - **Chat**: spaces active since `provider_last_run.chat` (10 most recent) → LLM → promises / missed questions
     - No new AI work after the 240 s run budget; skipped items are reported
     - Watermark advanced per provider only on success (a Chat failure doesn't re-draft old email)
     - `invalid_grant` on token refresh → `needs_reauth=true`, user told to `/connect`
   - LLM composes the user's digest (plain fallback if the LLM fails) + notes (skipped emails, paused drafts, key problems) → user's chat
   - Tokens and emails recorded in `usage/{user_id}`

### `/rewrite N <context>`
LLM rewrites draft N → Gmail `drafts.update` on the stored `gmail_draft_id` (same draft, no duplicates);
if the draft is gone from Gmail (sent/deleted) or never existed, a new one is created and its id stored.

## Firestore Collections

### `users/{telegram_user_id}`
- chat_id, name, username, role (admin/user), status (active/disabled), invited_by, created_at

### `accounts/{email}`
- user_id, refresh_token (Fernet-encrypted), providers, enabled, needs_reauth, last_run, provider_last_run{gmail,chat}

### `drafts/{uuid}`
- user_id, account, to, subject, body, thread_id, original_email_id, gmail_draft_id, created_at, status

### `promises/{uuid}`
- user_id, account, text, source (email/chat), source_detail, created_at, status (open/done)

### `invites/{code}`
- created_by, created_at, expires_at, note, used_by, used_at

### `oauth_states/{state}`
- user_id, code_verifier, redirect_uri, expires_at (optional Firestore TTL on `expires_at`)

### `usage/{telegram_user_id}`
- window_start, tokens, emails

### `llm_keys/{telegram_user_id}`
- provider, api_key (Fernet-encrypted), model, updated_at

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
| LLM, Gemini 2.5 Flash-Lite (shared key) | ≤ ~$0.03 per user per 12h window at the limit | ≈ $0.10 / 1M input, $0.40 / 1M output tokens (verify current pricing); typical use is far below the limit |
| LLM, own keys | $0 for the operator | billed to the user's own provider account |
| Telegram Bot API | Free | — |
| **Total** | **≈ $0.3–2 per active user/month** | dominated by the shared LLM key |

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

The shared key is the first one set of `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`
(models: `GEMINI_MODEL`, `DEEPSEEK_MODEL` + `DEEPSEEK_API_URL`, `OPENAI_MODEL`). Set/unset in `.env`
and `make deploy`. Users can override with their own key (`/setkey`).

## Adding Users / Accounts

- Another Google account for yourself: `/connect` in Telegram
- Another person: `/invite` → send them the link → they `/start` and `/connect`

## File Structure

```
src/
├── main.py            # FastAPI app: /webhook, /run, /oauth/start, /oauth/callback, startup migration
├── bot.py             # Telegram commands: registration, invites, admin, assistant
├── runner.py          # Scheduled per-user processing
├── quota.py           # LLM selection (own key / shared) and per-user limits
├── oauth.py           # Google web OAuth (PKCE, ID token, revoke)
├── config.py          # Pydantic Settings (incl. deepseek_api_url)
├── models.py          # Pydantic models
├── gmail_client.py    # Gmail API (fetch max 20, create drafts)
├── chat_client.py     # Google Chat API (list spaces, messages)
├── llm_client.py      # LLM providers (Gemini/DeepSeek/OpenAI), token accounting, prompts calls
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
