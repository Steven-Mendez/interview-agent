"""Error reports reach Sentry without interview content, offline.

The real SDK runs with a synthetic DSN and an in-memory transport: envelopes
are captured here and nothing leaves the process.
"""

from __future__ import annotations

import json
import logging
import os
import warnings

import pytest
import sentry_sdk
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sentry_sdk.transport import Transport

from interview_agent import error_reporting
from interview_agent.config import Settings
from interview_agent.server.auth import current_user

CANARY = "CANARY_CV_TRANSCRIPT_SECRET_5e7d1"
DSN = "https://synthetic-key@o0.ingest.sentry.io/0"
SAFE_TEMPLATE = "planning failed for %s"

logger = logging.getLogger("interview_agent.server")


class MemoryTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope) -> None:
        self.envelopes.append(envelope)

    def events(self) -> list[dict]:
        sentry_sdk.flush()
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == "event"
        ]

    def serialized(self) -> str:
        sentry_sdk.flush()
        return "".join(envelope.serialize().decode() for envelope in self.envelopes)


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, SENTRY_DSN=DSN, **overrides)


@pytest.fixture
def reported():
    """Sentry configured with a MemoryTransport; the client is closed and the
    scopes reset afterwards, so no other test reports anywhere."""
    transport = MemoryTransport()
    with sentry_sdk.isolation_scope():
        assert error_reporting.configure(settings(), "api", transport=transport)
        try:
            yield transport
        finally:
            sentry_sdk.get_client().close()
            sentry_sdk.get_global_scope().set_client(None)
            sentry_sdk.get_global_scope().remove_tag("component")


async def test_failing_route_reports_one_event_without_body_headers_query_or_message(reported):
    app = FastAPI()

    @app.post("/api/fail", dependencies=[Depends(current_user)])
    async def fail(request: Request):
        await request.json()
        raise ValueError(CANARY)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/fail?q={CANARY}",
            json={"resume": CANARY},
            headers={"Authorization": f"Bearer {CANARY}", "Cookie": f"session={CANARY}"},
        )

    assert response.status_code == 500
    events = reported.events()
    assert len(events) == 1
    (event,) = events
    assert CANARY not in reported.serialized()
    assert event["exception"]["values"][-1]["type"] == "ValueError"
    assert event["exception"]["values"][-1]["value"] == ""
    assert event["request"] == {"method": "POST", "url": "http://test/api/fail"}
    # The request's user is the local developer: a local id names nobody.
    assert "user" not in event
    assert event["tags"]["component"] == "api"


def test_logged_errors_keep_only_reviewed_templates(reported):
    try:
        raise RuntimeError(CANARY)
    except RuntimeError:
        logger.exception(SAFE_TEMPLATE, CANARY)
        logger.error("unsafe %s", CANARY, extra={"resume": CANARY})

    events = reported.events()
    assert [event["logentry"]["message"] for event in events] == [
        SAFE_TEMPLATE,
        "technical event (content omitted)",
    ]
    assert all(set(event["logentry"]) == {"message"} for event in events)
    assert events[0]["exception"]["values"][-1]["type"] == "RuntimeError"
    assert CANARY not in reported.serialized()


def test_log_records_replayed_from_another_process_are_not_reported_again(reported):
    # What LiveKit's worker parent replays from a job process: the formatted
    # text, no exception, the job process's pid.
    record = logger.makeRecord(
        logger.name, logging.ERROR, __file__, 0, f"planning failed for {CANARY}", None, None
    )
    record.process = os.getpid() + 1
    logging.getLogger(record.name).callHandlers(record)
    logger.error(SAFE_TEMPLATE, CANARY)

    (event,) = reported.events()
    assert event["logentry"] == {"message": SAFE_TEMPLATE}
    assert CANARY not in reported.serialized()


def test_local_variables_of_the_raising_frame_are_not_sent(reported):
    def parse(resume: str) -> None:
        excerpt = resume[:200]
        raise ValueError(len(excerpt))

    try:
        parse(CANARY)
    except ValueError:
        sentry_sdk.capture_exception()

    (event,) = reported.events()
    frames = event["exception"]["values"][-1]["stacktrace"]["frames"]
    assert frames and all("vars" not in frame for frame in frames)
    assert CANARY not in reported.serialized()


def test_user_is_reduced_to_its_opaque_id(reported):
    sentry_sdk.set_user(
        {"id": "user-synthetic", "email": CANARY, "username": CANARY, "ip_address": "10.0.0.1"}
    )
    sentry_sdk.capture_message(CANARY)

    (event,) = reported.events()
    assert event["user"] == {"id": "user-synthetic"}
    assert "message" not in event
    assert CANARY not in reported.serialized()


def test_before_send_strips_content_fields():
    event = {
        "message": CANARY,
        "logentry": {"message": CANARY, "params": [CANARY], "formatted": CANARY},
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "module": "builtins",
                    "value": CANARY,
                    "stacktrace": {"frames": [{"function": "f", "vars": {"x": CANARY}}]},
                }
            ]
        },
        "threads": {"values": [{"stacktrace": {"frames": [{"vars": {"x": CANARY}}]}}]},
        "request": {
            "method": "GET",
            "url": f"https://app.example/api/x?q={CANARY}#{CANARY}",
            "query_string": f"q={CANARY}",
            "headers": {"Authorization": CANARY},
            "cookies": {"session": CANARY},
            "data": {"resume": CANARY},
            "env": {"REMOTE_ADDR": CANARY},
        },
        "extra": {"sys.argv": [CANARY]},
        "breadcrumbs": {"values": [{"message": CANARY}]},
        "user": {"id": "user-synthetic", "email": CANARY},
    }

    result = error_reporting.before_send(event, {})

    assert CANARY not in json.dumps(result)
    assert result["exception"]["values"][0] == {
        "type": "ValueError",
        "module": "builtins",
        "value": "",
        "stacktrace": {"frames": [{"function": "f"}]},
    }
    assert result["logentry"] == {"message": "technical event (content omitted)"}
    assert result["request"] == {"method": "GET", "url": "https://app.example/api/x"}
    assert result["user"] == {"id": "user-synthetic"}


def test_without_dsn_nothing_is_configured_or_sent():
    assert not error_reporting.configure(Settings(_env_file=None), "api")
    assert not sentry_sdk.is_initialized()
    sentry_sdk.capture_message(CANARY)  # a no-op without a client


def test_configuration_emits_no_deprecation_warning():
    transport = MemoryTransport()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            assert error_reporting.configure(settings(), "worker", transport=transport)
        finally:
            sentry_sdk.get_client().close()
            sentry_sdk.get_global_scope().set_client(None)
            sentry_sdk.get_global_scope().remove_tag("component")
    assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]
