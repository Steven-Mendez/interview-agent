"""The installed SDK must discard project-enabled recording before startup errors."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from livekit import rtc
from livekit.agents import JobContext
from livekit.agents import job as sdk_job
from livekit.protocol import agent as agent_protocol

from interview_agent import agent


@pytest.mark.parametrize("failure", ["keys", "metadata"])
async def test_project_enabled_sdk_recording_cannot_upload_startup_content(monkeypatch, failure):
    setup_cloud = Mock(side_effect=AssertionError("Cloud recording must remain disabled"))
    discard_cloud = Mock()
    monkeypatch.setattr(sdk_job, "_setup_cloud_tracer", setup_cloud)
    monkeypatch.setattr(sdk_job, "_discard_cloud_tracer", discard_cloud)
    job = agent_protocol.Job(
        id="isolated-recording-test", enable_recording=True, metadata="{invalid-json"
    )
    ctx = JobContext(
        proc=SimpleNamespace(),
        info=SimpleNamespace(job=job, url="wss://privacy-test.livekit.cloud", fake_job=False),
        room=rtc.Room(),
        on_connect=Mock(),
        on_shutdown=Mock(),
        inference_executor=SimpleNamespace(),
    )
    monkeypatch.setattr(
        agent,
        "settings",
        SimpleNamespace(
            require_keys=Mock(
                side_effect=ValueError("SENSITIVE_STARTUP_ERROR") if failure == "keys" else None
            )
        ),
    )
    try:
        ctx._start_log_buffering()
        handler = ctx._early_log_handler
        assert handler is not None and handler in logging.getLogger().handlers
        logging.getLogger().warning("SENSITIVE_EARLY_LOG_CONTENT")
        assert handler.buffer
        if failure == "keys":
            with pytest.raises(ValueError):
                await agent.entrypoint(ctx)
        else:
            await agent.entrypoint(ctx)
        assert ctx._recording_initialized
        assert ctx._early_log_handler is None
        assert handler not in logging.getLogger().handlers
        assert ctx._telemetry_state is None
        await ctx._on_cleanup()
        setup_cloud.assert_not_called()
        discard_cloud.assert_called_once_with(job.id)
    finally:
        ctx._stop_log_buffering()
        ctx._tempdir.cleanup()
