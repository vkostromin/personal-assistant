.PHONY: auth deploy setup-telegram setup-scheduler

# Load .env if it exists
-include .env
export

auth:
	python3 -m venv .venv
	.venv/bin/pip install -q google-auth-oauthlib google-cloud-firestore
	PROJECT_ID=$(PROJECT_ID) .venv/bin/python3 scripts/auth.py

deploy:
	gcloud builds submit --config=cloudbuild.yaml \
		--project=$(PROJECT_ID) \
		--substitutions=_TELEGRAM_BOT_TOKEN=$(TELEGRAM_BOT_TOKEN),_DEEPSEEK_API_KEY=$(DEEPSEEK_API_KEY),_GOOGLE_CLIENT_ID=$(GOOGLE_CLIENT_ID),_GOOGLE_CLIENT_SECRET=$(GOOGLE_CLIENT_SECRET),_TOKEN_ENCRYPTION_KEY=$(TOKEN_ENCRYPTION_KEY),_TELEGRAM_WEBHOOK_SECRET=$(TELEGRAM_WEBHOOK_SECRET)

setup-scheduler:
	@echo "Updating Cloud Scheduler jobs with Cloud Run URL..."
	@URL=$$(gcloud run services describe personal-assistant --region=$(REGION) --format='value(status.url)' --project=$(PROJECT_ID)); \
	gcloud scheduler jobs update http lola-run --location=$(REGION) --uri="$$URL/run" --project=$(PROJECT_ID)
	@echo "Scheduler jobs updated."

setup-telegram:
	@echo "Setting Telegram webhook..."
	@URL=$$(gcloud run services describe personal-assistant --region=$(REGION) --format='value(status.url)'); \
	curl -s "https://api.telegram.org/bot$(TELEGRAM_BOT_TOKEN)/setWebhook?url=$$URL/webhook&secret_token=$(TELEGRAM_WEBHOOK_SECRET)&drop_pending_updates=true"
