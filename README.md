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

### 4. Deploy Cloud Run (first time)
```bash
cp .env.example .env   # fill in the values; PUBLIC_URL can stay empty for now
make deploy
gcloud run services describe personal-assistant --region=us-central1 --format='value(status.url)'
```

### 5. Create a **Web application** OAuth client
GCP Console → APIs & Services → Credentials → Create OAuth client ID → **Web application**
- Authorized redirect URI: `https://<your-service-url>/oauth/callback`
- Put the client ID/secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
- Set `PUBLIC_URL=https://<your-service-url>` in `.env`

> The old *Desktop app* client can't be used: desktop clients only redirect to `localhost`.

```bash
make deploy   # redeploy with the new client + PUBLIC_URL
```

### 6. Set Telegram webhook and scheduler
```bash
make setup-telegram
gcloud scheduler jobs create http lola-run --location=us-central1 \
  --schedule="0 8,20 * * *" --time-zone="Asia/Jakarta" \
  --uri="$(gcloud run services describe personal-assistant --region=us-central1 --format='value(status.url)')/run" \
  --http-method=POST --headers=X-Run-Secret=$RUN_SECRET --attempt-deadline=300s
make setup-firestore-ttl   # optional: auto-clean expired sign-in links
```

### 7. Become the admin
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
| `/status`, `/drafts`, `/rewrite N <context>`, `/raw <text>`, `/chat <question>` | `/invite [note]`, `/invites`, `/revoke <code>` |
| `/connect`, `/accounts`, `/disconnect <email>` (revokes Google access) | `/users`, `/disable <id>`, `/enable <id>` |
| `/llm`, `/setkey <provider> <key> [model]`, `/removekey` | `/remove <id>` — deletes the user, their accounts (tokens revoked), drafts and promises |
| `/help` | `/setlimit <id> <tokens\|default\|unlimited>` — e.g. `/setlimit 12345 300k` |

`/rewrite` edits the existing Gmail draft in place (it only creates a new one if the original was
sent or deleted in Gmail).

If a user's Google access is revoked or expires, the next summary tells them to `/connect` again.

## AI providers, limits and own keys

### Shared (server) provider
The server uses the **first provider in `LLM_PROVIDER_ORDER` (default `gemini,deepseek,openai`) whose
key is set**: `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` (+ `DEEPSEEK_API_URL`, `DEEPSEEK_MODEL`),
`OPENAI_API_KEY`. Models: `GEMINI_MODEL` (default `gemini-2.5-flash-lite`), `OPENAI_MODEL`.

Recommended: **Gemini 2.5 Flash-Lite** — the cheapest current Gemini model (≈ $0.10 / 1M input,
$0.40 / 1M output tokens; check current pricing), fast, no "thinking" by default, and good enough for
triage, summaries and short replies. If draft quality isn't good enough, switch to `gemini-2.5-flash`
(≈ 3× input / 6× output price; it "thinks" by default, so it's slower and uses more tokens).

Create the key in Google AI Studio (aistudio.google.com/apikey) **in a project with billing enabled** —
on the free tier Google may use prompts (i.e. your users' emails) to improve its products; on the paid
tier it doesn't.

### Per-user limit
Users of the shared key get `USER_TOKEN_LIMIT` tokens (prompt + completion, default **150 000**) per
12-hour period. The period restarts at every scheduled run (and at the latest after 12h).
Sizing: an email costs ≈ 300–600 prompt tokens (body truncated to 1 000 chars; non-Latin scripts
cost more) plus ≈ 100–300 tokens of draft — ~100 emails ≈ 60–90k tokens; chat spaces, the digest and a
few `/chat` or `/rewrite` calls fit in the rest. Worst case with Flash-Lite ≈ $0.03 per user per 12h.

When the limit is used up:
- during the run: remaining mail/chat isn't analyzed and no drafts are written; it is **not lost** — it's
  picked up at the next run. The digest is always sent and says the limit was reached;
- `/rewrite`, `/chat`, `/raw` reply that the limit is reached until the next run.

Admins are never limited. The admin can change one user's limit with `/setlimit`.

### Own API key (no limits)
`/setkey <gemini|deepseek|openai> <api key> [model]` — the key is checked with a tiny test request,
stored encrypted (`TOKEN_ENCRYPTION_KEY`), and the message with the key is deleted from the chat.
From then on the user's own key is used for everything, without limits. `/removekey` goes back to the
shared provider. `/llm` shows the current provider and usage.

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

## Capacity

Cloud Run: max 3 instances, concurrency 10, **300 s timeout**. `/run` processes all users in one request,
`RUN_CONCURRENCY` (5) users in parallel. With a fast model a user takes roughly 15–60 s, so one run
handles a few dozen users. After `RUN_SOFT_DEADLINE_SECONDS` (240 s) the run stops *starting* new
users/accounts; they're processed in full at the next run (nothing is lost, and nothing is half-done
when Cloud Run's timeout hits). Beyond that, move to a fan-out via Cloud Tasks (one request per user).

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
