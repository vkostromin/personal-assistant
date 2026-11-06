.PHONY: deploy setup-telegram setup-scheduler setup-firestore-ttl test

# Load .env if it exists
-include .env
export

deploy:
	gcloud builds submit --config=cloudbuild.yaml \
		--project=$(PROJECT_ID) \
		--substitutions=_TELEGRAM_BOT_TOKEN=$(TELEGRAM_BOT_TOKEN),_DEEPSEEK_API_KEY=$(DEEPSEEK_API_KEY),_GEMINI_API_KEY=$(GEMINI_API_KEY),_OPENAI_API_KEY=$(OPENAI_API_KEY),_USER_TOKEN_LIMIT=$(or $(USER_TOKEN_LIMIT),150000),_GOOGLE_CLIENT_ID=$(GOOGLE_CLIENT_ID),_GOOGLE_CLIENT_SECRET=$(GOOGLE_CLIENT_SECRET),_TOKEN_ENCRYPTION_KEY=$(TOKEN_ENCRYPTION_KEY),_TELEGRAM_WEBHOOK_SECRET=$(TELEGRAM_WEBHOOK_SECRET),_RUN_SECRET=$(RUN_SECRET),_PUBLIC_URL=$(PUBLIC_URL),_ADMIN_TELEGRAM_ID=$(ADMIN_TELEGRAM_ID),_TELEGRAM_BOT_USERNAME=$(TELEGRAM_BOT_USERNAME)

setup-scheduler:
	@echo "Updating Cloud Scheduler job with Cloud Run URL..."
	@URL=$$(gcloud run services describe personal-assistant --region=$(REGION) --format='value(status.url)' --project=$(PROJECT_ID)); \
	gcloud scheduler jobs update http lola-run --location=$(REGION) --uri="$$URL/run" \
		--update-headers=X-Run-Secret=$(RUN_SECRET) --attempt-deadline=300s --project=$(PROJECT_ID)
	@echo "Scheduler job updated."

setup-telegram:
	@echo "Setting Telegram webhook..."
	@URL=$$(gcloud run services describe personal-assistant --region=$(REGION) --format='value(status.url)' --project=$(PROJECT_ID)); \
	curl -s "https://api.telegram.org/bot$(TELEGRAM_BOT_TOKEN)/setWebhook?url=$$URL/webhook&secret_token=$(TELEGRAM_WEBHOOK_SECRET)&drop_pending_updates=true"

# Optional: let Firestore auto-delete expired "connect Google account" links
setup-firestore-ttl:
	gcloud firestore fields ttls update expires_at --collection-group=oauth_states --enable-ttl --project=$(PROJECT_ID)

test:
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
	PROJECT_ID=test .venv/bin/python -m pytest -q
