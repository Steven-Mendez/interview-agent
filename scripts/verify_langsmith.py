"""Live LangSmith checks.

Default: export one synthetic interview the way production does — the
interview trace (REST root and worker run with content, a finished LLM leaf
with tokens and cost, a feedback score) and its voice session trace (LiveKit's
default shape through the production InterviewSpanProcessor: the job root with
the audio recording and `ls_modality: audio`, a turn below it). Checks both,
requests their deletion with the production LangSmithDeletionAPI and polls
until LangSmith confirms them gone. Synthetic text only, no database, no model
calls. Costs two base traces.

  uv run python scripts/verify_langsmith.py [--wait-minutes 15]

Inspect a real interview's traces (read-only): tree, content, LLM calls, cost,
audio, and whether the deletion bookkeeping covers them.

  uv run python scripts/verify_langsmith.py --inspect <interview-id>
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from langsmith import Client
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider

from interview_agent.config import settings
from interview_agent.observability import (
    GRAPH_VERSION,
    TraceLinks,
    dotted_order,
    feedback_project,
    interview_trace_id,
    otel_run_id,
    trace_client,
    usage_metadata,
)
from interview_agent.privacy import LangSmithDeletionAPI, api_root
from interview_agent.voice_tracing import InterviewSpanProcessor, VoiceTracing

METADATA = {"source": "verify_langsmith", "graph_version": GRAPH_VERSION}


def export(client: Client, trace_id: uuid.UUID, started: datetime) -> None:
    common = {
        "trace_id": trace_id,
        "project_name": settings.langsmith_project,
        "extra": {"metadata": METADATA},
    }
    process, leaf = otel_run_id(), uuid.uuid4()
    root_order = dotted_order(started, trace_id)
    process_order = dotted_order(started, process, root_order)
    client.create_run(
        name="interview",
        id=trace_id,
        run_type="chain",
        inputs={"resume_markdown": "Synthetic CV", "job_offer": "Synthetic offer"},
        start_time=started,
        dotted_order=root_order,
        **common,
    )
    client.create_run(
        name="worker",
        id=process,
        parent_run_id=trace_id,
        run_type="chain",
        inputs={"plan": {"milestones": ["Synthetic topic"]}},
        start_time=started,
        dotted_order=process_order,
        **common,
    )
    client.create_run(
        name="interviewer",
        id=leaf,
        parent_run_id=process,
        run_type="llm",
        inputs={"messages": [{"role": "user", "content": "Synthetic answer"}]},
        outputs={"messages": [{"role": "assistant", "content": "Synthetic question"}]},
        start_time=started,
        dotted_order=dotted_order(started, leaf, process_order),
        end_time=datetime.now(UTC),
        **{
            **common,
            "extra": {
                "metadata": {
                    **METADATA,
                    "ls_provider": "openai",
                    "ls_model_name": "gpt-6-astra",
                    "usage_metadata": usage_metadata(
                        {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                        0.015,
                    ),
                }
            },
        },
    )
    ended = datetime.now(UTC)
    client.update_run(process, end_time=ended, outputs={"transcript": ["Synthetic"]})
    client.update_run(trace_id, end_time=ended, outputs={"score": 88})
    client.create_feedback(
        trace_id=trace_id,
        key="evaluation_score",
        score=88.0,
        extend_trace_retention=False,
        stop_after_attempt=1,
        start_time=started,
        **feedback_project(client, settings),
    )


def export_voice_session(trace_id: uuid.UUID) -> uuid.UUID:
    """What the worker does with LiveKit's spans, minus LiveKit itself."""
    processor = InterviewSpanProcessor(
        api_key=settings.langsmith_api_key,
        project=settings.langsmith_project,
        endpoint=api_root(settings.langsmith_endpoint) + "otel/v1/traces",
    )
    provider = TracerProvider()
    provider.add_span_processor(processor)
    voice = VoiceTracing(processor, provider.get_tracer("interview_agent"))
    telemetry = SimpleNamespace(
        trace_id=trace_id,
        thread_id=f"verify-{trace_id}",
        dimensions=lambda: dict(METADATA),
        annotate=lambda **_: None,
        follow_otel_trace=lambda _: None,
    )
    livekit = provider.get_tracer("livekit-agents")
    root = livekit.start_span("job_entrypoint")
    with otel_trace.use_span(root, end_on_exit=True):
        session_trace = voice.session_trace_id()
        voice.follow(telemetry, session_trace)
        with (
            livekit.start_as_current_span("agent_session"),
            livekit.start_as_current_span("user_turn", attributes={"lk.user_transcript": "Hi"}),
        ):
            pass
        # LiveKit delivers this report when the session closes; a synthetic one.
        recording = Path(tempfile.mkdtemp()) / "audio.ogg"
        recording.write_bytes(b"OggS" + bytes(2048))
        processor.attach_session_report(
            SimpleNamespace(
                chat_history=None,
                audio_recording_path=recording,
                audio_recording_started_at=time.time(),
            ),
            telemetry.thread_id,
        )
    provider.shutdown()
    return session_trace


async def poll(api, trace_id, project_id, since, *, expect_absent, deadline):
    while True:
        absent = await api.absent(trace_id, project_id, since)
        if absent == expect_absent:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(15)


def trace_runs(client: Client, trace_id: uuid.UUID) -> list:
    return list(client.list_runs(project_name=settings.langsmith_project, trace_id=trace_id))


def print_tree(runs: list) -> None:
    children: dict = {}
    for run in runs:
        children.setdefault(run.parent_run_id, []).append(run)

    def walk(parent, depth):
        for run in sorted(children.get(parent, []), key=lambda r: r.start_time):
            cost = f" ${run.total_cost:.4f}" if run.total_cost else ""
            tokens = f" {run.total_tokens} tok" if run.total_tokens else ""
            print(f"{'  ' * depth}- {run.name} [{run.run_type}]{tokens}{cost}")
            walk(run.id, depth + 1)

    walk(None, 0)


def audio_of(client: Client, run_id: uuid.UUID) -> tuple[dict | None, dict]:
    run = client.read_run(run_id)
    metadata = (run.extra or {}).get("metadata", {})
    return next(iter((run.attachments or {}).values()), None), metadata


async def main(args) -> int:
    if not settings.langsmith_api_key:
        print("LANGSMITH_API_KEY is not configured; nothing was sent.")
        return 2
    trace_id = interview_trace_id(uuid.uuid4())  # synthetic interview
    started = datetime.now(UTC)
    client = trace_client(settings)
    await asyncio.to_thread(export, client, trace_id, started)
    session_trace = await asyncio.to_thread(export_voice_session, trace_id)
    print(f"exported synthetic traces {trace_id} (interview) and {session_trace} (voice)")
    links = TraceLinks(settings)
    for _ in range(12):  # a new project appears with its first trace
        try:
            await links.resolve()
            break
        except LookupError:
            await asyncio.sleep(5)
    print("open:", links.url(trace_id), links.url(session_trace))
    reader = Client(api_key=settings.langsmith_api_key, api_url=settings.langsmith_endpoint)
    api = LangSmithDeletionAPI(settings)
    deadline = time.monotonic() + args.wait_minutes * 60
    traces = (trace_id, session_trace)
    try:
        project_id = await api.project_id(settings.langsmith_project)
        for identifier in traces:
            visible = await poll(
                api, identifier, project_id, started, expect_absent=False, deadline=deadline
            )
            print("visible in LangSmith:", identifier, visible)
            if not visible:
                return 1
        await asyncio.sleep(10)  # the root (with audio) is the last run ingested
        audio, metadata = await asyncio.to_thread(audio_of, reader, session_trace)
        print("voice root audio:", audio is not None, "modality:", metadata.get("ls_modality"))
        print("linked to the interview:", metadata.get("interview_trace_id") == str(trace_id))
        if audio is None:
            return 1
        for identifier in traces:
            await api.submit(identifier, project_id)
        print("deletion accepted; waiting for LangSmith to process it…")
        gone = True
        for identifier in traces:
            gone &= await poll(
                api, identifier, project_id, started, expect_absent=True, deadline=deadline
            )
        print("deletion verified" if gone else "still present at the deadline")
        if gone:
            status = httpx.get(audio["presigned_url"]).status_code
            print("audio link after deletion: HTTP", status, "(a signed link expires on its own)")
        return 0 if gone else 1
    finally:
        await api.close()


async def inspect(conversation_id: uuid.UUID) -> int:
    from sqlalchemy import select

    from interview_agent.interview import db

    interview = interview_trace_id(conversation_id)
    engine, sessionmaker = db.create_engine_and_sessionmaker(settings.database_url)
    try:
        async with sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(db.ExternalTrace)
                    .where(db.ExternalTrace.conversation_id == conversation_id)
                    .order_by(db.ExternalTrace.created_at)
                )
            )
    finally:
        await engine.dispose()
    client = Client(api_key=settings.langsmith_api_key, api_url=settings.langsmith_endpoint)
    total = 0.0
    for row in rows:
        kind = "interview" if row.id == interview else "voice"
        print(f"\n== {kind} trace {row.id}: {row.state}, deleted after {row.expires_at:%Y-%m-%d}")
        runs = trace_runs(client, row.id)
        print(f"{len(runs)} runs; all in this trace:", {r.trace_id for r in runs} == {row.id})
        print_tree(runs)
        llms = [r for r in runs if r.run_type == "llm" and (r.total_tokens or 0) > 0]
        cost = float(sum(r.total_cost or 0 for r in llms))
        total += cost
        print("priced LLM calls:", len(llms), "cost:", cost)
        root = next((r for r in runs if r.parent_run_id is None), None)
        if root is not None and kind == "voice":
            audio, metadata = audio_of(client, root.id)
            print(
                "audio:",
                audio is not None,
                metadata.get("ls_audio_attach_status"),
                "modality:",
                metadata.get("ls_modality"),
                "thread:",
                metadata.get("thread_id"),
            )
        elif root is not None:
            print("root inputs:", sorted(root.inputs or {}), "outputs:", sorted(root.outputs or {}))
    print(f"\ntotal priced LLM cost across traces: {total:.4f}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-minutes", type=float, default=15)
    parser.add_argument("--inspect", type=uuid.UUID)
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(inspect(parsed.inspect) if parsed.inspect else main(parsed)))
