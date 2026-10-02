"""Effective process manifests; local factory verification never implies a provider call."""

from __future__ import annotations

import hashlib
import inspect
import json
import marshal
import os
import platform
import subprocess
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from sqlalchemy import text

from interview_agent.interview import db
from interview_agent.llm import build_chat_model, close_chat_model

EXPECTED_REVISION = "dad9ce0068bd"
ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    "planner_model",
    "planner_reasoning_effort",
    "interviewer_model",
    "interviewer_reasoning_effort",
    "interviewer_temperature",
    "evaluator_model",
    "evaluator_reasoning_effort",
    "stt_model",
    "interview_max_minutes",
    "interview_idle_minutes",
    "interview_reconnect_seconds",
    "closing_timeout_seconds",
    "retention_days",
    "metrics_detail_days",
    "worker_drain_minutes",
)
SOURCES = (
    "interview_agent/config.py",
    "interview_agent/llm.py",
    "interview_agent/agent.py",
    "interview_agent/interview/dialogue.py",
    "interview_agent/interview/evaluator.py",
    "interview_agent/interview/models.py",
    "interview_agent/interview/evaluation_contract.py",
    "interview_agent/server/routes.py",
    "interview_agent/server/evaluations.py",
    "interview_agent/closing.py",
    "interview_agent/stt_drain.py",
    "interview_agent/privacy.py",
    "interview_agent/runtime.py",
    "interview_agent/prompts.py",
    "interview_agent/voices.py",
    "pyproject.toml",
    "uv.lock",
)


async def process_manifest(settings, role: str, *, config=None, functions=()) -> dict:
    config = config or {}
    defaults = {name: settings.__class__.model_fields[name].default for name in FIELDS}
    effective = {name: getattr(settings, name) for name in FIELDS}
    factories = {}
    roles = (
        ("interviewer",)
        if role == "worker"
        else (("evaluator",) if role == "evaluator" else ("planner", "evaluator"))
    )
    for component in roles:
        chosen = (config.get("models") or {}).get(component, {})
        model = build_chat_model(
            settings,
            model=chosen.get("model", getattr(settings, component + "_model")),
            reasoning_effort=chosen.get(
                "reasoning_effort", getattr(settings, component + "_reasoning_effort")
            ),
            max_retries=1 if component == "interviewer" else 3,
            timeout_seconds=15 if component == "interviewer" else 120,
        )
        try:
            effective[component + "_model"] = model.model_name
            effective[component + "_reasoning_effort"] = chosen.get(
                "reasoning_effort", getattr(settings, component + "_reasoning_effort")
            )
            factories[component] = {
                "requested_model": model.model_name,
                "provider_reported_model": None,
                "reasoning": model.reasoning,
                "temperature": model.temperature,
                "responses_api": model.use_responses_api,
                "http_attempt_ceiling": model.root_async_client.max_retries + 1,
                "verification": "local_factory_only",
            }
        finally:
            await close_chat_model(model)
    packages = {}
    for package in (
        "livekit-agents",
        "langgraph",
        "langchain-openai",
        "openai",
        "langsmith",
        "sqlalchemy",
    ):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    try:
        commit, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    manifest = {
        "id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
        "role": role,
        "pid": os.getpid(),
        "python": platform.python_version(),
        "commit": commit,
        "working_tree_dirty": dirty,
        "loaded_defaults": defaults,
        "process_settings": {name: getattr(settings, name) for name in FIELDS},
        "effective_settings": effective,
        "factories": factories,
        "dependencies": packages,
        "disk_artifact_hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES
        },
        "loaded_callable_hashes": {
            function.__module__ + "." + function.__qualname__: hashlib.sha256(
                marshal.dumps(function.__code__)
            ).hexdigest()
            for function in functions
        },
        "worker_dispatch": settings.livekit_agent_name,
        "langsmith_configured": bool(settings.langsmith_api_key),
        "provider_calls_performed": False,
    }
    from interview_agent import prompts

    manifest["loaded_prompt_hashes"] = {
        name: hashlib.sha256(value.encode()).hexdigest()
        for name, value in vars(prompts).items()
        if name.isupper() and isinstance(value, str)
    }
    manifest["loaded_prompt_callable_hashes"] = {
        name: hashlib.sha256(marshal.dumps(value.__code__)).hexdigest()
        for name, value in vars(prompts).items()
        if inspect.isfunction(value) and value.__module__ == prompts.__name__
    }
    manifest["loaded_calibration_hash"] = hashlib.sha256(
        json.dumps(
            {
                "seniority": prompts.SENIORITY_CALIBRATION,
                "length": prompts.LENGTH_PROFILE,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    if role == "worker":
        # The entrypoint resolves this from the saved interview before building
        # the SDK session; process defaults may have changed since creation.
        manifest["effective_settings"]["stt_model"] = config.get("stt_model")
        manifest["voice"] = {
            key: config.get(key) for key in ("stt_model", "tts_model", "tts_voice", "language")
        }
        manifest["snapshot_config_version"] = config.get("config_version")
    return manifest


async def record_manifest(sessionmaker, manifest: dict, *, conversation_id=None):
    async with sessionmaker() as session:
        session.add(
            db.ProcessManifest(
                id=uuid.UUID(manifest["id"]),
                conversation_id=conversation_id,
                role=manifest["role"],
                snapshot=manifest,
            )
        )
        await session.commit()
    return manifest


async def validate_database_revision(sessionmaker):
    async with sessionmaker() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != EXPECTED_REVISION:
            raise RuntimeError("Database schema is not current: run `alembic upgrade head`")
    return revision
