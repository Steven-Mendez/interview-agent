"""Metadata-only console and file logs; content belongs in SQL content tables."""

from __future__ import annotations

import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path

from interview_agent.log_templates import SAFE_LOG_TEMPLATES

_INFRA_LOG_TEMPLATES = frozenset(
    {
        "Application startup complete.",
        "Application shutdown complete.",
        "Waiting for application startup.",
        "Waiting for application shutdown.",
        "Shutting down",
        "Started server process [%d]",
        "Finished server process [%d]",
    }
)
_HANDLER_TAG = "interview_agent_file_log"
_NUMERIC_FIELDS = frozenset(
    {
        "duration",
        "latency",
        "attempt",
        "bytes",
        "max_concurrent_interviews",
        "pending_tasks",
        "timeout",
        "status_code",
    }
)
_COMPONENTS = frozenset(
    {
        "interview_agent",
        "livekit",
        "uvicorn",
        "pdfminer",
        "httpx",
        "httpcore",
        "openai",
        "asyncio",
        "langsmith",
        "langchain",
        "sqlalchemy",
    }
)


def _rotated_name(default_name: str) -> str:
    path = Path(default_name)
    if path.suffix[1:].isdigit():
        stem = Path(path.stem)
        return str(path.with_name(f"{stem.stem}-{path.suffix[1:]}{stem.suffix}"))
    return default_name


class MetadataFormatter(logging.Formatter):
    """Never stringify runtime arguments, extras or exception messages.

    Only reviewed literal application templates and bounded numeric fields are
    emitted. Session identifiers stay in SQL, not in log files that cannot
    participate in conversation cascade deletion.
    """

    def format(self, record: logging.LogRecord) -> str:
        component = record.name.split(".")[0]
        if component not in _COMPONENTS:
            component = "other"
        template = record.msg if type(record.msg) is str else None
        message = (
            template
            if (
                (component == "interview_agent" and template in SAFE_LOG_TEMPLATES)
                or (component == "uvicorn" and template in _INFRA_LOG_TEMPLATES)
            )
            else "technical event (content omitted)"
        )
        # Templates intentionally remain unexpanded: arguments can be CV text,
        # credentials, filenames, provider error bodies or linked interview IDs.
        fields = []
        for name in sorted(_NUMERIC_FIELDS):
            value = record.__dict__.get(name)
            if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e12:
                fields.append(f"{name}={value}")
        if record.exc_info and isinstance(record.exc_info, tuple):
            error = record.exc_info[1]
            category = next(
                (
                    kind.__name__
                    for kind in (TimeoutError, ConnectionError, ValueError, TypeError, OSError)
                    if isinstance(error, kind)
                ),
                "Exception",
            )
            fields.append("exception_type=" + category)
        fields.append(
            "source_line=" + (str(record.lineno) if type(record.lineno) is int else "unknown")
        )
        return f"{self.formatTime(record)} {record.levelname} {component} {message} | " + " ".join(
            fields
        )


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        Path(self.baseFilename).chmod(0o600)
        return stream


def protect_log_handlers() -> None:
    """Cover console handlers installed by the pinned CLI or ASGI server."""
    root = logging.getLogger()
    formatter = MetadataFormatter()
    # Uvicorn/LiveKit configure non-propagating console handlers before startup.
    # Protect those outputs too; lowering the root level alone misses them.
    handlers = set(root.handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers.update(logger.handlers)
    for handler in handlers:
        handler.setFormatter(formatter)


def setup_file_logging(
    path: str | Path | None = "logs/agent.log", level: int = logging.INFO
) -> Path | None:
    """Install metadata formatters on existing outputs and a private rotating file.

    No path (LOG_DIR empty) means console only. A directory that cannot be
    created or a file that cannot be opened (a read-only or ephemeral host
    filesystem) also leaves the console as the only output, with a warning,
    rather than keeping the process from starting: the console still carries
    the same metadata-only lines.
    """
    root = logging.getLogger()
    protect_log_handlers()
    for handler in root.handlers:
        if getattr(handler, "_tag", None) == _HANDLER_TAG:
            return Path(handler.baseFilename)
    if path is None:
        return None
    log_path = Path(path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = PrivateRotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        # The path is not logged: it is configuration, and this message must
        # stay a reviewed template (see log_templates).
        logging.getLogger(__name__).warning(
            "file logging unavailable: log directory not writable; console only"
        )
        return None
    handler.namer = _rotated_name
    handler.setLevel(level)
    handler._tag = _HANDLER_TAG
    handler.setFormatter(MetadataFormatter())
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return log_path
