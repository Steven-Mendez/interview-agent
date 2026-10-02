# syntax=docker/dockerfile:1
# One Dockerfile, two images; each builds only the stages it needs:
#   --target app   the API serving the built SPA. docker-compose.yml runs it
#                  as `app` (uvicorn) and as `migrate` (alembic upgrade head).
#   --target worker, or no --target: the LiveKit worker (python main.py start).
#                  It is the last stage on purpose: LiveKit Cloud builds the
#                  repository's Dockerfile without a target, so the default
#                  image must be the worker, and BuildKit skips the Node stage
#                  it never copies from.

FROM node:24-alpine AS webbuilder
WORKDIR /web

# corepack ships with the node:24 image and fetches the pnpm version pinned
# in package.json's "packageManager" field; fall back to a global npm install
# of the same major if corepack can't verify/fetch it (e.g. signature service
# down).
RUN corepack enable && (corepack prepare pnpm@11 --activate || npm i -g pnpm@11)

# Lockfile-only layer first so this only reinstalls when deps actually change.
# pnpm-workspace.yaml holds allowBuilds: pnpm 11 fails the install on any
# dependency build script it was not told to run or skip.
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

COPY web/ ./
RUN pnpm build


FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# Use the image's Python, compile bytecode for faster startup, and copy (not
# hardlink) from the cache mount, which lives on a different filesystem.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies only (--no-install-project): the project itself runs from the
# /app source tree, so this layer is only invalidated by lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project


# Everything the two Python images share: the venv, the source and the
# tiktoken files. No CMD: each image below says what it runs.
FROM python:3.12-slim AS base

RUN adduser --disabled-password --gecos "" --home /home/appuser appuser

WORKDIR /app
COPY --from=builder /app/.venv .venv

# PYTHONPATH: the package is imported from /app (not installed in the venv),
# so uvicorn/alembic console scripts can resolve it from any CWD.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    HOME=/home/appuser \
    TIKTOKEN_CACHE_DIR=/home/appuser/.cache/tiktoken

RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs /home/appuser
USER appuser

# Bake the tiktoken BPE files into the image: langchain-openai tokenizes with
# tiktoken, which otherwise downloads them on first use from an OpenAI blob
# that intermittently 503s. cl100k_base = embeddings; o200k_base = GPT-5-family.
RUN python -c "\
import tiktoken; \
tiktoken.get_encoding('cl100k_base'); \
tiktoken.get_encoding('o200k_base')"

COPY pyproject.toml uv.lock alembic.ini main.py ./
COPY alembic/ alembic/
COPY interview_agent/ interview_agent/


FROM base AS app
# Built SPA (TanStack Start, SPA mode): must match app.py's _FRONTEND_DIR.
COPY --from=webbuilder /web/dist/client web/dist/client
CMD ["uvicorn", "interview_agent.server.app:app", "--host", "0.0.0.0", "--port", "8000"]


# Last, so a build without --target produces it (see the header).
FROM base AS worker
CMD ["python", "main.py", "start"]
