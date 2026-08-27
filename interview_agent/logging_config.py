"""Adds a rotating file handler to the root logger so a full session can be
analyzed later. Console output is left untouched."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_HANDLER_TAG = "interview_agent_file_log"

# Attributes every LogRecord carries; anything beyond these came from `extra=`.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None))
) | {"message", "asctime", "taskName"}


def _rotated_name(default_name: str) -> str:
    """logs/server.log.1 → logs/server-1.log (keep the .log extension last)."""
    path = Path(default_name)
    if path.suffix[1:].isdigit():
        stem = Path(path.stem)  # "server.log"
        return str(path.with_name(f"{stem.stem}-{path.suffix[1:]}{stem.suffix}"))
    return default_name


class _ExtraFormatter(logging.Formatter):
    """Appends `extra={...}` fields as JSON. LiveKit puts the interesting
    payloads (transcripts, latency timings) in `extra`, which the default
    Formatter silently drops."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS
        }
        if not extras:
            return base
        try:
            return f"{base} | {json.dumps(extras, default=str, ensure_ascii=False)}"
        except (TypeError, ValueError):
            return f"{base} | {extras!r}"


def setup_file_logging(
    path: str = "logs/agent.log", level: int = logging.DEBUG
) -> Path:
    """Write all logs (DEBUG and up) to a rotating file (~5 MB, 3 backups)."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()

    # Idempotent: don't stack a new handler on hot-reload (`dev` mode).
    for h in root.handlers:
        if getattr(h, "_tag", None) == _HANDLER_TAG:
            return log_path

    handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    # Rotate to `server-1.log` instead of the default `server.log.1`, so
    # editors keep recognizing (and highlighting) rotated files as logs.
    handler.namer = _rotated_name
    handler.setLevel(level)
    handler._tag = _HANDLER_TAG  # type: ignore[attr-defined]
    handler.setFormatter(
        # No datefmt: the default asctime keeps milliseconds, without which
        # sub-second ordering (an STT final vs. the turn flush it raced) is
        # unreadable in the log.
        _ExtraFormatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    root.addHandler(handler)

    # asyncio logs a useless "Using selector" DEBUG line on every loop start.
    logging.getLogger("asyncio").setLevel(logging.INFO)

    # The root must allow DEBUG through for the handler to see it; the console
    # handler keeps its own (higher) level, so terminal output stays readable.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    return log_path
