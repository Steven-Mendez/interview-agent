"""Retention purge, shared by the server's daily loop and POST /internal/maintenance."""

from __future__ import annotations

import logging

from interview_agent.interview import db

logger = logging.getLogger("interview_agent.server")


async def purge_expired(sessionmaker, settings) -> int:
    """Process manifests older than METRICS_DETAIL_DAYS, then (PII) interviews
    older than RETENTION_DAYS; Postgres CASCADE removes their milestones,
    messages and evaluations. Interview quotas are never touched. Returns how
    many interviews were deleted."""
    async with sessionmaker() as session:
        await db.expire_manifests(session, settings.metrics_detail_days)
        deleted = (
            await db.delete_conversations_older_than(session, settings.retention_days)
            if settings.retention_days > 0
            else []
        )
    logger.info(
        "retention purge done",
        extra={"deleted": len(deleted), "days": settings.retention_days},
    )
    return len(deleted)
