"""Who is calling: Neon Auth users (JWT), the local developer, or another server.

Neon Auth signs its JWTs with EdDSA and publishes the public keys at
`{NEON_AUTH_URL}/.well-known/jwks.json`; the token's issuer and audience are
that URL's origin. Admins are listed in ADMIN_USER_IDS by `sub`: the token's
own `role` claim is never read. Server-to-server calls (the worker's
evaluation trigger, the scheduled maintenance) carry INTERNAL_API_TOKEN in
X-Internal-Token instead of a user.
"""

from __future__ import annotations

import asyncio
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

import jwt
import sentry_sdk
from fastapi import Depends, HTTPException, Request

from interview_agent.config import settings

LOCAL_USER_ID = "local-dev"


@dataclass(frozen=True)
class User:
    id: str
    email: str | None
    name: str | None


def is_admin(user: User) -> bool:
    return user.id in settings.admin_user_ids


def _unauthorized() -> HTTPException:
    # Generic on purpose: neither the token nor why it failed is echoed back.
    return HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


_jwks_client: jwt.PyJWKClient | None = None
# A token can name any kid, so an unknown one may refresh the keys (Neon
# rotated them) at most this often; otherwise anyone could make every
# request fetch the JWKS.
_JWKS_REFRESH_SECONDS = 60
_jwks_refresh_lock = threading.Lock()
_jwks_refreshed_at: float | None = None


def _jwks() -> jwt.PyJWKClient:
    """One client per JWKS URL, so its key cache outlives the request."""
    global _jwks_client, _jwks_refreshed_at
    uri = f"{settings.neon_auth_url}/.well-known/jwks.json"
    if _jwks_client is None or _jwks_client.uri != uri:
        _jwks_client = jwt.PyJWKClient(uri, cache_keys=True, lifespan=300, timeout=5)
        _jwks_refreshed_at = None
    return _jwks_client


def _may_refresh_jwks() -> bool:
    global _jwks_refreshed_at
    with _jwks_refresh_lock:
        now = time.monotonic()
        if _jwks_refreshed_at is not None and now - _jwks_refreshed_at < _JWKS_REFRESH_SECONDS:
            return False
        _jwks_refreshed_at = now
        return True


def _signing_key(token: str) -> jwt.PyJWK:
    # Checked before any lookup, so a malformed or foreign header costs no fetch.
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if header.get("alg") != "EdDSA" or not isinstance(kid, str) or not 0 < len(kid) <= 128:
        raise jwt.InvalidTokenError("Unexpected token header")
    client = _jwks()
    # Blocking: the JWKS fetch (when the cache is cold) is a urllib request.
    key = client.match_kid(client.get_signing_keys(), kid)
    if key is None and _may_refresh_jwks():
        key = client.match_kid(client.get_signing_keys(refresh=True), kid)
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
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
    )


async def current_user(request: Request) -> User:
    user = await _authenticate(request)
    # This request's error reports name the account by its opaque id only
    # (a no-op without Sentry).
    sentry_sdk.set_user({"id": user.id})
    return user


async def _authenticate(request: Request) -> User:
    if settings.auth_mode == "local":
        return User(LOCAL_USER_ID, None, "Local developer")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip() or not settings.neon_auth_url:
        raise _unauthorized()
    try:
        claims = await asyncio.to_thread(_verify, token.strip())
    except Exception:
        raise _unauthorized() from None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise _unauthorized()
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
