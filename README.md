# Personal Assistant (Lola)

AI personal assistant that monitors Gmail + Google Chat, drafts responses, tracks promises, and sends daily summaries via Telegram.

## Architecture

Cloud Run service + Cloud Scheduler (2x daily) + interactive Telegram bot.

## Setup

### 1. Create Telegram bot
- Open @BotFather on Telegram
- `/newbot` → name: `Lola` → username: `your_lola_bot`
- Copy the bot token

### 2. Create GCP project
```bash
gcloud projects create personal-assistant --name="Personal Assistant"
gcloud config set project personal-assistant
gcloud services enable run.googleapis.com scheduler.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com \
  gmail.googleapis.com chat.googleapis.com artifactregistry.googleapis.com
```

### 3. Create OAuth credentials
- Go to GCP Console → APIs & Services → Credentials
- Create OAuth 2.0 Client ID → **Desktop app** type
- Download JSON as `credentials.json` in project root

### 4. Run OAuth flow
```bash
pip install google-auth-oauthlib
OAUTH_CLIENT_SECRET_FILE=credentials.json python scripts/auth.py
# → opens browser → sign in → copy refresh token
```

### 5. Deploy secrets
```bash
export TELEGRAM_BOT_TOKEN="your:token"
export DEEPSEEK_API_KEY="sk-..."
export GOOGLE_CLIENT_ID="...apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="..."
export REFRESH_TOKEN="..."  # from auth step
export ACCOUNTS="user@example.com"
make deploy-secrets
```

### 6. Deploy Cloud Run
```bash
make deploy
```

### 7. Set Telegram webhook
```bash
make setup-telegram
```

### 8. Configure Cloud Scheduler
```bash
gcloud scheduler jobs create http twice-daily-morning \
  --schedule="0 8 * * *" \
  --uri="$(gcloud run services describe personal-assistant --region=us-central1 --format='value(status.url)')/run" \
  --http-method=POST

gcloud scheduler jobs create http twice-daily-evening \
  --schedule="0 20 * * *" \
  --uri="$(gcloud run services describe personal-assistant --region=us-central1 --format='value(status.url)')/run" \
  --http-method=POST
```

### 9. Set timezone
```bash
gcloud scheduler jobs update twice-daily-morning --time-zone="Asia/Jakarta"
gcloud scheduler jobs update twice-daily-evening --time-zone="Asia/Jakarta"
```

### 10. Start the bot
- Open Telegram → find your bot
- Send `/start`

## Adding another account

```bash
# Re-run auth with the second account
OAUTH_CLIENT_SECRET_FILE=credentials.json python scripts/auth.py
# Copy the new refresh token

# Store the refresh token
gcloud secrets create account-user2-example-com-refresh-token \
  --replication-policy=automatic
echo -n "NEW_REFRESH_TOKEN" | \
  gcloud secrets versions add account-user2-example-com-refresh-token --data-file=-

# Update Cloud Run to access the new secret
gcloud run services update personal-assistant \
  --update-secrets=account-user2-example-com-refresh-token=account-user2-example-com-refresh-token:latest
```

## Development

```bash
# Local testing
pip install -r requirements.txt
python -m uvicorn src.main:app --reload --port 8080
```
