"""Google OAuth 2.0 web-server flow used to link Google accounts from Telegram.

Requires an OAuth client of type **Web application** whose authorized redirect
URI is ``<PUBLIC_URL>/oauth/callback``.
"""

import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx

from src.chat_client import CHAT_SCOPES, CHAT_SEND_SCOPE
from src.gmail_client import GMAIL_SCOPES

log = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
SCOPES = IDENTITY_SCOPES + GMAIL_SCOPES + CHAT_SCOPES + [CHAT_SEND_SCOPE]


class OAuthError(Exception):
    pass


def make_code_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43..128


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def build_auth_url(client_id: str, redirect_uri: str, state: str, code_verifier: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",  # always return a refresh token; let user pick the account
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URI}?{urlencode(params)}"


async def exchange_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str, code_verifier: str
) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TOKEN_URI,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
    if not resp.is_success:
        raise OAuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def _b64url_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def parse_id_token(id_token: str, client_id: str) -> dict:
    """Return the claims of an ID token obtained *directly* from Google's token endpoint.

    Per OpenID Connect Core §3.1.3.7 the TLS connection to the token endpoint
    authenticates the issuer, so the signature check can be skipped; we still
    validate issuer, audience and expiry.
    """
    try:
        claims = _b64url_json(id_token.split(".")[1])
    except (IndexError, ValueError) as exc:
        raise OAuthError("Malformed ID token") from exc
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise OAuthError("Unexpected ID token issuer")
    if claims.get("aud") != client_id:
        raise OAuthError("ID token audience mismatch")
    if int(claims.get("exp", 0)) < time.time() - 60:
        raise OAuthError("ID token expired")
    return claims


def providers_from_scopes(granted: str) -> list[str]:
    """Google lets users untick individual permissions; only enable what was granted."""
    scopes = set(granted.split())
    providers = []
    if all(s in scopes for s in GMAIL_SCOPES):
        providers.append("gmail")
    if all(s in scopes for s in CHAT_SCOPES):
        providers.append("chat")
    return providers


def can_send_chat(granted: str) -> bool:
    return CHAT_SEND_SCOPE in set(granted.split())


async def revoke_token(token: str) -> bool:
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(REVOKE_URI, data={"token": token})
        return resp.is_success
    except httpx.HTTPError as exc:
        log.warning("Token revocation failed: %s", exc)
        return False
