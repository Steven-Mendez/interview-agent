"""Metadata-only console and file logs; content belongs in SQL content tables."""

from __future__ import annotations

import logging
import math
import re
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


# livekit/rtc/_ffi_client.py forwards every native (Rust) log record through
# one Python call with this template and (target, line, module_path, message)
# as arguments, so source_line alone is the same for every native event.
_FFI_TEMPLATE = "%s:%s:%s - %s"
_FFI_MAX_LEN = 120
# A target is a Rust path: segments of letters, digits, "_", ".", "/" and "-"
# joined by "::". A single ":" (as in "https:" or "host:port"), spaces and "@"
# never match, so URLs, e-mail style identities and free text are dropped.
_FFI_TARGET = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*(?:::[A-Za-z_][A-Za-z0-9_./-]*)*")
# A module path is stricter: what Rust's module_path!() produces.
_FFI_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*")
# Room names and identities often embed generated IDs (UUIDs, hex tokens, long
# numbers); code paths do not, so anything shaped like one is dropped.
_ID_LIKE = re.compile(r"[0-9A-Fa-f-]{8,}|\d{5,}")
_FFI_MAX_LINE = 1_000_000
# What a filtered native record carries instead of its message.
_FFI_OMITTED = "native livekit event (message omitted)"


def _safe_rust_path(value: object, pattern: re.Pattern[str]) -> str | None:
    if type(value) is not str or not 0 < len(value) <= _FFI_MAX_LEN:
        return None
    if not pattern.fullmatch(value) or "//" in value or ".." in value:
        return None
    if any(any(c.isdigit() for c in run) for run in _ID_LIKE.findall(value)):
        return None
    return value


def _safe_line(value: object) -> int | None:
    if type(value) is int and 0 < value <= _FFI_MAX_LINE:
        return value
    return None


def _ffi_location(
    target: object, line: object, module_path: object
) -> tuple[str | None, int | None, str | None]:
    return (
        _safe_rust_path(target, _FFI_TARGET),
        _safe_line(line),
        _safe_rust_path(module_path, _FFI_MODULE),
    )


def _is_ffi_record(record: logging.LogRecord) -> bool:
    return (
        record.name == "livekit"
        and record.msg == _FFI_TEMPLATE
        and type(record.args) is tuple
        and len(record.args) == 4
    )


def _ffi_location_fields(record: logging.LogRecord) -> list[str]:
    """Code location of a forwarded native record; its message is never read."""
    if record.name != "livekit":
        return []
    if _is_ffi_record(record):
        # Not filtered yet (no FfiLocationFilter on this path): read the args.
        target, line, module_path = _ffi_location(*record.args[:3])
    else:
        # Filtered in the process that logged it. A job process ships its
        # records to the worker already expanded (args None), so only these
        # attributes survive; they are validated again because any code can
        # set attributes with these names.
        target, line, module_path = _ffi_location(
            record.__dict__.get("ffi_target"),
            record.__dict__.get("ffi_line"),
            record.__dict__.get("ffi_module"),
        )
    fields = []
    if target is not None:
        fields.append("ffi_target=" + target)
    if line is not None:
        fields.append(f"ffi_line={line}")
    if module_path is not None:
        fields.append("ffi_module=" + module_path)
    return fields


class FfiLocationFilter(logging.Filter):
    """Keep a native record's validated code location and drop its message.

    Runs on the "livekit" logger, so it sees the record before any handler.
    That matters in a LiveKit job process: its queue handler expands the
    message into the record and sends it to the worker over a socket, after
    which the location can no longer be told apart from the message. Here the
    location becomes plain attributes, which survive that copy and pickle,
    and the message (room names, identities, URLs) is replaced so it never
    leaves the process, not even towards error reporting.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_ffi_record(record):
            target, line, module_path = _ffi_location(*record.args[:3])
            if target is not None:
                record.ffi_target = target
            if line is not None:
                record.ffi_line = line
            if module_path is not None:
                record.ffi_module = module_path
            record.msg = _FFI_OMITTED
            record.args = None
        return True


def install_ffi_location_filter() -> None:
    """Attach FfiLocationFilter to the "livekit" logger once per process."""
    livekit_logger = logging.getLogger("livekit")
    if not any(isinstance(f, FfiLocationFilter) for f in livekit_logger.filters):
        livekit_logger.addFilter(FfiLocationFilter())


# Installed at import: a job process imports this module (through the agent
# module that defines its entrypoint) before it joins any room, and it never
# runs the worker's logging setup, so import time is the one hook it shares.
install_ffi_location_filter()


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
    participate in conversation cascade deletion. Native LiveKit records that
    the FFI client forwards also carry their validated Rust code location
    (ffi_target, ffi_line, ffi_module) while their message stays omitted.
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
        fields.extend(_ffi_location_fields(record))
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
    install_ffi_location_filter()
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
