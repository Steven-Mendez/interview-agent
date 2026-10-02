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
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from interview_agent import error_reporting, otel_metrics
from interview_agent.config import settings
from interview_agent.interview.db import create_engine_and_sessionmaker
from interview_agent.logging_config import setup_file_logging
from interview_agent.runtime import process_manifest, record_manifest, validate_database_revision
from interview_agent.server.evaluations import EvaluationRunner
from interview_agent.server.reconciliation import LifecycleSweeper
from interview_agent.server.retention import purge_expired
from interview_agent.server.routes import router

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIR = _REPO_ROOT / "web" / "dist" / "client"

_SHELL = "index.html"


class SpaStaticFiles(StaticFiles):
    """Serves the built SPA, falling back to the shell for unknown paths.

    Client-side routes (e.g. `/interviews/<uuid>`) aren't real files — a
    direct load or refresh must still get the app shell instead of a 404, so
    the router can take over and render the right view.
    """

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response(_SHELL, scope)
            raise


_PURGE_INTERVAL_SECONDS = 24 * 60 * 60
_METRICS_EXPORT_SECONDS = 5

logger = logging.getLogger("interview_agent.server")


async def _purge_loop(sessionmaker) -> None:
    """Retention (retention.purge_expired), once at startup and then daily.
    POST /api/internal/maintenance runs the same purge on demand."""
    while True:
        try:
            await purge_expired(sessionmaker, settings)
        except Exception:
            # Keep the loop alive: a transient DB outage should not
            # end retention for the rest of the process lifetime.
            logger.exception("retention purge failed; retrying next cycle")
        await asyncio.sleep(_PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Same rotating-file setup as the worker, in its own file. INFO on the
    # console: uvicorn only wires its own loggers, not the app's.
    logging.basicConfig(level=logging.INFO)  # no-op if handlers already exist
    log_path = setup_file_logging("logs/server.log")

    settings.require_keys("api")
    otel_metrics.configure(settings, "interview-agent-api")
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url)
    try:
        database_revision = await validate_database_revision(sessionmaker)
    except BaseException:
        await engine.dispose()
        raise
    logger.info(
        "server ready",
        extra={
            "log_file": str(log_path),
            "max_concurrent_interviews": settings.max_concurrent_interviews,
        },
    )

    app.state.sessionmaker = sessionmaker
    app.state.runtime_manifest = await process_manifest(
        settings, "api", functions=(lifespan.__wrapped__, _purge_loop, purge_expired)
    )
    app.state.runtime_manifest["database_revision"] = database_revision
    await record_manifest(sessionmaker, app.state.runtime_manifest)
    # POST /evaluate claims a row and hands it here; the run itself happens
    # in this process, off the request.
    app.state.evaluations = EvaluationRunner(sessionmaker)

    purge_task = asyncio.create_task(_purge_loop(sessionmaker))
    sweep_task = asyncio.create_task(
        LifecycleSweeper(sessionmaker, settings, app.state.evaluations).run()
    )
    if settings.retention_days <= 0:
        logger.info("retention purge disabled (RETENTION_DAYS=0)")

    try:
        yield
    finally:
        sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            await sweep_task
        if purge_task is not None:
            purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await purge_task
        # Before the engine goes: each cancelled run marks its row so the UI
        # offers a retry instead of a spinner over a run nobody is doing.
        await app.state.evaluations.shutdown()
        # The last samples leave with the process. Shutdown is their final
        # export and bounds itself, so a slow collector cannot hold the exit.
        await asyncio.to_thread(otel_metrics.shutdown, _METRICS_EXPORT_SECONDS * 1000)
        await engine.dispose()


def add_cors(application: FastAPI, origins: list[str]) -> None:
    """Let the listed browser origins call the API; none adds no middleware.

    Bearer tokens, not cookies: credentials stay off, and the origins are an
    explicit list, never "*". Retry-After is exposed so the browser can read
    when a 429 lifts."""
    if not origins:
        return
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Retry-After"],
        allow_credentials=False,
    )


# Before the app exists, so Sentry's Starlette/FastAPI integration wraps it.
error_reporting.configure(settings, "api")
app = FastAPI(title="interview-agent", lifespan=lifespan)
# At creation: Starlette refuses new middleware once the app has started.
add_cors(app, settings.cors_allowed_origins)
app.include_router(router, prefix="/api")
# Mounted last so /api/* wins over static files. SpaStaticFiles falls back to
# the shell for client-side routes so refreshes/deep links keep working.
# Backend-only dev without a `cd web && pnpm build` still gets the API.
if _FRONTEND_DIR.is_dir():
    app.mount("/", SpaStaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
else:
    logger.warning("web/dist/client missing; serving API only (run: cd web && pnpm build)")
