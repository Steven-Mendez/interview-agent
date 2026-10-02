"""Neon Auth JWT verification, local accounts, admins and the internal token,
offline.

A generated Ed25519 key stands in for Neon Auth's signing key and a fake JWKS
for its endpoint: nothing is fetched over the network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import time
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import OKPAlgorithm
from pydantic import ValidationError

from interview_agent.config import LocalAccount, Settings, settings
from interview_agent.log_templates import SAFE_LOG_TEMPLATES
from interview_agent.server import auth, auth_routes

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

    app.include_router(auth_routes.router, prefix="/api")

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


def unreachable_jwks(monkeypatch) -> list[str]:
    """Every later JWKS fetch fails. Returns the list of attempts."""
    attempts = []

    def unreachable(client):
        attempts.append(client.uri)
        raise jwt.PyJWKClientConnectionError("SENSITIVE_JWKS_FAILURE")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", unreachable)
    return attempts


async def test_an_unreachable_jwks_is_503_without_its_error(neon_auth, client, monkeypatch, caplog):
    # Never fetched yet: nothing can verify the token, but nothing says it is
    # invalid either, so the browser keeps its session.
    attempts = unreachable_jwks(monkeypatch)
    with caplog.at_level("WARNING", logger="interview_agent"):
        response = await client.get("/whoami", headers=bearer(token()))
        # No request waits on another fetch within the minute...
        assert (await client.get("/whoami", headers=bearer(token()))).status_code == 503
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication is temporarily unavailable"}
    assert "www-authenticate" not in response.headers
    assert "SENSITIVE_JWKS_FAILURE" not in response.text
    assert len(attempts) == 1
    assert [record.msg for record in caplog.records] == ["Neon Auth JWKS fetch failed"]
    assert "Neon Auth JWKS fetch failed" in SAFE_LOG_TEMPLATES
    assert "SENSITIVE_JWKS_FAILURE" not in caplog.text
    # ...and a minute later the JWKS is tried again.
    auth._jwks_failed_at -= auth._JWKS_REFRESH_SECONDS + 1
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 503
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("SENSITIVE_JWKS_FAILURE"), jwt.PyJWKClientError("no signing keys")],
    ids=["timeout", "no-keys"],
)
async def test_any_failure_obtaining_the_keys_is_503(neon_auth, client, monkeypatch, failure):
    def failing(client):
        raise failure

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", failing)
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 503


def expire_jwks_cache() -> None:
    """PyJWKClient's copy of the keys outlives its lifespan."""
    cached = auth._jwks_client.jwk_set_cache.jwk_set_with_timestamp
    cached.timestamp -= auth._jwks_client.jwk_set_cache.lifespan + 1


async def test_known_keys_outlive_a_failed_refresh(neon_auth, client, monkeypatch):
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    attempts = unreachable_jwks(monkeypatch)
    expire_jwks_cache()
    # The cache's lifespan is over and the refetch fails: the keys fetched
    # earlier still verify the token.
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    assert len(attempts) == 1
    # Meanwhile no request waits on another fetch within the minute...
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    assert len(attempts) == 1
    # ...an unknown key cannot be told apart from a rotated one, refreshed
    # (and failing) at most once a minute...
    for kid in ("kid-rotated", "kid-other"):
        response = await client.get("/whoami", headers=bearer(foreign_kid_token(kid)))
        assert response.status_code == 503
    assert len(attempts) == 2
    # ...and a minute later the JWKS is tried again.
    auth._jwks_failed_at -= auth._JWKS_REFRESH_SECONDS + 1
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    assert len(attempts) == 3


async def test_known_keys_serve_while_another_request_fetches(neon_auth, client, monkeypatch):
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    attempts = unreachable_jwks(monkeypatch)
    expire_jwks_cache()
    # A fetch already under way (and possibly hanging): no second one.
    with auth._jwks_fetch_lock:
        assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    assert attempts == []


async def test_a_failed_refresh_for_an_unknown_key_is_503(neon_auth, client, monkeypatch):
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200
    unreachable_jwks(monkeypatch)
    # The rotated key may be in the JWKS that could not be fetched.
    response = await client.get("/whoami", headers=bearer(foreign_kid_token("kid-rotated")))
    assert response.status_code == 503
    # The known key still works.
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 200


@pytest.mark.parametrize(
    ("claim", "offset"), [("iat", 20), ("exp", -20)], ids=["issued-ahead", "just-expired"]
)
async def test_clock_skew_within_the_leeway_is_accepted(neon_auth, client, claim, offset):
    # The time is taken when the test runs, not at collection: a slow
    # suite would otherwise push "just expired" past the leeway.
    skewed = token(**{claim: int(time.time()) + offset})
    assert (await client.get("/whoami", headers=bearer(skewed))).status_code == 200


async def test_clock_skew_beyond_the_leeway_is_401(neon_auth, client):
    ahead = token(iat=int(time.time()) + 120)
    assert (await client.get("/whoami", headers=bearer(ahead))).status_code == 401


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
    hint = r"\(for local development set AUTH_MODE=local\)"
    with pytest.raises(RuntimeError, match=f"NEON_AUTH_URL, INTERNAL_API_TOKEN {hint}"):
        Settings(_env_file=None, AUTH_MODE="neon", **base).require_keys("api")
    Settings(_env_file=None, AUTH_MODE="local", **base).require_keys("api")
    configured = Settings(
        _env_file=None,
        AUTH_MODE="neon",
        NEON_AUTH_URL=NEON_AUTH_URL + "/",
        INTERNAL_API_TOKEN="synthetic-internal-token",
        ADMIN_USER_IDS=" user-a, ,user-b ",
        **base,
    ).require_keys("api")
    assert configured.neon_auth_url == NEON_AUTH_URL
    assert configured.admin_user_ids == ["user-a", "user-b"]
    assert "synthetic-internal-token" not in repr(configured)


def test_the_worker_requires_only_the_internal_token():
    # It never verifies a JWT: it only sends INTERNAL_API_TOKEN to /evaluate.
    from interview_agent.config import Settings

    base = {
        "OPENAI_API_KEY": "k",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
        "LIVEKIT_URL": "wss://synthetic.example",
    }
    with pytest.raises(RuntimeError, match=r"\.env: INTERNAL_API_TOKEN \(for local") as missing:
        Settings(_env_file=None, AUTH_MODE="neon", **base).require_keys("worker")
    assert "NEON_AUTH_URL" not in str(missing.value)
    Settings(
        _env_file=None, AUTH_MODE="neon", INTERNAL_API_TOKEN="synthetic-internal-token", **base
    ).require_keys("worker")
    Settings(_env_file=None, AUTH_MODE="local", **base).require_keys("worker")
    # Provider keys missing are not an accounts problem: no AUTH_MODE hint.
    with pytest.raises(RuntimeError, match=r"\.env: LIVEKIT_API_KEY\.$") as provider:
        Settings(
            _env_file=None, AUTH_MODE="local", **(base | {"LIVEKIT_API_KEY": ""})
        ).require_keys("worker")
    assert "AUTH_MODE" not in str(provider.value)


# ---- local accounts (the dev login) ------------------------------------------

ADMIN_PASSWORD = "synthetic-admin-password"
GUEST_PASSWORD = "contraseña-de-prueba-ñandú-密码"
ACCOUNTS = (LocalAccount("admin", ADMIN_PASSWORD), LocalAccount("guest", GUEST_PASSWORD))
SIGN_IN = "/api/auth/local/sign-in"
REJECTED = {"detail": "Invalid username or password"}
PROVIDER_KEYS = {
    "OPENAI_API_KEY": "k",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "s",
    "LIVEKIT_URL": "wss://synthetic.example",
}


@pytest.fixture
def local_accounts(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "local_accounts", ACCOUNTS)


def local_token(key: bytes | None = None, **overrides) -> str:
    """A local-account token; a claim overridden with None is left out."""
    now = int(time.time())
    claims = {
        "sub": "local:admin",
        "name": "admin",
        "iss": "interview-agent-local",
        "aud": "interview-agent-local",
        "iat": now,
        "exp": now + 900,
    } | overrides
    return jwt.encode(
        {name: value for name, value in claims.items() if value is not None},
        key if key is not None else auth._local_signing_key(),
        algorithm="HS256",
    )


async def sign_in(client: AsyncClient, username: str, password: str):
    return await client.post(SIGN_IN, json={"username": username, "password": password})


async def test_a_local_account_signs_in_and_its_token_is_the_user(local_accounts, client):
    before = datetime.now(UTC)
    response = await sign_in(client, "admin", ADMIN_PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {"id": "local:admin", "name": "admin"}
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert expires_at.utcoffset() == timedelta(0)
    lifetime = expires_at - before
    assert timedelta(hours=12) - timedelta(seconds=2) <= lifetime <= timedelta(hours=12)
    me = await client.get("/whoami", headers=bearer(body["token"]))
    assert me.status_code == 200
    assert me.json() == {"id": "local:admin", "email": None, "name": "admin"}
    # Without the token, every user route is closed.
    anonymous = await client.get("/whoami")
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"


async def test_a_non_ascii_password_signs_in(local_accounts, client):
    response = await sign_in(client, "guest", GUEST_PASSWORD)
    assert response.status_code == 200
    token = response.json()["token"]
    me = (await client.get("/whoami", headers=bearer(token))).json()
    assert me["id"] == "local:guest"


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("admin", "wrong-password"),
        ("admin", ADMIN_PASSWORD.upper()),
        ("admin", "ñandú-密码"),
        ("guest", GUEST_PASSWORD[:-1]),
        ("nobody", ADMIN_PASSWORD),
        ("ADMIN", ADMIN_PASSWORD),
        ("admin", ""),
        ("", ""),
    ],
    ids=["wrong", "case", "non-ascii", "prefix", "unknown", "other-case", "empty", "blank"],
)
async def test_wrong_credentials_get_the_same_401(
    local_accounts, client, caplog, username, password
):
    with caplog.at_level("INFO", logger="interview_agent"):
        response = await sign_in(client, username, password)
    assert response.status_code == 401
    assert response.json() == REJECTED
    assert "token" not in response.text
    # Only the fixed template, never the username or the password.
    assert [record.msg for record in caplog.records] == ["local sign-in rejected"]
    assert "local sign-in rejected" in SAFE_LOG_TEMPLATES
    for record in caplog.records:
        assert record.args == ()
    if password:
        assert password not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {"username": "a" * 65, "password": ADMIN_PASSWORD},
        {"username": "admin", "password": "p" * 257},
        {"username": "admin"},
        {"username": "admin", "password": 12345},
        {"username": ["admin"], "password": ADMIN_PASSWORD},
        {"username": "admin", "password": ADMIN_PASSWORD, "role": "admin"},
    ],
    ids=["long-username", "long-password", "no-password", "number", "list", "extra-field"],
)
async def test_malformed_sign_in_bodies_are_422(local_accounts, client, body):
    response = await client.post(SIGN_IN, json=body)
    assert response.status_code == 422
    # The input is never echoed back.
    assert response.json() == {"detail": "Invalid sign-in request"}


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"username": "admin", "password": "\\ud800x"}'],
    ids=["empty", "not-json", "array", "lone-surrogate"],
)
async def test_unparsable_sign_in_bodies_are_422(local_accounts, client, body):
    response = await client.post(
        SIGN_IN, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid sign-in request"}


@pytest.mark.parametrize(
    ("mode", "accounts"),
    [("neon", ACCOUNTS), ("local", ())],
    ids=["neon", "local-without-accounts"],
)
async def test_the_sign_in_route_is_404_without_the_dev_login(client, monkeypatch, mode, accounts):
    monkeypatch.setattr(settings, "auth_mode", mode)
    monkeypatch.setattr(settings, "local_accounts", accounts)
    response = await sign_in(client, "admin", ADMIN_PASSWORD)
    assert response.status_code == 404
    assert "token" not in response.json()


@pytest.mark.parametrize(
    ("mode", "accounts", "expected"),
    [("local", (), "none"), ("local", ACCOUNTS, "local"), ("neon", ACCOUNTS, "neon")],
)
async def test_the_auth_config_names_the_mode(client, monkeypatch, mode, accounts, expected):
    monkeypatch.setattr(settings, "auth_mode", mode)
    monkeypatch.setattr(settings, "local_accounts", accounts)
    # Public: no token needed, whatever the mode.
    response = await client.get("/api/auth/config")
    assert response.status_code == 200
    assert response.json() == {"mode": expected}


def counted(points) -> dict[tuple, int]:
    """Samples per attribute set, the attributes as sorted (key, value) pairs."""
    return {tuple(sorted(dict(point.attributes).items())): point.count for point in points}


async def test_dev_sign_ins_and_rejections_are_counted_without_who(
    local_accounts, client, recorded_metrics
):
    issued = (await sign_in(client, "admin", ADMIN_PASSWORD)).json()["token"]
    for password in ("wrong-password", ""):
        assert (await sign_in(client, "guest", password)).status_code == 401
    assert counted(recorded_metrics("interview_agent.auth.sign_in")) == {
        (("result", "ok"),): 1,
        (("result", "failed"),): 2,
    }
    # A signed-in request is no rejection; a missing or bad token is one.
    assert (await client.get("/whoami", headers=bearer(issued))).status_code == 200
    assert (await client.get("/whoami")).status_code == 401
    for value in (issued + "x", local_token(sub="local:nobody")):
        assert (await client.get("/whoami", headers=bearer(value))).status_code == 401
    assert counted(recorded_metrics("interview_agent.auth.rejected")) == {
        (("reason", "missing_token"),): 1,
        (("reason", "invalid_token"),): 2,
    }


async def test_neon_rejections_are_counted_by_reason(
    neon_auth, client, monkeypatch, recorded_metrics
):
    unreachable_jwks(monkeypatch)
    assert (await client.get("/whoami", headers=bearer(token()))).status_code == 503
    assert (await client.get("/whoami", headers={"Authorization": "Bearer "})).status_code == 401
    # Refused from its header alone, before any key is needed.
    foreign = jwt.encode({"sub": "user-synthetic"}, "a-shared-secret-of-thirty-two-bytes!")
    assert (await client.get("/whoami", headers=bearer(foreign))).status_code == 401
    assert counted(recorded_metrics("interview_agent.auth.rejected")) == {
        (("reason", "keys_unavailable"),): 1,
        (("reason", "missing_token"),): 1,
        (("reason", "invalid_token"),): 1,
    }
    # The internal token is not a user's: its refusals are not counted here.
    assert (await client.post("/internal")).status_code == 401
    assert sum(point.count for point in recorded_metrics("interview_agent.auth.rejected")) == 3


async def test_the_token_lapses_after_its_expiry(local_accounts, client):
    now = int(time.time())
    expired = local_token(iat=now - 13 * 3600, exp=now - 1)
    assert (await client.get("/whoami", headers=bearer(expired))).status_code == 401
    assert (await client.get("/whoami", headers=bearer(local_token()))).status_code == 200


async def test_the_token_lapses_when_the_account_is_removed(local_accounts, client, monkeypatch):
    issued = (await sign_in(client, "guest", GUEST_PASSWORD)).json()["token"]
    # Only the guest's account goes; the key is that of the new list.
    monkeypatch.setattr(settings, "local_accounts", ACCOUNTS[:1])
    still_signed = local_token(sub="local:guest", name="guest")
    for value in (issued, still_signed):
        assert (await client.get("/whoami", headers=bearer(value))).status_code == 401
    assert (await client.get("/whoami", headers=bearer(local_token()))).status_code == 200


async def test_changing_a_password_invalidates_every_token(local_accounts, client, monkeypatch):
    issued = (await sign_in(client, "admin", ADMIN_PASSWORD)).json()["token"]
    assert (await client.get("/whoami", headers=bearer(issued))).status_code == 200
    # Only the guest's password changes: the admin's token lapses all the same.
    monkeypatch.setattr(
        settings, "local_accounts", (ACCOUNTS[0], LocalAccount("guest", "a-new-password"))
    )
    assert (await client.get("/whoami", headers=bearer(issued))).status_code == 401
    assert (await sign_in(client, "guest", GUEST_PASSWORD)).status_code == 401
    assert (await sign_in(client, "guest", "a-new-password")).status_code == 200


def test_the_signing_key_survives_a_restart(monkeypatch):
    # Derived from the accounts, not random: a restart (or --reload) keeps it.
    monkeypatch.setattr(settings, "local_accounts", ACCOUNTS)
    first = auth._local_signing_key()
    monkeypatch.setattr(settings, "local_accounts", tuple(LocalAccount(*a) for a in ACCOUNTS))
    assert auth._local_signing_key() == first
    assert len(first) == 32


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.example"},
        {"aud": "https://attacker.example"},
        {"sub": "admin"},
        {"sub": "local-dev"},
        {"sub": "user-synthetic"},
        {"sub": "local:"},
        {"sub": None},
        {"exp": None},
        {"iat": None},
        {"iss": None},
        {"aud": None},
    ],
    ids=[
        "issuer",
        "audience",
        "unprefixed",
        "local-dev",
        "neon-user",
        "empty-username",
        "no-sub",
        "no-exp",
        "no-iat",
        "no-iss",
        "no-aud",
    ],
)
async def test_an_invalid_local_token_is_401(local_accounts, client, claims):
    value = local_token(**claims)
    response = await client.get("/whoami", headers=bearer(value))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Authentication required"}


async def test_a_token_signed_with_another_key_is_401(local_accounts, client):
    forged = local_token(key=hashlib.sha256(b"another-key").digest())
    assert (await client.get("/whoami", headers=bearer(forged))).status_code == 401


async def test_the_accounts_alone_never_sign_a_token(local_accounts, client, monkeypatch):
    # Knowing (or guessing) every password is not enough: the key also needs
    # the shared secret, so a token is no offline oracle for the passwords.
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-internal-token")
    accounts = f"admin:{ADMIN_PASSWORD},guest:{GUEST_PASSWORD}".encode()
    from_accounts = hashlib.sha256(b"interview-agent-local-jwt\0" + accounts).digest()
    forged = local_token(key=from_accounts)
    assert (await client.get("/whoami", headers=bearer(forged))).status_code == 401
    issued = (await sign_in(client, "admin", ADMIN_PASSWORD)).json()["token"]
    assert (await client.get("/whoami", headers=bearer(issued))).status_code == 200
    # Rotating the shared secret signs every account out.
    monkeypatch.setattr(settings, "internal_api_token", "another-internal-token")
    assert (await client.get("/whoami", headers=bearer(issued))).status_code == 401


async def test_an_undecodable_password_in_the_environment_never_breaks_sign_in(client, monkeypatch):
    # POSIX decodes non-UTF-8 environment bytes into lone surrogates.
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(
        settings, "local_accounts", (ACCOUNTS[0], LocalAccount("guest", "bad-\udcff"))
    )
    response = await sign_in(client, "admin", ADMIN_PASSWORD)
    assert response.status_code == 200
    me = await client.get("/whoami", headers=bearer(response.json()["token"]))
    assert me.status_code == 200


async def test_local_mode_never_accepts_a_neon_token(local_accounts, client, monkeypatch):
    # A valid Neon token (EdDSA) for an id that happens to be a local one.
    monkeypatch.setattr(settings, "neon_auth_url", NEON_AUTH_URL)
    for value in (token(), token(sub="local:admin", iss=ORIGIN)):
        assert (await client.get("/whoami", headers=bearer(value))).status_code == 401
    # Nor an unsigned one.
    unsigned = jwt.encode(
        jwt.decode(local_token(), options={"verify_signature": False}), None, algorithm="none"
    )
    assert (await client.get("/whoami", headers=bearer(unsigned))).status_code == 401


async def test_neon_mode_never_accepts_a_local_token(neon_auth, client, monkeypatch):
    monkeypatch.setattr(settings, "local_accounts", ACCOUNTS)
    value = local_token()
    response = await client.get("/whoami", headers=bearer(value))
    assert response.status_code == 401
    # Rejected from its header alone: no JWKS fetch.
    assert neon_auth == []


def test_local_mode_warns_at_startup(monkeypatch, caplog):
    template = "AUTH_MODE=local is for development only"
    assert template in SAFE_LOG_TEMPLATES
    for accounts in ((), ACCOUNTS):
        caplog.clear()
        monkeypatch.setattr(settings, "auth_mode", "local")
        monkeypatch.setattr(settings, "local_accounts", accounts)
        with caplog.at_level("WARNING", logger="interview_agent"):
            auth.warn_local_mode()
        assert [record.msg for record in caplog.records] == [template]
        assert ADMIN_PASSWORD not in caplog.text
    caplog.clear()
    monkeypatch.setattr(settings, "auth_mode", "neon")
    auth.warn_local_mode()
    assert caplog.records == []


# ---- the startup guard and required keys -------------------------------------


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://interview:interview@postgres:5432/interview",
        "postgresql+asyncpg://interview:interview@localhost:5432/interview",
        "postgresql+asyncpg://interview:interview@LocalHost./interview",
        "postgresql+asyncpg://interview:interview@127.0.0.1:5432/interview",
        "postgresql+asyncpg://interview:interview@127.0.0.2/interview",
        "postgresql+asyncpg://interview:interview@[::1]:5432/interview",
        "postgresql+asyncpg://interview:interview@host.docker.internal:5432/interview",
        "postgresql+asyncpg://interview:interview@/interview",
        "postgresql+asyncpg:///interview?host=/var/run/postgresql",
    ],
    ids=[
        "compose",
        "localhost",
        "localhost-variant",
        "loopback",
        "loopback-range",
        "ipv6-loopback",
        "docker-host",
        "socket",
        "socket-query",
    ],
)
@pytest.mark.parametrize("accounts", ["", "admin:synthetic-admin-password"])
def test_local_mode_starts_against_a_local_database(database_url, accounts):
    Settings(
        _env_file=None,
        AUTH_MODE="local",
        LOCAL_ACCOUNTS=accounts,
        INTERNAL_API_TOKEN="synthetic-internal-token",
        DATABASE_URL=database_url,
        **PROVIDER_KEYS,
    ).require_keys("api")


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://neondb_owner:SENSITIVE_DB_PASSWORD@ep-x.us-east-2.aws.neon.tech/neondb?ssl=verify-full",
        "postgresql+asyncpg://interview:SENSITIVE_DB_PASSWORD@db.example.com:5432/interview",
        "postgresql+asyncpg://interview:SENSITIVE_DB_PASSWORD@10.0.0.5:5432/interview",
        "postgresql+asyncpg://interview:SENSITIVE_DB_PASSWORD@localhost.example.com/interview",
        "postgresql+asyncpg://interview:SENSITIVE_DB_PASSWORD@/interview?host=db.example.com",
        "postgresql+asyncpg://interview:SENSITIVE_DB_PASSWORD@localhost:5432,db.example.com:5432/i",
        "not a url SENSITIVE_DB_PASSWORD",
    ],
    ids=[
        "neon",
        "remote-name",
        "private-ip",
        "lookalike",
        "query-host",
        "several-hosts",
        "garbage",
    ],
)
@pytest.mark.parametrize("accounts", ["", "admin:synthetic-admin-password"])
def test_local_mode_refuses_a_remote_database(database_url, accounts):
    configured = Settings(
        _env_file=None,
        AUTH_MODE="local",
        LOCAL_ACCOUNTS=accounts,
        INTERNAL_API_TOKEN="synthetic-internal-token",
        DATABASE_URL=database_url,
        **PROVIDER_KEYS,
    )
    with pytest.raises(RuntimeError, match="development only") as refused:
        configured.require_keys("api")
    assert "SENSITIVE_DB_PASSWORD" not in str(refused.value)
    assert "synthetic-admin-password" not in str(refused.value)
    # Neon mode, with the same database, is never affected.
    Settings(
        _env_file=None,
        AUTH_MODE="neon",
        LOCAL_ACCOUNTS=accounts,
        NEON_AUTH_URL=NEON_AUTH_URL,
        INTERNAL_API_TOKEN="synthetic-internal-token",
        DATABASE_URL=database_url,
        SENTRY_ENVIRONMENT="production",
        **PROVIDER_KEYS,
    ).require_keys("api")


@pytest.mark.parametrize(
    ("pghost", "local"),
    [
        (None, True),
        ("localhost", True),
        ("/var/run/postgresql", True),
        ("ep-x.us-east-2.aws.neon.tech", False),
        ("localhost,db.example.com", False),
    ],
    ids=["socket", "localhost", "socket-dir", "neon", "several-hosts"],
)
def test_a_url_without_a_host_is_as_local_as_pghost(monkeypatch, pghost, local):
    # asyncpg connects to PGHOST, not a socket, when the URL names no host.
    if pghost is None:
        monkeypatch.delenv("PGHOST", raising=False)
    else:
        monkeypatch.setenv("PGHOST", pghost)
    configured = Settings(
        _env_file=None,
        AUTH_MODE="local",
        DATABASE_URL="postgresql+asyncpg://interview:interview@/interview?ssl=require",
        **PROVIDER_KEYS,
    )
    if local:
        configured.require_keys("api")
    else:
        with pytest.raises(RuntimeError, match="host is not local"):
            configured.require_keys("api")
    # A host in the URL wins over PGHOST, as it does for asyncpg.
    monkeypatch.setenv("PGHOST", "ep-x.us-east-2.aws.neon.tech")
    Settings(
        _env_file=None,
        AUTH_MODE="local",
        DATABASE_URL="postgresql+asyncpg://interview:interview@localhost/interview",
        **PROVIDER_KEYS,
    ).require_keys("api")


@pytest.mark.parametrize(
    "environment",
    [
        {"DATABASE_URL": "postgresql+asyncpg://interview:interview@db.example.com/interview"},
        {"SENTRY_ENVIRONMENT": "production"},
    ],
    ids=["remote-database", "production"],
)
async def test_the_product_journey_stops_before_any_charge(monkeypatch, tmp_path, environment):
    # Its API runs with AUTH_MODE=local, which would refuse this setup only
    # after the database and the paid answer synthesis.
    path = Path(__file__).parents[1] / "scripts" / "verify_product_journey.py"
    spec = importlib.util.spec_from_file_location("verify_product_journey", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    for name, value in PROVIDER_KEYS.items() | environment.items():
        monkeypatch.setenv(name, value)

    async def charged(*_args):
        raise AssertionError("charged before the local-mode check")

    monkeypatch.setattr(script, "synthesize_answer", charged)
    output = tmp_path / "journey.json"
    with pytest.raises(ValueError, match="local DATABASE_URL in development"):
        await script.journey(Namespace(output=output, language="en", langsmith_project=None))
    assert not output.exists()


@pytest.mark.parametrize("environment", ["production", " Production "])
def test_local_mode_refuses_the_production_environment(environment):
    configured = Settings(
        _env_file=None,
        AUTH_MODE="local",
        SENTRY_ENVIRONMENT=environment,
        DATABASE_URL="postgresql+asyncpg://interview:interview@localhost:5432/interview",
        **PROVIDER_KEYS,
    )
    with pytest.raises(RuntimeError, match="SENTRY_ENVIRONMENT=production"):
        configured.require_keys("api")
    Settings(
        _env_file=None,
        AUTH_MODE="local",
        SENTRY_ENVIRONMENT="staging",
        DATABASE_URL="postgresql+asyncpg://interview:interview@localhost:5432/interview",
        **PROVIDER_KEYS,
    ).require_keys("api")


@pytest.mark.parametrize("role", ["api", "worker"])
def test_local_accounts_require_the_internal_token(role):
    # Without it the worker's evaluation trigger would get a silent 401.
    configured = Settings(
        _env_file=None,
        AUTH_MODE="local",
        LOCAL_ACCOUNTS="admin:synthetic-admin-password",
        **PROVIDER_KEYS,
    )
    with pytest.raises(RuntimeError, match=r"\.env: INTERNAL_API_TOKEN \(") as missing:
        configured.require_keys(role)
    message = str(missing.value)
    assert "openssl rand -hex 32" in message
    assert "AUTH_MODE=local" not in message
    assert "synthetic-admin-password" not in message
    Settings(
        _env_file=None,
        AUTH_MODE="local",
        LOCAL_ACCOUNTS="admin:synthetic-admin-password",
        INTERNAL_API_TOKEN="synthetic-internal-token",
        **PROVIDER_KEYS,
    ).require_keys(role)
    # Neon mode ignores LOCAL_ACCOUNTS: its own hint stays.
    with pytest.raises(RuntimeError, match=r"set AUTH_MODE=local\)"):
        Settings(
            _env_file=None,
            AUTH_MODE="neon",
            LOCAL_ACCOUNTS="admin:synthetic-admin-password",
            **PROVIDER_KEYS,
        ).require_keys(role)


def test_local_accounts_parse_into_username_password_pairs():
    configured = Settings(
        _env_file=None,
        LOCAL_ACCOUNTS=" admin:SECRET:with:colons , guest_2:ñandú-密码 ,, ",
        **PROVIDER_KEYS,
    )
    assert configured.local_accounts == (
        LocalAccount("admin", "SECRET:with:colons"),
        LocalAccount("guest_2", "ñandú-密码"),
    )
    assert "SECRET" not in repr(configured)
    assert Settings(_env_file=None, LOCAL_ACCOUNTS="").local_accounts == ()


@pytest.mark.parametrize(
    "value",
    [
        "admin-SECRET-PW",
        "admin:",
        ":SECRET-PW",
        "Admin:SECRET-PW",
        "ad min:SECRET-PW",
        "admin.x:SECRET-PW",
        "a" * 33 + ":SECRET-PW",
        "admin:SECRET-PW,admin:SECRET-PW-2",
        "admin:SECRET-PW,guest",
    ],
    ids=[
        "no-colon",
        "empty-password",
        "empty-username",
        "uppercase",
        "space",
        "dot",
        "too-long",
        "duplicate",
        "second-entry",
    ],
)
def test_invalid_local_accounts_never_print_the_password(value):
    with pytest.raises(ValidationError) as invalid:
        Settings(_env_file=None, LOCAL_ACCOUNTS=value)
    assert "LOCAL_ACCOUNTS" in str(invalid.value)
    assert "SECRET-PW" not in str(invalid.value)
    assert "SECRET-PW" not in repr(invalid.value)
