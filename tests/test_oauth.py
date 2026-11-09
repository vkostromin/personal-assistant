import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models import Account, User
from src.oauth import OAuthError, build_auth_url, parse_id_token, pkce_challenge, providers_from_scopes, SCOPES
from tests.conftest import BASE_URL, CLIENT_ID

ALL_SCOPES = " ".join(SCOPES)


def id_token(email="new@x.com", aud=CLIENT_ID, iss="https://accounts.google.com", exp=None, verified=True):
    claims = {"iss": iss, "aud": aud, "email": email, "email_verified": verified, "exp": exp or int(time.time()) + 3600}
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def bob(store):
    u = User(id=2002, chat_id=2002, name="Bob", username="bob")
    await store.save_user(u)
    return u


@pytest.fixture
def token_response(monkeypatch):
    resp = {"refresh_token": "rt-new", "access_token": "at", "scope": ALL_SCOPES, "id_token": id_token()}
    calls = []

    async def fake_exchange(code, client_id, client_secret, redirect_uri, code_verifier):
        calls.append(dict(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier))
        return resp

    monkeypatch.setattr("src.main.exchange_code", fake_exchange)
    resp["_calls"] = calls
    return resp


async def test_start_page_names_the_telegram_user_and_links_to_google(client, store, bob):
    st = await store.create_oauth_state(bob.id, "verifier-123", f"{BASE_URL}/oauth/callback")
    r = client.get(f"/oauth/start?state={st.id}")
    assert r.status_code == 200
    assert "Bob (@bob)" in r.text and "your own" in r.text
    href = r.text.split('class="btn" href="')[1].split('"')[0].replace("&amp;", "&")
    q = parse_qs(urlparse(href).query)
    assert href.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["state"] == [st.id]
    assert q["redirect_uri"] == [f"{BASE_URL}/oauth/callback"]
    assert q["access_type"] == ["offline"]
    assert q["code_challenge"] == [pkce_challenge("verifier-123")]
    assert "https://www.googleapis.com/auth/gmail.modify" in q["scope"][0]


def test_start_page_rejects_unknown_state(client):
    assert client.get("/oauth/start?state=nope").status_code == 400
    assert client.get("/oauth/start").status_code == 400


async def test_callback_links_account_to_the_right_user(client, store, bob, outbox, token_response):
    st = await store.create_oauth_state(bob.id, "verifier-123", f"{BASE_URL}/oauth/callback")
    r = client.get(f"/oauth/callback?state={st.id}&code=auth-code")
    assert r.status_code == 200 and "new@x.com" in r.text
    assert token_response["_calls"] == [dict(code="auth-code", redirect_uri=f"{BASE_URL}/oauth/callback", code_verifier="verifier-123")]

    acc = await store.get_account("new@x.com")
    assert acc.user_id == bob.id and acc.refresh_token == "rt-new" and acc.providers == ["gmail", "chat"]
    assert "connected" in outbox.last(bob.id)[1]

    # replaying the callback fails: state is single use
    assert client.get(f"/oauth/callback?state={st.id}&code=auth-code").status_code == 400


async def test_callback_refuses_email_owned_by_another_user(client, store, bob, outbox, token_response):
    await store.link_account(Account(email="new@x.com", user_id=1001, refresh_token="owner-token"))
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    r = client.get(f"/oauth/callback?state={st.id}&code=c")
    assert r.status_code == 409
    acc = await store.get_account("new@x.com")
    assert acc.user_id == 1001 and acc.refresh_token == "owner-token"
    assert "already linked" in outbox.last(bob.id)[1]


async def test_callback_partial_scopes(client, store, bob, outbox, token_response):
    token_response["scope"] = "openid email https://www.googleapis.com/auth/gmail.modify"
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    assert client.get(f"/oauth/callback?state={st.id}&code=c").status_code == 200
    assert (await store.get_account("new@x.com")).providers == ["gmail"]
    assert "No access granted for: chat" in outbox.last(bob.id)[1]


async def test_callback_no_permissions_granted(client, store, bob, outbox, token_response):
    token_response["scope"] = "openid email"
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    assert client.get(f"/oauth/callback?state={st.id}&code=c").status_code == 400
    assert await store.get_account("new@x.com") is None


async def test_callback_user_cancelled(client, store, bob, outbox):
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    r = client.get(f"/oauth/callback?state={st.id}&error=access_denied")
    assert r.status_code == 200 and "cancelled" in r.text
    assert "cancelled" in outbox.last(bob.id)[1]


async def test_callback_for_disabled_user_is_refused(client, store, bob, token_response):
    await store.update_user(bob.id, {"status": "disabled"})
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    assert client.get(f"/oauth/callback?state={st.id}&code=c").status_code == 400
    assert await store.get_account("new@x.com") is None


def test_parse_id_token_validation():
    assert parse_id_token(id_token(), CLIENT_ID)["email"] == "new@x.com"
    with pytest.raises(OAuthError):
        parse_id_token(id_token(aud="other"), CLIENT_ID)
    with pytest.raises(OAuthError):
        parse_id_token(id_token(iss="https://evil.example"), CLIENT_ID)
    with pytest.raises(OAuthError):
        parse_id_token(id_token(exp=int(time.time()) - 3600), CLIENT_ID)
    with pytest.raises(OAuthError):
        parse_id_token("garbage", CLIENT_ID)


def test_providers_from_scopes():
    assert providers_from_scopes(ALL_SCOPES) == ["gmail", "chat"]
    assert providers_from_scopes("https://www.googleapis.com/auth/chat.spaces.readonly") == []
    assert providers_from_scopes("") == []


def test_auth_url_always_requests_refresh_token():
    q = parse_qs(urlparse(build_auth_url("cid", "https://x/cb", "st", "v")).query)
    assert q["access_type"] == ["offline"] and "consent" in q["prompt"][0]
    assert q["code_challenge_method"] == ["S256"]


async def test_chat_send_permission_is_recorded(client, store, bob, token_response):
    from src.chat_client import CHAT_SEND_SCOPE
    assert CHAT_SEND_SCOPE in SCOPES
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    client.get(f"/oauth/callback?state={st.id}&code=c")
    assert (await store.get_account("new@x.com")).chat_send is True

    token_response["scope"] = ALL_SCOPES.replace(CHAT_SEND_SCOPE, "")  # user unticked "send messages"
    st = await store.create_oauth_state(bob.id, "v", f"{BASE_URL}/oauth/callback")
    client.get(f"/oauth/callback?state={st.id}&code=c")
    acc = await store.get_account("new@x.com")
    assert acc.chat_send is False and acc.providers == ["gmail", "chat"]
