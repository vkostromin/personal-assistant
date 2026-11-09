# Personal Assistant (Lola)

AI personal assistant that monitors Gmail + Google Chat, drafts responses, tracks promises in both
directions (what I promised / what others owe me), reminds you before things slip, and tells you what to
focus on right now — all from Telegram, with buttons to act on the spot.

**Multi-user:** one Cloud Run service and one Telegram bot serve many people. Every user links their
own Google account(s) from Telegram and gets their own summaries, drafts and promises — fully
independent of the admin's account. The first person to talk to a fresh deployment becomes the admin;
everybody else joins with an invitation link.

## Architecture

Cloud Run service + Cloud Scheduler (hourly; each user gets summaries at 08:00 and 20:00 in their own
timezone) + interactive Telegram bot. See [PLAN.md](PLAN.md).

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
  `.../auth/chat.spaces.readonly`, `.../auth/chat.messages.readonly`, `.../auth/chat.messages.create`.
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
make setup-telegram   # also enables button presses (callback_query updates)
gcloud scheduler jobs create http lola-run --location=us-central1 \
  --schedule="0 * * * *" --time-zone="Etc/UTC" \
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
| `/focus`, `/tasks [kind]`, `/task <text>`, plain/forwarded text → task | |
| `/who [name]` — everything open with a person; `/review` — decide on old items | |
| `/status`, `/chat <question>`, `/raw <text>` | `/invite [note]`, `/invites`, `/revoke <code>` |
| Drafts: `/drafts`, `/draft N`, `/rewrite N <context>`, `/sent N…`, `/deletedraft N…` | `/users`, `/disable <id>`, `/enable <id>` |
| Google: `/connect`, `/accounts`, `/unlink [email] <gmail\|chat>`, `/disconnect <email>` | `/remove <id>` — deletes the user, their accounts (tokens revoked), drafts and promises |
| Settings: `/timezone [zone]`, `/llm`, `/setkey <provider> <key> [model]`, `/removekey` | `/setlimit <id> <tokens\|default\|unlimited>` — e.g. `/setlimit 12345 300k` |
| `/deleteme` — delete own account and all data (confirm with `/deleteme YES`), `/help` | `/transferadmin <id>` — hand the admin role to another user |

### Focus, promises and reminders
Lola tracks four kinds of **items**, extracted from incoming mail, **your sent mail**, and Chat — or added by
you (`/task …`, or just send/forward any message and tap **➕ Add as task**):

| Kind | Example | Reminded |
|------|---------|----------|
| 🫡 I promised | "I'll send the deck tomorrow" (from your sent mail / Chat) | 2h before the deadline, at the deadline, then daily 09:00 while overdue (3×) |
| ⏳ Waiting for | "Alex will send numbers by Friday", or a question you asked that's unanswered | when they're late (default deadline: 48h) — with **✍️ Follow-up**, which drafts a polite nudge in the same thread |
| 💬 Needs my reply | a Chat question with no draft possible | via Focus (and its deadline, if any) |
| 📌 To do | "sign the contract by the 15th" | like promises |

- Deadlines are understood in your timezone ("by Friday" → Fri 18:00). Items are closed automatically when
  mail/chat shows they're resolved (the numbers arrived, you sent what you promised), or by you.
- **🎯 `/focus`** ranks everything: overdue first, then due today, priority, things people wait on you for,
  drafts ready to send, age. "Waiting for" items only appear once they're late. Every summary starts with the
  top items and ends with a Focus message with buttons.
- Reminders are never sent during quiet hours (`QUIET_HOURS`, default 21–8 local) and at most
  `MAX_REMINDERS_PER_RUN` (5) per hour.
- `/tasks` (or `/tasks waiting|mine|replies|todo`) lists everything; tap a number to open its card.

**Buttons** on every card:
- item: ✅ Done · ⏰ Snooze (2h / 4h / 8h / tomorrow 9:00) · 📅 Due (today / tomorrow / +3 days / next Monday /
  none / ✏️ type it) · ✖️ Cancel · ✍️ Follow-up · ↩️ Reply (Chat items) · 🔗 Open in Gmail/Chat
- draft: 📤 Send (asks for confirmation, sends the Gmail draft as it is — including your edits in Gmail) ·
  ✏️ Rewrite (reply with instructions) · ⏰ Snooze · 🗑 Discard · ✔️ Already sent · 🔗 Gmail

### Replying to Google Chat from Telegram
Chat items (questions from Chat) have **↩️ Reply**: type your answer — or tap **✨ Let Lola draft it** —
check the preview and press **📤 Send**. It's posted *as you*, in the same Chat thread, and the item is
closed. Needs the `chat.messages.create` permission: **accounts linked before this feature must `/connect`
again** (`/accounts` shows which ones). Add the scope to the OAuth consent screen.

### /who and /review
- `/who alex` — before a call: what you promised them, what they owe you, what they wait for, drafts to
  them. Matches names and emails (case/accents ignored) and groups "Alex Smith <alex@x.com>" with
  "alex@x.com". `/who` alone lists people with open items, overdue first.
- `/review` — walks through items older than `REVIEW_AFTER_DAYS` (7) or overdue with all reminders used:
  ✅ Done · ✖️ Cancel · 🔁 Remind again (restarts the reminder sequence) · 📅 New deadline · ⏭ Skip · ⏹ Stop.
  Reviewed items don't come back for a week. Summaries suggest `/review` when 5+ items are getting old.

### How much mail is read
- Every run reads everything since the previous run, up to `MAX_MESSAGES_PER_RUN` (100) received + 100
  sent emails per mailbox; if there were more, the newest 100 are analyzed and the summary says so.
- The first run of a new account looks back `INITIAL_LOOKBACK_HOURS` (1 week), up to
  `MAX_MESSAGES_FIRST_RUN` (200).
- Users with their **own AI key have no count limit**.
- Mail is processed oldest-first in batches of `EMAIL_BATCH_SIZE` (20) per AI call; the position is
  saved after every batch, so a run that hits the time or AI limit continues where it stopped. A big
  backlog continues **quietly in the next hourly runs** (no extra summaries) until it's done.

### Drafts
- Lola creates replies as **Gmail drafts**; nothing is sent unless you press 📤 Send and confirm.
- **Sync with Gmail:** at every summary and whenever you open `/drafts`, drafts that disappeared from
  Gmail are updated: *sent* if a message was sent in that conversation after the draft was created,
  otherwise *deleted*. They drop out of the list (the summary says e.g. "Drafts updated from Gmail: 2 sent").
- **Manually:** `/sent 1 3` marks drafts as sent (Gmail untouched), `/deletedraft 2` deletes a draft in
  Lola *and* in Gmail. Numbers follow `/drafts`; ranges (`2-4`) and `all` work.
- `/rewrite` edits the existing Gmail draft in place; if the draft was already sent/deleted it says so.
- Replies are threaded properly (Gmail `threadId` + `In-Reply-To`/`References` headers).

### Timezones
Each user gets summaries at **08:00 and 20:00 in their own timezone** (`SUMMARY_HOURS`). Set it with
`/timezone Europe/Berlin`, `/timezone jakarta`, `/timezone +7`. Users without a timezone use
`DEFAULT_TIMEZONE`. The scheduler calls `/run` hourly; a summary missed because of an outage or the run
deadline is sent at the next hourly run. `POST /run?force=true` processes everyone immediately.

### Leaving
- `/unlink me@x.com chat` — stop reading Chat (or Gmail) of an account; `/connect` turns it back on.
- `/disconnect me@x.com` — unlink the Google account and revoke Lola's access.
- `/deleteme` → `/deleteme YES` — delete the Lola account with everything. The admin must first
  `/transferadmin` to another user (if the admin is the only user, the bot is reset instead).

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
Reading your sent mail adds one analysis call per account and run when you sent something.
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

**Re-run `make setup-telegram`** (buttons need `callback_query` updates). Open promises are moved to
the new `items` collection automatically. **Switch the scheduler to hourly** (`make setup-scheduler`) and set `DEFAULT_TIMEZONE` (e.g.
`Asia/Jakarta`, the old scheduler's zone) so existing users keep their times.

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

Cloud Run: max 3 instances, concurrency 10, **300 s timeout**. Each hourly `/run` processes only the users
whose local 08:00/20:00 just arrived (timezones spread the load), `RUN_CONCURRENCY` (5) in parallel. With a fast model a user takes roughly 15–60 s, so one run
handles a few dozen users. After `RUN_SOFT_DEADLINE_SECONDS` (240 s) the run stops *starting* new
users/accounts; they're processed at the next hourly run (nothing is lost, and nothing is half-done
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
Test settings come from `tests/.env` (git-ignored; copy `tests/.env.example`) — falling back to
`tests/.env.example`. Only that file is used, so secrets exported in your shell never leak into tests.
