# Personal Assistant (Lola) — Architecture & Plan

## Overview

AI personal assistant that monitors Gmail + Google Chat, drafts responses, tracks promises ("what I promised to do"), and sends twice-daily summaries via Telegram. Runs on Google Cloud Run, free tier.

## Architecture

```
Cloud Scheduler (08:00 / 20:00 UTC+2)  →  single job: 0 6,18 * * * UTC
       │
       ▼
Cloud Run Service (FastAPI) ─── POST /run
       │
       ├── Gmail API → fetch up to 20 recent emails → LLM analyzes → creates drafts
       ├── Chat API  → list messages from all spaces → LLM analyzes
       ├── Firestore → stores drafts, promises, accounts, state
       └── Telegram Bot API → sends daily digest

Interactive (any time):
  Telegram message → POST /webhook → parse command → respond

Cloud Run URL: https://personal-assistant-mxv6y6olsq-uc.a.run.app
```

## Key Decisions

| Decision | Choice |
|----------|--------|
| Deployment | Cloud Run service (scales to zero, min 0, max 1) |
| LLM | DeepSeek V4 Flash via `opencode.ai/zen/go/v1` (URL configurable via `DEEPSEEK_API_URL`) |
| LLM tokens | `max_tokens: 8000` — required because the endpoint uses a reasoning model |
| State store | Firestore (free tier: 1 GiB, 50K reads/day) |
| Secrets | Plain env vars (no Secret Manager) — set via Cloud Build substitutions |
| Notifications | Telegram bot [@lolavkbot](https://t.me/lolavkbot) |
| Gmail integration | Direct API — fetch max 20 emails, body truncated at 2000 chars, create drafts, never auto-send |
| Chat integration | Google Chat API — read-only |
| Email signature | "Lola, Vladimir's AI Assistant" |
| Accounts | Multi-account ready (Firestore `accounts/{email}` doc per account) |
| OAuth | Desktop OAuth flow → refresh token saved directly to Firestore by `scripts/auth.py` |
| Region | us-central1 |

## Schedule

- One Cloud Scheduler job: `lola-run`, cron `0 6,18 * * *` UTC → fires at 08:00 and 20:00 UTC+2
- Calls `POST /run` on the Cloud Run service

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Register chat ID, show help |
| `/status` | Show pending drafts, open promises, last check time |
| `/drafts` | List all pending email drafts with index numbers |
| `/rewrite N <context>` | Update draft #N with new info via LLM |
| `/raw <text>` | Analyze arbitrary text for action items/promises |
| `/chat <question>` | Ask Lola about your current state |
| `/help` | Show all commands |

## Data Flow: Scheduled Run

1. Read `state/global` from Firestore (last run timestamp)
2. For each enabled account in Firestore:
   - **Gmail**: fetch up to 20 emails since last run → LLM analyzes → creates Gmail drafts → saves draft + promise metadata to Firestore
   - **Chat**: list spaces → fetch messages since last run → LLM analyzes → saves missed questions + promises
   - Each account is wrapped in try/except — one failure doesn't abort other accounts
3. LLM composes a unified Telegram summary
4. Send summary to configured Telegram chat ID
5. Update `state/global.last_run`

## Data Flow: Interactive Commands

### `/rewrite N <context>`
1. Fetch pending drafts from Firestore
2. Get draft #N, call LLM with draft body + new context
3. Update draft in Gmail
4. Update draft in Firestore
5. Confirm to user

### `/chat <question>`
1. Fetch current state (drafts, promises, last run)
2. Call LLM with question + context
3. Reply with answer

## Firestore Collections

### `accounts/{email}`
- email, refresh_token, providers=["gmail","chat"], enabled=true

### `drafts/{uuid}`
- account, to, subject, body, thread_id, original_email_id, created_at, status (pending/reviewed/sent/discarded)

### `promises/{uuid}`
- text, source (email/chat), source_detail, created_at, status (open/done)

### `state/global`
- last_run (datetime)

### `settings/global`
- telegram_chat_id (int), timezone (str, default "UTC")

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
| LLM via opencode.ai | ~$0.14/month | Estimate at current usage |
| Telegram Bot API | Free | — |
| **Total** | **~$0.14/month** | |

## Deployment Steps (first time)

1. Create Telegram bot via @BotFather → get token
2. Create GCP project, enable APIs:
   ```
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
     cloudscheduler.googleapis.com firestore.googleapis.com \
     artifactregistry.googleapis.com gmail.googleapis.com chat.googleapis.com
   ```
3. Create Firestore database (Native mode, us-central1)
4. Create Artifact Registry repo: `assistant` in us-central1
5. Create OAuth Desktop credentials in GCP Console → download JSON → save as `credentials.json`
6. Fill in `.env` (copy from `.env.example`)
7. `make auth` → browser OAuth → saves refresh token to Firestore automatically
8. `make deploy` → builds container via Cloud Build → deploys to Cloud Run
9. `make setup-scheduler` → points `lola-run` job at the Cloud Run URL
10. `make setup-telegram` → sets Telegram webhook
11. Send `/start` to the bot

## Redeployment

```bash
# Code change:
make deploy

# Env var only change (no rebuild needed):
gcloud run services update personal-assistant \
  --region=us-central1 \
  --update-env-vars=KEY=value \
  --project=personal-assistant-lola

# Add a second Google account (no redeploy needed):
make auth
```

## Switching LLM Provider

Update two lines in `.env` and redeploy:
```
DEEPSEEK_API_KEY=<new-key>
DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions
```

## Adding a Second Account

1. `make auth` — sign in with second Google account, auto-saved to Firestore
2. No redeploy needed

## File Structure

```
src/
├── main.py            # FastAPI app, routes
├── config.py          # Pydantic Settings (incl. deepseek_api_url)
├── models.py          # Pydantic models
├── gmail_client.py    # Gmail API (fetch max 20, create drafts)
├── chat_client.py     # Google Chat API (list spaces, messages)
├── llm_client.py      # LLM client (configurable URL, max_tokens=8000)
├── telegram_client.py # Telegram Bot API
├── state.py           # Firestore CRUD
└── prompts.py         # LLM prompt templates
scripts/auth.py        # One-time OAuth flow → saves account to Firestore
.env                   # Local secrets (not committed)
.env.example           # Template
.gcloudignore          # Excludes .venv, .env, credentials from Cloud Build uploads
Dockerfile             # python:3.12-slim, uvicorn on port 8080
cloudbuild.yaml        # Build + push + deploy pipeline
Makefile               # auth, deploy, setup-scheduler, setup-telegram
```
