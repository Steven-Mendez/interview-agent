"""How the browser signs in: the mode, and the dev login of local accounts.

GET /auth/config is public: the SPA asks it which sign-in to show. The dev
login exists only with AUTH_MODE=local and LOCAL_ACCOUNTS (development; config
refuses it with a remote database); elsewhere its route is a 404.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from interview_agent.server import auth

logger = logging.getLogger("interview_agent.server")

router = APIRouter()


@router.get("/auth/config")
async def get_auth_config():
    """none: no login (local developer); local: the dev login; neon: Neon Auth."""
    return {"mode": auth.auth_mode()}


class LocalSignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.post("/auth/local/sign-in")
async def local_sign_in(request: Request):
    """A token for a local account. Unknown usernames and wrong passwords get
    the same answer; neither is ever logged."""
    if auth.auth_mode() != "local":
        raise HTTPException(status_code=404, detail="Not Found")
    # Validated here rather than as a body parameter: FastAPI's 422 would
    # echo the input (the password) back, and fail on what it cannot encode.
    try:
        body = LocalSignIn.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid sign-in request") from None
    user = auth.verify_local_credentials(body.username, body.password)
    if user is None:
        logger.info("local sign-in rejected")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, expires_at = auth.issue_local_token(user)
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": {"id": user.id, "name": user.name},
    }
