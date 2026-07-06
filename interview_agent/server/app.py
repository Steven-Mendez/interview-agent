"""FastAPI application factory.

Run with: uv run uvicorn interview_agent.server.app:app --port 8000
Schema is applied separately with `uv run alembic upgrade head` (not here).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from qdrant_client import AsyncQdrantClient

from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.db import create_engine_and_sessionmaker
from interview_agent.logging_config import setup_file_logging
from interview_agent.server.routes import router

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

_PURGE_INTERVAL_SECONDS = 24 * 60 * 60

logger = logging.getLogger("interview_agent.server")


async def _purge_loop(sessionmaker, qdrant: AsyncQdrantClient) -> None:
    """PII retention: drop interviews older than RETENTION_DAYS, once at
    startup and then daily. Postgres CASCADE removes milestones, messages
    and evaluations; the matching Qdrant points go with them."""
    while True:
        try:
            async with sessionmaker() as session:
                deleted = await db.delete_conversations_older_than(
                    session, settings.retention_days
                )
            if deleted:
                await rag.delete_resume_points(qdrant, settings, deleted)
            logger.info(
                "retention purge done",
                extra={"deleted": len(deleted), "days": settings.retention_days},
            )
        except Exception:
            # Keep the loop alive: a transient DB/Qdrant outage should not
            # end retention for the rest of the process lifetime.
            logger.exception("retention purge failed; retrying next cycle")
        await asyncio.sleep(_PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Same rotating-file setup as the worker, in its own file. INFO on the
    # console: uvicorn only wires its own loggers, not the app's.
    logging.basicConfig(level=logging.INFO)  # no-op if handlers already exist
    log_path = setup_file_logging("logs/server.log")

    settings.require_keys()
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    await rag.ensure_collection(qdrant, settings)
    logger.info(
        "server ready",
        extra={
            "log_file": str(log_path),
            "qdrant": settings.qdrant_url,
            "max_concurrent_interviews": settings.max_concurrent_interviews,
        },
    )

    app.state.sessionmaker = sessionmaker
    app.state.qdrant = qdrant
    app.state.embeddings = rag.build_embeddings(settings)

    purge_task: asyncio.Task | None = None
    if settings.retention_days > 0:
        purge_task = asyncio.create_task(_purge_loop(sessionmaker, qdrant))
    else:
        logger.info("retention purge disabled (RETENTION_DAYS=0)")

    try:
        yield
    finally:
        if purge_task is not None:
            purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await purge_task
        await qdrant.close()
        await engine.dispose()


app = FastAPI(title="interview-agent", lifespan=lifespan)
app.include_router(router)
# Mounted last so /interviews/* wins over static files.
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
