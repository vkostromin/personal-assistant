# Personal Assistant (Lola)

AI personal assistant that monitors Gmail + Google Chat, drafts responses, tracks promises, and sends twice-daily summaries via Telegram.

**Multi-user:** one Cloud Run service and one Telegram bot serve many people. Every user links their
own Google account(s) from Telegram and gets their own summaries, drafts and promises — fully
independent of the admin's account. The first person to talk to a fresh deployment becomes the admin;
everybody else joins with an invitation link.

## Architecture

Cloud Run service + Cloud Scheduler (2x daily) + interactive Telegram bot. See [PLAN.md](PLAN.md).

## Setup

### 1. Create Telegram bot
- Open @BotFather on Telegram
- `/newbot` → name: `Lola` → username: `your_lola_bot`
- Copy the bot token

### 2. Create GCP project
```bash
gcloud projects create personal-assistant --name="Personal Assistant"
gcloud config set project personal-assistant
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com \
  firestore.googleapis.com artifactregistry.googleapis.com \
  gmail.googleapis.com chat.googleapis.com
```
Create a Firestore database (Native mode) and an Artifact Registry repo `assistant` in your region.

### 3. Configure the OAuth consent screen
GCP Console → APIs & Services → OAuth consent screen (Google Auth Platform):
- Add scopes: `openid`, `.../auth/userinfo.email`, `.../auth/gmail.modify`,
  `.../auth/chat.spaces.readonly`, `.../auth/chat.messages.readonly`.
- **Audience** — pick what fits your users (see [Google account limits](#google-account-limits)):
  - *Internal*: all users are in your Google Workspace organisation. No verification, no expiry.
  - *External + Testing*: add every user's Gmail address under **Test users** (max 100).

### 4. Get an AI key
Set **one** of `GEMINI_API_KEY` (recommended), `DEEPSEEK_API_KEY`, `OPENAI_API_KEY` in `.env` —
the first one set (in that order) is used for everybody without their own key.
See [AI providers and limits](#ai-providers-and-limits).

### 5. Deploy Cloud Run (first time)
```bash
cp .env.example .env   # fill in the values; PUBLIC_URL can stay empty for now
make deploy
gcloud run services describe personal-assistant --region=us-central1 --format='value(status.url)'
```

### 6. Create a **Web application** OAuth client
GCP Console → APIs & Services → Credentials → Create OAuth client ID → **Web application**
- Authorized redirect URI: `https://<your-service-url>/oauth/callback`
- Put the client ID/secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
- Set `PUBLIC_URL=https://<your-service-url>` in `.env`

> The old *Desktop app* client can't be used: desktop clients only redirect to `localhost`.

```bash
make deploy   # redeploy with the new client + PUBLIC_URL
```

### 7. Set Telegram webhook and scheduler
```bash
make setup-telegram
gcloud scheduler jobs create http lola-run --location=us-central1 \
  --schedule="0 8,20 * * *" --time-zone="Asia/Jakarta" \
  --uri="$(gcloud run services describe personal-assistant --region=us-central1 --format='value(status.url)')/run" \
  --http-method=POST --headers=X-Run-Secret=$RUN_SECRET --attempt-deadline=300s
make setup-firestore-ttl   # optional: auto-clean expired sign-in links
```

### 8. Become the admin
Open the bot in Telegram and send `/start`. On a fresh deployment **the first user becomes the admin**
(set `ADMIN_TELEGRAM_ID` to restrict this to your Telegram user id). Tap **Connect Google account**,
confirm, sign in with Google — done.

## Users

| Who | How |
|-----|-----|
| Admin | First `/start` on a fresh deployment |
| New user | Admin sends `/invite [note]` → gets `https://t.me/<bot>?start=<code>` → sends it to the person → they open it, press **Start**, tap **Connect Google account** |
| More Google accounts | Any user: `/connect` (each Google account belongs to exactly one user) |

Invitations are single-use and expire after `INVITE_TTL_HOURS` (72h). Sign-in links are personal,
single-use and expire after 15 minutes; the sign-in page shows which Telegram user the Google account
will be linked to, to protect people from "please click this link" phishing.

### Commands
| Everyone | Admin only |
|----------|------------|
| `/status`, `/drafts`, `/rewrite N <context>` (updates the Gmail draft in place), `/raw <text>`, `/chat <question>` | `/invite [note]`, `/invites`, `/revoke <code>` |
| `/connect`, `/accounts`, `/disconnect <email>` (revokes Google access) | `/users`, `/disable <id>`, `/enable <id>` |
| `/usage` — AI provider, usage and limits | `/remove <id>` — deletes the user, their accounts (tokens revoked), drafts, promises, AI key |
| `/setkey <provider> <key> [model]`, `/removekey` — own AI key, no limits | |
| `/help` | |

If a user's Google access is revoked or expires, the next summary tells them to `/connect` again.

## AI providers and limits

### Shared key
The server uses the **first** provider that has a key: `GEMINI_API_KEY` → `DEEPSEEK_API_KEY` →
`OPENAI_API_KEY`. All are called through their OpenAI-compatible chat API.

Recommended: **Gemini 2.5 Flash-Lite** (`gemini-2.5-flash-lite`, the default) — the cheapest current
Gemini model, fast, no "thinking" by default, 1M context; good enough for triage, promises and short
replies. If drafts feel too plain, set `GEMINI_MODEL=gemini-2.5-flash` (smarter, roughly 3–6× the
price, slower). The same models are on Vertex AI at the same price; the Gemini API key route is
simply the least setup. Check current prices at ai.google.dev/pricing.

> ⚠️ Enable billing on the Google project that owns the Gemini key. On the free tier Google may use
> prompts and responses (here: your users' emails) to improve its products; on the paid tier it doesn't.

### Per-user limits (shared key)
A usage window lasts 12 hours and restarts with each scheduled run.
| Limit | Default | What happens |
|-------|---------|--------------|
| `USER_EMAIL_LIMIT` | 100 emails / window | newest 100 are analysed; the summary says how many were skipped |
| `USER_TOKEN_LIMIT` | 150 000 tokens / window | ~100 emails with drafts + chat + summary take ~60–90k; the rest is for `/rewrite`, `/chat`, `/raw`. When used up, **draft replies and those commands pause until the next run**; summaries continue |

Admins aren't limited (`LIMIT_ADMINS=true` to change that). Users see their numbers with `/usage`.
Rough cost on Gemini 2.5 Flash-Lite: a fully used window is ≈ $0.03, so ≤ ~$2 per user per month at
the limit, typically far less.

### Own key
`/setkey gemini|deepseek|openai <key> [model]` — the bot deletes the message, tests the key, and
stores it encrypted. That user then has no token limit (the email cap becomes
`MAX_EMAILS_PER_RUN`=200 — a technical cap so a run fits the 300 s request timeout) and pays their
own provider. `/removekey` goes back to the shared key.

## Scaling

Cloud Run: max 3 instances, concurrency 10, 300 s timeout. A scheduled run processes 3 users in
parallel and stops starting new AI work after `RUN_TIME_BUDGET_SECONDS` (240 s); anything left is
reported in the summary. That comfortably fits a few dozen users per run. Beyond that, split users
over several scheduler jobs — each gets its own request/instance:
```bash
# 3 jobs: ...&shard=0, 1, 2 (users are split by user_id % shards)
--uri="$URL/run?shard=0&shards=3"
```

## Upgrading an existing single-user deployment

Deploy as usual — on startup the service migrates the data automatically (once):
- the Telegram chat that owned the bot (`settings/global.telegram_chat_id`) becomes the **admin**;
- existing accounts, drafts and promises are assigned to their owner (accounts added with
  `auth.py --chat-id` go to that chat as a regular user); the old global `last_run` is kept per account.

Then:
1. Create the Web OAuth client (step 5) and redeploy.
2. Refresh tokens are bound to the OAuth client that issued them, so tokens made by the old Desktop
   client stop working with the new client. **Send `/connect` and sign in again for each existing
   account.** Until you do, the summary shows a "🔑 … send /connect" reminder for it.
3. `scripts/auth.py` / `make auth` are gone — accounts are only linked via Telegram now.
4. Optionally switch the shared AI to Gemini: set `GEMINI_API_KEY` (it takes precedence over DeepSeek).

## Google account limits

`gmail.modify` is a *restricted* scope, which affects who can use the app:
- **Internal (Workspace org)** — simplest: unlimited org users, no verification, tokens don't expire.
- **External, Testing status** — up to 100 listed test users, but Google **expires refresh tokens after
  7 days**; users then get the reconnect reminder and must `/connect` again weekly.
- **External, In production** — anyone can sign in, but Google requires app verification plus a
  third-party security assessment for restricted scopes.

Google Chat API access only works for Google Workspace accounts; for personal Gmail accounts the Chat
part is skipped.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
PROJECT_ID=test python -m pytest          # or: make test
python -m uvicorn src.main:app --reload --port 8080
```
Tests run against an in-memory Firestore fake (`tests/fake_firestore.py`) — no GCP credentials needed.
