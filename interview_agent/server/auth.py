"""Who is calling: Neon Auth users (JWT), local accounts, or another server.

Neon Auth signs its JWTs with EdDSA and publishes the public keys at
`{NEON_AUTH_URL}/.well-known/jwks.json`; the token's issuer and audience are
that URL's origin. Only a token that fails verification is a 401 (the
browser signs in again); keys that cannot be fetched are a 503. Admins are
listed in ADMIN_USER_IDS by `sub`: the token's own `role` claim is never
read. Server-to-server calls (the worker's evaluation trigger, the scheduled
maintenance) carry INTERNAL_API_TOKEN in X-Internal-Token instead of a user.

AUTH_MODE=local is development only (config refuses it with a remote database):
without LOCAL_ACCOUNTS every request is the local developer; with them, users
sign in (auth_routes) and the API verifies its own HS256 tokens, never Neon's.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import jwt
import sentry_sdk
from fastapi import Depends, HTTPException, Request

from interview_agent import otel_metrics
from interview_agent.config import settings

logger = logging.getLogger("interview_agent.server")

LOCAL_USER_ID = "local-dev"
LOCAL_ACCOUNT_PREFIX = "local:"
LOCAL_TOKEN_ISSUER = "interview-agent-local"
LOCAL_TOKEN_LIFETIME = timedelta(hours=12)
# Compared against when the username is unknown, so the time a sign-in takes
# does not tell which usernames exist.
_NO_ACCOUNT_DIGEST = hashlib.sha256(b"interview-agent-no-such-account").digest()


@dataclass(frozen=True)
class User:
    id: str
    email: str | None
    name: str | None


def is_admin(user: User) -> bool:
    return user.id in settings.admin_user_ids


def auth_provider(user: User) -> Literal["neon", "local"]:
    """Who vouched for the user: the API itself (the local developer and the
    local accounts) or Neon Auth."""
    if user.id == LOCAL_USER_ID or user.id.startswith(LOCAL_ACCOUNT_PREFIX):
        return "local"
    return "neon"


def _unauthorized() -> HTTPException:
    # Generic on purpose: neither the token nor why it failed is echoed back.
    return HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unavailable() -> HTTPException:
    # Neon Auth's keys could not be fetched: the token may well be valid, so
    # the browser must not take this for a sign-out.
    return HTTPException(status_code=503, detail="Authentication is temporarily unavailable")


def _rejected(
    reason: Literal["missing_token", "invalid_token", "keys_unavailable"],
) -> HTTPException:
    """The error for a user request that is refused, counted by reason."""
    otel_metrics.record("auth", "rejected", 1, {"reason": reason})
    return _unavailable() if reason == "keys_unavailable" else _unauthorized()


class _KeysUnavailableError(Exception):
    """The JWKS could not be fetched and no earlier copy can answer."""


_jwks_client: jwt.PyJWKClient | None = None
# A token can name any kid, so an unknown one may refresh the keys (Neon
# rotated them) at most this often; otherwise anyone could make every
# request fetch the JWKS. A failed fetch is retried no sooner either.
_JWKS_REFRESH_SECONDS = 60
_jwks_refresh_lock = threading.Lock()
_jwks_fetch_lock = threading.Lock()
_jwks_refreshed_at: float | None = None
# The last keys fetched successfully. PyJWKClient forgets its copy once the
# lifespan is over, fetch or no fetch: kept here, an unreachable JWKS keeps
# verifying the keys it already published instead of refusing everyone.
_jwks_keys: list[jwt.PyJWK] | None = None
_jwks_failed_at: float | None = None
# Clock skew between Neon Auth and the API: a token minted a moment ahead of
# this clock (iat) or expiring as it arrives (exp) is still accepted.
_JWT_LEEWAY_SECONDS = 30


def _jwks() -> jwt.PyJWKClient:
    """One client per JWKS URL, so its key cache outlives the request."""
    global _jwks_client, _jwks_refreshed_at, _jwks_keys, _jwks_failed_at
    uri = f"{settings.neon_auth_url}/.well-known/jwks.json"
    if _jwks_client is None or _jwks_client.uri != uri:
        _jwks_client = jwt.PyJWKClient(uri, cache_keys=True, lifespan=300, timeout=5)
        _jwks_refreshed_at = _jwks_keys = _jwks_failed_at = None
    return _jwks_client


def _may_refresh_jwks() -> bool:
    global _jwks_refreshed_at
    with _jwks_refresh_lock:
        now = time.monotonic()
        if _jwks_refreshed_at is not None and now - _jwks_refreshed_at < _JWKS_REFRESH_SECONDS:
            return False
        _jwks_refreshed_at = now
        return True


def _fetch_keys(client: jwt.PyJWKClient, *, refresh: bool = False) -> list[jwt.PyJWK]:
    global _jwks_keys, _jwks_failed_at
    try:
        # Blocking: the JWKS fetch (when the cache is cold) is a urllib request.
        keys = client.get_signing_keys(refresh=refresh)
    except Exception:
        # Unreachable, timed out, or an answer without keys: none of it says
        # anything about the token. The error itself is never logged.
        _jwks_failed_at = time.monotonic()
        logger.warning("Neon Auth JWKS fetch failed")
        raise _KeysUnavailableError from None
    _jwks_keys, _jwks_failed_at = keys, None
    return keys


def _current_keys(client: jwt.PyJWKClient) -> list[jwt.PyJWK]:
    # One fetch at a time: with keys in hand the other requests use them
    # meanwhile; without, they wait for its outcome rather than fetch again.
    if _jwks_keys is None:
        _jwks_fetch_lock.acquire()
    elif not _jwks_fetch_lock.acquire(blocking=False):
        return _jwks_keys
    try:
        failed_at = _jwks_failed_at
        if failed_at is not None and time.monotonic() - failed_at < _JWKS_REFRESH_SECONDS:
            # A fetch just failed: no request waits on another timeout yet.
            if _jwks_keys is None:
                raise _KeysUnavailableError
            return _jwks_keys
        try:
            return _fetch_keys(client)
        except _KeysUnavailableError:
            if _jwks_keys is None:
                raise
            return _jwks_keys
    finally:
        _jwks_fetch_lock.release()


def _signing_key(token: str) -> jwt.PyJWK:
    # Checked before any lookup, so a malformed or foreign header costs no fetch.
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if header.get("alg") != "EdDSA" or not isinstance(kid, str) or not 0 < len(kid) <= 128:
        raise jwt.InvalidTokenError("Unexpected token header")
    client = _jwks()
    key = client.match_kid(_current_keys(client), kid)
    if key is None and _may_refresh_jwks():
        key = client.match_kid(_fetch_keys(client, refresh=True), kid)
    elif key is None and _jwks_failed_at is not None:
        # Possibly a rotated key the unreachable JWKS would have listed.
        raise _KeysUnavailableError
    if key is None:
        raise jwt.InvalidTokenError("Unknown signing key")
    return key


def _verify(token: str) -> dict:
    parts = urlsplit(settings.neon_auth_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    signing_key = _signing_key(token)
    return jwt.decode(
        token,
        signing_key,
        algorithms=["EdDSA"],
        issuer=origin,
        audience=origin,
        leeway=_JWT_LEEWAY_SECONDS,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
    )


def auth_mode() -> Literal["none", "local", "neon"]:
    """How users sign in: Neon Auth, local accounts, or not at all."""
    if settings.auth_mode == "neon":
        return "neon"
    return "local" if settings.local_accounts else "none"


def warn_local_mode() -> None:
    if settings.auth_mode == "local":
        logger.warning("AUTH_MODE=local is for development only")


def _local_signing_key() -> bytes:
    # Derived from the accounts themselves: tokens survive restarts and
    # --reload, and changing any password invalidates every token. The shared
    # secret (required with accounts) keeps a token from being an offline
    # oracle for guessing the other accounts' passwords.
    accounts = ",".join(
        f"{account.username}:{account.password}" for account in settings.local_accounts
    )
    return hashlib.sha256(
        b"interview-agent-local-jwt\0"
        + settings.internal_api_token.encode("utf-8", "surrogatepass")
        + b"\0"
        + accounts.encode("utf-8", "surrogatepass")
    ).digest()


def _digest(password: str) -> bytes:
    # surrogatepass: a lone surrogate is a wrong password, not an encoding error.
    return hashlib.sha256(password.encode("utf-8", "surrogatepass")).digest()


def verify_local_credentials(username: str, password: str) -> User | None:
    """The local account these credentials sign in as, if any."""
    expected = next(
        (account.password for account in settings.local_accounts if account.username == username),
        None,
    )
    matches = hmac.compare_digest(
        _digest(password), _digest(expected) if expected is not None else _NO_ACCOUNT_DIGEST
    )
    if expected is None or not matches:
        return None
    return User(LOCAL_ACCOUNT_PREFIX + username, None, username)


def issue_local_token(user: User) -> tuple[str, datetime]:
    """A signed token for a local account and when it expires."""
    issued = datetime.now(UTC).replace(microsecond=0)
    expires = issued + LOCAL_TOKEN_LIFETIME
    claims = {
        "iss": LOCAL_TOKEN_ISSUER,
        "aud": LOCAL_TOKEN_ISSUER,
        "sub": user.id,
        "name": user.name,
        "iat": issued,
        "exp": expires,
    }
    return jwt.encode(claims, _local_signing_key(), algorithm="HS256"), expires


def _verify_local(token: str) -> User:
    claims = jwt.decode(
        token,
        _local_signing_key(),
        algorithms=["HS256"],
        issuer=LOCAL_TOKEN_ISSUER,
        audience=LOCAL_TOKEN_ISSUER,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
    )
    subject = claims["sub"]
    username = subject.removeprefix(LOCAL_ACCOUNT_PREFIX) if isinstance(subject, str) else ""
    # The account must still be listed, not just have been when it signed in.
    if subject != LOCAL_ACCOUNT_PREFIX + username or not any(
        account.username == username for account in settings.local_accounts
    ):
        raise jwt.InvalidTokenError("Unknown local account")
    return User(subject, None, username)


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def current_user(request: Request) -> User:
    user = await _authenticate(request)
    # This request's error reports name the account by its opaque id only
    # (a no-op without Sentry).
    sentry_sdk.set_user({"id": user.id})
    return user


async def _authenticate(request: Request) -> User:
    mode = auth_mode()
    if mode == "none":
        return User(LOCAL_USER_ID, None, "Local developer")
    token = _bearer_token(request)
    if token is None:
        raise _rejected("missing_token")
    if mode == "local":
        try:
            return _verify_local(token)
        except Exception:
            raise _rejected("invalid_token") from None
    if not settings.neon_auth_url:
        raise _rejected("invalid_token")
    try:
        claims = await asyncio.to_thread(_verify, token)
    except _KeysUnavailableError:
        raise _rejected("keys_unavailable") from None
    except Exception:
        raise _rejected("invalid_token") from None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise _rejected("invalid_token")
    email, name = claims.get("email"), claims.get("name")
    return User(
        subject,
        email if isinstance(email, str) else None,
        name if isinstance(name, str) else None,
    )


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _internal_token_valid(request: Request) -> bool:
    configured = settings.internal_api_token
    presented = request.headers.get("x-internal-token", "")
    # An unset token rejects everything, an empty header included.
    return bool(configured) and hmac.compare_digest(presented.encode(), configured.encode())


async def internal_caller(request: Request) -> None:
    if not _internal_token_valid(request):
        raise HTTPException(status_code=401, detail="Internal token required")


async def internal_caller_or_user(request: Request) -> User | None:
    """None for a server with a valid internal token, else the signed-in user.

    The user is resolved here rather than through Depends so a server call
    needs no JWT; an override of current_user (the route tests) still applies.
    """
    if _internal_token_valid(request):
        return None
    resolve = request.app.dependency_overrides.get(current_user, current_user)
    return await resolve(request)
