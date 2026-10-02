"""Error reports to Sentry without interview content.

An event keeps what locates a failure (exception type, stack frames, the
reviewed log template, method and path) and nothing that can carry a resume,
transcript, job offer or credential: exception messages, local variables,
log arguments, request bodies, headers, cookies, query strings, extras and
breadcrumbs never leave the process. The user is the opaque account id only.

Without SENTRY_DSN nothing is configured and every call here is a no-op.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk
from sentry_sdk.integrations import DidNotEnable
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from interview_agent.config import Settings
from interview_agent.log_templates import SAFE_LOG_TEMPLATES

_OMITTED = "technical event (content omitted)"

# Every category is set: a data_collection dict collects whatever it leaves out.
_DATA_COLLECTION = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "http_headers": {"request": {"mode": "off"}},
    "http_bodies": [],
    "url_query_params": {"mode": "off"},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    "stack_frame_variables": False,
    # Source lines around each frame are our code, not interview content.
    "frame_context_lines": 5,
}

# Without tracing these add no error data, only a path to prompts and answers.
_GEN_AI_INTEGRATIONS = (
    ("sentry_sdk.integrations.openai", "OpenAIIntegration"),
    ("sentry_sdk.integrations.langchain", "LangchainIntegration"),
    ("sentry_sdk.integrations.langgraph", "LanggraphIntegration"),
    ("sentry_sdk.integrations.openai_agents", "OpenAIAgentsIntegration"),
)


def _gen_ai_integrations() -> list[type]:
    found = []
    for module_name, class_name in _GEN_AI_INTEGRATIONS:
        try:
            module = importlib.import_module(module_name)
        except DidNotEnable:  # its library is not installed: nothing to disable
            continue
        found.append(getattr(module, class_name))
    return found


def _without_query(url: Any) -> Any:
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def before_send(event: dict, hint: dict) -> dict | None:
    """Strip an event down to what locates the failure; see the module docstring."""
    record = hint.get("log_record")
    if record is not None and record.process not in (None, os.getpid()):
        # LiveKit replays a job process's log records in the worker parent.
        # The job process reported it already, with its exception; the replay
        # has neither.
        return None
    for container in ("exception", "threads"):
        for value in (event.get(container) or {}).get("values") or ():
            if container == "exception":
                value["value"] = ""
            for frame in (value.get("stacktrace") or {}).get("frames") or ():
                frame.pop("vars", None)
    logentry = event.get("logentry")
    if logentry is not None:
        template = logentry.get("message")
        event["logentry"] = {"message": template if template in SAFE_LOG_TEMPLATES else _OMITTED}
    if "message" in event and event["message"] not in SAFE_LOG_TEMPLATES:
        del event["message"]
    request = event.get("request")
    if request is not None:
        event["request"] = {
            key: value
            for key, value in (
                ("method", request.get("method")),
                ("url", _without_query(request.get("url"))),
            )
            if value
        }
    event.pop("extra", None)
    event.pop("breadcrumbs", None)
    user = event.get("user")
    if isinstance(user, dict) and user.get("id") is not None:
        event["user"] = {"id": user["id"]}
    else:
        event.pop("user", None)
    return event


def configure(settings: Settings, component: str, *, transport: Transport | None = None) -> bool:
    """Start reporting this process's errors, once; False without SENTRY_DSN.

    `component` ("api" / "worker") tags every event. The transport replaces
    the network one in tests."""
    if not settings.sentry_dsn:
        return False
    if not sentry_sdk.is_initialized():
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            transport=transport,
            data_collection=_DATA_COLLECTION,
            max_request_body_size="never",
            include_local_variables=False,
            traces_sample_rate=0,
            integrations=[
                # Log lines never become breadcrumbs or Sentry logs; errors
                # (logger.error/exception) still become events.
                LoggingIntegration(level=None, event_level=logging.ERROR, sentry_logs_level=None)
            ],
            disabled_integrations=_gen_ai_integrations(),
            before_send=before_send,
            before_breadcrumb=lambda crumb, hint: None,
        )
    # The global scope, so every thread and request carries it.
    sentry_sdk.get_global_scope().set_tag("component", component)
    return True


def flush(timeout: float) -> None:
    """Send the queued events, waiting at most `timeout` seconds: a LiveKit job
    process exits without Sentry's own exit flush. A no-op when unconfigured."""
    sentry_sdk.flush(timeout=timeout)
