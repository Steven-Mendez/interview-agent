"""Exercise the installed SDK drain behavior with local controlled job handles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit.agents.worker import AgentServer

from interview_agent import agent


def configured_server(monkeypatch):
    options = []
    monkeypatch.setattr(agent.settings, "worker_drain_minutes", 7)
    monkeypatch.setattr(agent.cli, "run_app", options.append)
    agent.run()
    server = AgentServer.from_server_options(options[0])
    server._update_worker_status = AsyncMock()
    server._job_lifecycle_tasks = set()
    return server


@pytest.mark.asyncio
async def test_sdk_drain_uses_configured_deadline_and_rejects_new_jobs_until_existing_finish(
    monkeypatch,
):
    server = configured_server(monkeypatch)
    done = asyncio.Event()

    class Job:
        running_job = True

        async def join(self):
            await done.wait()
            self.running_job = False

    job = Job()
    server._proc_pool = SimpleNamespace(processes=[job])
    # The local pool is deliberately controlled; no LiveKit or provider network.
    server._simulation = True
    assert server._is_available()
    drain = asyncio.create_task(server.drain())
    await asyncio.sleep(0)
    assert server.draining and not server._is_available()
    assert server._drain_timeout == 420
    assert not drain.done() and job.running_job
    done.set()
    await asyncio.wait_for(drain, 1)
    assert not job.running_job
    assert server._update_worker_status.await_count == 1
    await server.drain()
    assert server._update_worker_status.await_count == 1


@pytest.mark.asyncio
async def test_sdk_drain_timeout_stays_unavailable_and_does_not_invent_job_completion(monkeypatch):
    server = configured_server(monkeypatch)
    never = asyncio.Event()

    class Job:
        running_job = True

        async def join(self):
            await never.wait()

    job = Job()
    server._proc_pool = SimpleNamespace(processes=[job])
    server._simulation = True
    with pytest.raises(TimeoutError):
        await server.drain(timeout=0.02)
    assert server.draining and not server._is_available()
    assert job.running_job
