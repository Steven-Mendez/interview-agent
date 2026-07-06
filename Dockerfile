# syntax=docker/dockerfile:1
# One image, three services (see docker-compose.yml):
#   migrate: alembic upgrade head (one-shot)
#   app:     uvicorn interview_agent.server.app:app
#   worker:  python main.py start

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
COPY frontend/ frontend/

CMD ["python", "main.py", "start"]
