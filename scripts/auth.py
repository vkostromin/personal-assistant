"""
One-time OAuth setup script.

Run this locally to get a refresh token for Gmail + Google Chat APIs
and save the account directly to Firestore.

Usage:
  pip install google-auth-oauthlib google-cloud-firestore
  python scripts/auth.py

You'll be prompted to:
1. Open a browser URL
2. Sign in to your Google account
3. Grant permissions for Gmail and Chat access
4. Enter your Google email address
5. The refresh token is saved automatically to Firestore

Requires PROJECT_ID env var (or set in .env).
"""

import argparse
import os
import sys
from datetime import datetime

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]

CLIENT_SECRET_FILE = os.environ.get(
    "OAUTH_CLIENT_SECRET_FILE",
    os.path.join(os.path.dirname(__file__), "..", "credentials.json"),
)


def save_to_firestore(email: str, refresh_token: str, project_id: str, chat_id: int | None = None) -> None:
    from google.cloud import firestore

    encryption_key = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if encryption_key:
        from src.crypto import encrypt
        refresh_token = encrypt(refresh_token, encryption_key)

    db = firestore.Client(project=project_id)
    data = {
        "refresh_token": refresh_token,
        "providers": ["gmail", "chat"],
        "enabled": True,
        "created_at": datetime.now(),
    }
    if chat_id is not None:
        data["chat_id"] = chat_id
    db.collection("accounts").document(email).set(data)
    print(f"✅ Account saved to Firestore: accounts/{email}")


def main():
    parser = argparse.ArgumentParser(description="Authorize a Google account")
    parser.add_argument("email", nargs="?", help="Google email address")
    parser.add_argument("--chat-id", type=int, help="Telegram chat ID to associate with this account")
    args = parser.parse_args()

    email_arg = args.email or os.environ.get("ACCOUNT_EMAIL", "")
    chat_id = args.chat_id

    if not os.path.exists(CLIENT_SECRET_FILE):
        print(f"Error: OAuth client secret file not found at {CLIENT_SECRET_FILE}")
        print()
        print("To create one:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create OAuth 2.0 Client ID (Desktop app type)")
        print(f"  3. Download JSON and save as {CLIENT_SECRET_FILE}")
        print()
        sys.exit(1)

    project_id = os.environ.get("PROJECT_ID", "")
    if not project_id:
        # Try loading from .env
        env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_file):
            for line in open(env_file):
                line = line.strip()
                if line.startswith("PROJECT_ID="):
                    project_id = line.split("=", 1)[1].strip()
                    break

    if not project_id:
        print("Error: PROJECT_ID is not set. Add it to .env or export PROJECT_ID=...")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=8080, open_browser=True)

    print("\n" + "=" * 60)
    print("OAUTH SUCCESSFUL")
    print("=" * 60)

    email = email_arg or input("\nEnter your Google email address: ").strip()
    if not email:
        print("Email is required. Pass it as argument: python3 scripts/auth.py user@gmail.com")
        sys.exit(1)

    save_to_firestore(email, creds.refresh_token, project_id, chat_id=chat_id)

    print()
    print(f"Refresh token: {creds.refresh_token}")
    print()
    print("To add another account, run this script again.")


if __name__ == "__main__":
    main()
