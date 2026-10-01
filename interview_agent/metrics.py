"""Atomic anonymous rollups with bounded relative-error percentile histograms."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import ARRAY, Integer, Text, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert

from interview_agent.interview import db

HISTOGRAM_BASE = 1.05
DIMENSIONS = frozenset(
    {
        "graph_version",
        "config_version",
        "language",
        "seniority",
        "length",
        "model",
        "resolved_model",
        "reasoning_effort",
        "price_version",
        "farewell_status",
        "stt_model",
        "tts_model",
        "provider",
        "status_code",
        "error_type",
        "source",
        "action",
        "audio_output",
    }
)
FILTERS = frozenset({"graph_version", "language", "seniority", "length", "model"})
_NAMESPACE = uuid.UUID("38d33355-284c-44f0-85f5-cd7245785dfa")


def safe_dimensions(dimensions: dict, *, detail=False) -> dict:
    allowed = DIMENSIONS | ({"trace_id"} if detail else set())
    return {
        key: value
        for key, value in dimensions.items()
        if key in allowed and isinstance(value, (str, int, bool)) and len(str(value)) <= 128
    }


def histogram_bucket(value: float) -> str:
    if value == 0:
        return "z"
    exponent = math.floor(math.log(abs(value), HISTOGRAM_BASE))
    return f"{'p' if value > 0 else 'n'}{exponent}"


def bucket_midpoint(bucket: str) -> float:
    if bucket == "z":
        return 0.0
    midpoint = HISTOGRAM_BASE ** (int(bucket[1:]) + 0.5)
    return midpoint if bucket[0] == "p" else -midpoint


def percentile(histogram: dict[str, int], quantile: float) -> float | None:
    count = sum(histogram.values())
    if count == 0:
        return None
    target = max(1, math.ceil(count * quantile))
    cumulative = 0
    for bucket, frequency in sorted(histogram.items(), key=lambda pair: bucket_midpoint(pair[0])):
        cumulative += frequency
        if cumulative >= target:
            return bucket_midpoint(bucket)
    return None


async def record_metric(session, event: db.MetricEvent) -> None:
    """Detail and its aggregate commit together; duplicates cannot be double counted."""
    if event.conversation_id is not None:
        exists = await session.scalar(
            select(db.Conversation.id)
            .where(db.Conversation.id == event.conversation_id)
            .with_for_update(read=True, key_share=True)
        )
        if exists is None:
            await session.rollback()
            return
    trace_id = (event.dimensions or {}).get("trace_id")
    if trace_id:
        try:
            identifier = uuid.UUID(str(trace_id))
        except ValueError:
            identifier = None
        if identifier:
            trace = await session.scalar(
                select(db.ExternalTrace).where(db.ExternalTrace.id == identifier).with_for_update()
            )
            if trace and (
                trace.state != "active"
                or trace.expires_at <= await session.scalar(select(func.clock_timestamp()))
            ):
                await session.rollback()
                return
    now = event.created_at or datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    dimensions = safe_dimensions(event.dimensions)
    fingerprint = json.dumps(
        [day.isoformat(), event.component, event.name, dimensions], sort_keys=True
    )
    aggregate_id = uuid.uuid5(_NAMESPACE, fingerprint)
    value = event.value if event.value is not None and math.isfinite(event.value) else None
    event.value = value
    event.dimensions = safe_dimensions(event.dimensions, detail=True)
    event.created_at = now
    session.add(event)
    bucket = histogram_bucket(value) if value is not None else None
    statement = insert(db.MetricAggregate).values(
        id=aggregate_id,
        bucket_date=day,
        dimensions=dimensions,
        component=event.component,
        name=event.name,
        count=int(value is not None),
        unknown_count=int(value is None),
        total=value or 0,
        minimum=value,
        maximum=value,
        histogram={bucket: 1} if bucket else {},
    )
    table = db.MetricAggregate
    histogram = table.histogram
    if bucket:
        frequency = func.coalesce(table.histogram[bucket].astext.cast(Integer), 0) + 1
        histogram = func.jsonb_set(
            table.histogram, cast([bucket], ARRAY(Text)), func.to_jsonb(frequency)
        )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.id],
            set_={
                "count": table.count + statement.excluded.count,
                "unknown_count": table.unknown_count + statement.excluded.unknown_count,
                "total": table.total + statement.excluded.total,
                "minimum": func.least(table.minimum, statement.excluded.minimum),
                "maximum": func.greatest(table.maximum, statement.excluded.maximum),
                "histogram": histogram,
            },
        )
    )
    await session.commit()


async def purge_metrics(session, detail_days: int = 30, aggregate_days: int = 365) -> None:
    from interview_agent.privacy import retire_traces

    await retire_traces(session, expired_only=True)
    now = await session.scalar(select(func.clock_timestamp()))
    await session.execute(
        delete(db.MetricEvent).where(db.MetricEvent.created_at < now - timedelta(days=detail_days))
    )
    await session.execute(
        delete(db.ProcessManifest).where(
            db.ProcessManifest.created_at < now - timedelta(days=detail_days)
        )
    )
    await session.execute(
        delete(db.MetricAggregate).where(
            db.MetricAggregate.bucket_date < now - timedelta(days=aggregate_days)
        )
    )
    await session.commit()


def apply_filters(statement, table, filters: dict):
    for key, value in filters.items():
        if key in FILTERS and value:
            statement = statement.where(table.dimensions[key].astext == value)
    return statement


async def metrics_report(session, days: int, filters: dict) -> dict:
    statement = apply_filters(
        select(db.MetricAggregate).where(
            db.MetricAggregate.bucket_date
            >= datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=days - 1),
        ),
        db.MetricAggregate,
        filters,
    )
    rows = (await session.scalars(statement)).all()
    grouped = {}
    facets = {key: set() for key in FILTERS}
    for row in rows:
        for key in facets:
            if key in row.dimensions:
                facets[key].add(str(row.dimensions[key]))
        key = json.dumps([row.component, row.name, row.dimensions], sort_keys=True)
        group = grouped.setdefault(
            key,
            {
                "component": row.component,
                "name": row.name,
                "dimensions": row.dimensions,
                "count": 0,
                "unknown_count": 0,
                "total": 0.0,
                "minimum": None,
                "maximum": None,
                "histogram": Counter(),
            },
        )
        group["count"] += row.count
        group["unknown_count"] += row.unknown_count
        group["total"] += row.total
        for name, reducer in (("minimum", min), ("maximum", max)):
            values = [v for v in (group[name], getattr(row, name)) if v is not None]
            group[name] = reducer(values) if values else None
        group["histogram"].update(row.histogram)
    items = []
    for group in grouped.values():
        histogram = group.pop("histogram")
        group["mean"] = group["total"] / group["count"] if group["count"] else None
        group["p50"] = percentile(histogram, 0.5)
        group["p95"] = percentile(histogram, 0.95)
        group["series_id"] = hashlib.sha256(
            json.dumps(
                [group["component"], group["name"], group["dimensions"]], sort_keys=True
            ).encode()
        ).hexdigest()[:16]
        items.append(group)
    return {
        "days": days,
        "items": sorted(
            items, key=lambda item: (item["component"], item["name"], item["series_id"])
        ),
        "facets": {key: sorted(values) for key, values in facets.items()},
        "percentiles": {
            "method": "logarithmic_histogram",
            "relative_bucket_width": HISTOGRAM_BASE - 1,
        },
    }
