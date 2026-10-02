"""User and quota counts as gauges for Grafana's users dashboard.

The API recounts them from the database every minute (account_metrics_loop,
started in the lifespan) and on POST /internal/maintenance. Counts only, by
fixed categories: never who.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select

from interview_agent import otel_metrics
from interview_agent.interview import db

logger = logging.getLogger("interview_agent.server")

ACCOUNT_METRICS_INTERVAL_SECONDS = 60
# Users seen within each window, by label.
ACTIVE_WINDOWS = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}


async def account_snapshot(sessionmaker, settings) -> dict[str, dict]:
    """Every accounts gauge with its values by label set, zeros included."""
    profile_admin = db.UserProfile.owner_id.in_(settings.admin_user_ids)
    async with sessionmaker() as session:
        admins, guests, *active = (
            await session.execute(
                select(
                    func.count().filter(profile_admin),
                    func.count().filter(~profile_admin),
                    *(
                        func.count().filter(db.UserProfile.last_seen_at >= func.now() - window)
                        for window in ACTIVE_WINDOWS.values()
                    ),
                ).select_from(db.UserProfile)
            )
        ).one()
        out_of_interviews = await session.scalar(
            select(func.count())
            .select_from(db.UserInterviewQuota)
            .where(
                db.UserInterviewQuota.owner_id.not_in(settings.admin_user_ids),
                db.UserInterviewQuota.interviews_used >= settings.lifetime_interviews_per_user,
            )
        )
        this_month = await db.guest_interviews_this_month(session)
    return {
        "users": {(("role", "guest"),): guests, (("role", "admin"),): admins},
        "active_users": {
            (("window", label),): count for label, count in zip(ACTIVE_WINDOWS, active, strict=True)
        },
        "guests_out_of_interviews": {(): int(out_of_interviews or 0)},
        "guest_interviews_this_month": {(): this_month},
        "guest_interviews_monthly_limit": {(): settings.guest_interviews_per_month},
    }


async def publish_account_metrics(sessionmaker, settings) -> None:
    """Recount and publish the gauges. Never raises: a failed recount only
    leaves the gauges at their last values until the next one."""
    try:
        snapshot = await account_snapshot(sessionmaker, settings)
    except Exception:
        logger.exception("account metrics failed; retrying next cycle")
        return
    for name, values in snapshot.items():
        otel_metrics.set_snapshot("accounts", name, values)


async def account_metrics_loop(sessionmaker, settings) -> None:
    """publish_account_metrics, at startup and then every minute."""
    while True:
        await publish_account_metrics(sessionmaker, settings)
        await asyncio.sleep(ACCOUNT_METRICS_INTERVAL_SECONDS)
