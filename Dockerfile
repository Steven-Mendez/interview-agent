# syntax=docker/dockerfile:1
# One image, three services (see docker-compose.yml):
#   migrate: alembic upgrade head (one-shot)
#   app:     uvicorn interview_agent.server.app:app
#   worker:  python main.py start

FROM node:22-alpine AS webbuilder
WORKDIR /web

# corepack ships with the node:22 image and can pin/fetch the pnpm version
# declared in package.json's "packageManager" field; fall back to a global
# npm install if corepack can't verify/fetch it (e.g. signature service down).
RUN corepack enable && (corepack prepare pnpm@10 --activate || npm i -g pnpm)

# Lockfile-only layer first so this only reinstalls when deps actually change.
COPY web/package.json web/pnpm-lock.yaml ./
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


FROM python:3.12-slim

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
# Built SPA (TanStack Start, SPA mode): must match app.py's _FRONTEND_DIR.
COPY --from=webbuilder /web/dist/client web/dist/client

CMD ["python", "main.py", "start"]
