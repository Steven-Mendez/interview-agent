"""Neon Auth JWT verification, admins and the internal token, offline.

A generated Ed25519 key stands in for Neon Auth's signing key and a fake JWKS
for its endpoint: nothing is fetched over the network.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import OKPAlgorithm

from interview_agent.config import settings
from interview_agent.server import auth

NEON_AUTH_URL = "https://ep-synthetic.neonauth.us-east-2.aws.neon.tech/neondb/auth"
ORIGIN = "https://ep-synthetic.neonauth.us-east-2.aws.neon.tech"
KEY_ID = "synthetic-key"
SIGNING_KEY = Ed25519PrivateKey.generate()
JWKS = {
    "keys": [
        {
            **OKPAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True),
            "kid": KEY_ID,
            "alg": "EdDSA",
        }
    ]
}


def token(**overrides) -> str:
    """A Neon Auth-shaped JWT; a claim overridden with None is left out."""
    now = int(time.time())
    claims = {
        "sub": "user-synthetic",
        "email": "candidate@example.com",
        "name": "Synthetic Candidate",
        "iss": ORIGIN,
        "aud": ORIGIN,
        "iat": now,
        "exp": now + 900,
    } | overrides
    return jwt.encode(
        {key: value for key, value in claims.items() if value is not None},
        SIGNING_KEY,
        algorithm="EdDSA",
        headers={"kid": KEY_ID},
    )


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def use_neon_auth(monkeypatch) -> list[str]:
    """neon mode against the fake JWKS. Returns the list of JWKS fetches."""
    fetches = []

    def fetch_data(client):
        # Like the real fetch: a successful response fills the JWK set cache.
        fetches.append(client.uri)
        client.jwk_set_cache.put(JWKS)
        return JWKS

    monkeypatch.setattr(settings, "auth_mode", "neon")
    monkeypatch.setattr(settings, "neon_auth_url", NEON_AUTH_URL)
    monkeypatch.setattr(auth, "_jwks_client", None)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_data)
    return fetches


@pytest.fixture
def neon_auth(monkeypatch):
    return use_neon_auth(monkeypatch)


@pytest.fixture
async def client():
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: auth.CurrentUser):
        return {"id": user.id, "email": user.email, "name": user.name}

    @app.get("/admin", dependencies=[Depends(auth.require_admin)])
    async def admin_only():
        return {"ok": True}

    @app.post("/internal", dependencies=[Depends(auth.internal_caller)])
    async def internal_only():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_a_valid_neon_token_is_the_user(neon_auth, client):
    response = await client.get("/whoami", headers=bearer(token()))
    assert response.status_code == 200
    assert response.json() == {
        "id": "user-synthetic",
        "email": "candidate@example.com",
        "name": "Synthetic Candidate",
    }
    assert neon_auth == [f"{NEON_AUTH_URL}/.well-known/jwks.json"]
    # The keys are cached: a second request fetches nothing.
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    assert len(neon_auth) == 1


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 60},
        {"iss": "https://attacker.example"},
        {"iss": NEON_AUTH_URL},
        {"aud": "https://attacker.example"},
        {"sub": None},
        {"exp": None},
        {"iat": None},
    ],
    ids=["expired", "issuer", "issuer-with-path", "audience", "no-sub", "no-exp", "no-iat"],
)
async def test_an_invalid_token_is_401(neon_auth, client, claims):
    value = token(**claims)
    response = await client.get("/whoami", headers=bearer(value))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert value not in response.text


async def test_only_eddsa_is_accepted(neon_auth, client):
    # Same claims and kid, signed with a shared secret: the algorithm list,
    # not the token header, decides how it is verified.
    forged = jwt.encode(
        jwt.decode(token(), options={"verify_signature": False}),
        "a-shared-secret-of-thirty-two-bytes!",
        algorithm="HS256",
        headers={"kid": KEY_ID},
    )
    response = await client.get("/whoami", headers=bearer(forged))
    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer "}],
    ids=["missing", "other-scheme", "empty"],
)
async def test_a_missing_bearer_token_is_401(neon_auth, client, headers):
    response = await client.get("/whoami", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert isinstance(response.json()["detail"], str)


async def test_an_unreachable_jwks_is_401_without_its_error(neon_auth, client, monkeypatch):
    def unreachable(client):
        raise jwt.PyJWKClientConnectionError("SENSITIVE_JWKS_FAILURE")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", unreachable)
    response = await client.get("/whoami", headers=bearer(token()))
    assert response.status_code == 401
    assert "SENSITIVE_JWKS_FAILURE" not in response.text


def foreign_kid_token(kid: str) -> str:
    return jwt.encode(
        {"sub": "x", "iss": ORIGIN, "aud": ORIGIN, "iat": 0, "exp": 1},
        SIGNING_KEY,
        algorithm="EdDSA",
        headers={"kid": kid},
    )


async def test_unknown_key_ids_refresh_the_jwks_at_most_once_a_minute(neon_auth, client):
    # Cold cache plus one refresh for the unknown kid.
    first = await client.get("/whoami", headers=bearer(foreign_kid_token("kid-1")))
    assert first.status_code == 401
    assert len(neon_auth) == 2
    # A fresh kid on every request fetches nothing more within the minute.
    for index in range(2, 6):
        response = await client.get("/whoami", headers=bearer(foreign_kid_token(f"kid-{index}")))
        assert response.status_code == 401
    assert len(neon_auth) == 2
    # Known keys keep working from the cache meanwhile.
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    assert len(neon_auth) == 2
    # A minute later a rotated key can be picked up again.
    auth._jwks_refreshed_at -= auth._JWKS_REFRESH_SECONDS + 1
    await client.get("/whoami", headers=bearer(foreign_kid_token("kid-rotated")))
    assert len(neon_auth) == 3


@pytest.mark.parametrize(
    "headers",
    [{"alg": "HS256", "kid": KEY_ID}, {"alg": "EdDSA"}, {"alg": "EdDSA", "kid": "k" * 129}],
    ids=["other-alg", "no-kid", "oversized-kid"],
)
async def test_a_foreign_token_header_is_401_without_a_fetch(neon_auth, client, headers):
    import base64
    import json

    encoded = base64.urlsafe_b64encode(json.dumps(headers).encode()).rstrip(b"=").decode()
    response = await client.get("/whoami", headers=bearer(f"{encoded}.e30.AA"))
    assert response.status_code == 401
    assert neon_auth == []


async def test_local_mode_is_the_local_developer_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local")
    response = await client.get("/whoami")
    assert response.status_code == 200
    assert response.json() == {"id": "local-dev", "email": None, "name": "Local developer"}


async def test_admins_are_listed_by_id_and_the_role_claim_is_ignored(
    neon_auth, client, monkeypatch
):
    monkeypatch.setattr(settings, "admin_user_ids", ["user-admin"])
    elevated = token(sub="user-synthetic", role="admin")
    assert (await client.get("/admin", headers=bearer(elevated))).status_code == 403
    listed = token(sub="user-admin", role="user")
    assert (await client.get("/admin", headers=bearer(listed))).status_code == 200


async def test_an_unset_internal_token_rejects_every_call(client, monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    for headers in ({}, {"X-Internal-Token": ""}, {"X-Internal-Token": "anything"}):
        assert (await client.post("/internal", headers=headers)).status_code == 401


async def test_the_internal_token_must_match(client, monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-internal-token")
    assert (await client.post("/internal")).status_code == 401
    wrong = {"X-Internal-Token": "synthetic-internal-tokeN"}
    assert (await client.post("/internal", headers=wrong)).status_code == 401
    right = {"X-Internal-Token": "synthetic-internal-token"}
    assert (await client.post("/internal", headers=right)).status_code == 200


def test_neon_mode_requires_the_auth_url_and_the_internal_token():
    from interview_agent.config import Settings

    base = {
        "OPENAI_API_KEY": "k",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
        "LIVEKIT_URL": "wss://synthetic.example",
    }
    with pytest.raises(RuntimeError, match="NEON_AUTH_URL, INTERNAL_API_TOKEN"):
        Settings(_env_file=None, AUTH_MODE="neon", **base).require_keys()
    Settings(_env_file=None, AUTH_MODE="local", **base).require_keys()
    configured = Settings(
        _env_file=None,
        AUTH_MODE="neon",
        NEON_AUTH_URL=NEON_AUTH_URL + "/",
        INTERNAL_API_TOKEN="synthetic-internal-token",
        ADMIN_USER_IDS=" user-a, ,user-b ",
        **base,
    ).require_keys()
    assert configured.neon_auth_url == NEON_AUTH_URL
    assert configured.admin_user_ids == ["user-a", "user-b"]
    assert "synthetic-internal-token" not in repr(configured)
