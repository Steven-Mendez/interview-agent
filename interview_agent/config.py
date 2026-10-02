"""Configuration loaded and validated from `.env` via pydantic-settings."""

from __future__ import annotations

import os
from typing import Annotated, Literal

import certifi
import langsmith
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

VERIFIED_STT_MODELS = frozenset({"assemblyai/universal-3-6-pro"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unrelated vars in .env
    )

    # LLM (OpenAI via LangGraph)
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    # The interviewer's model decides every voice turn: quality first, with its
    # audible latency measured. Planner and evaluator have their own below.
    interviewer_model: str = Field(
        default="gpt-6-astra",
        alias="INTERVIEWER_MODEL",
        description="Decision model for the realtime voice loop.",
    )
    # GPT-6 models take reasoning effort "low" and above (not none/minimal) and
    # reject temperature; pre-GPT-5 models take temperature instead.
    interviewer_reasoning_effort: str = Field(default="low", alias="INTERVIEWER_REASONING_EFFORT")
    interviewer_temperature: float = Field(default=0.7, alias="INTERVIEWER_TEMPERATURE")

    # Interview planner/evaluator: run once per interview with no latency
    # pressure, so quality-first models with high reasoning effort.
    planner_model: str = Field(default="gpt-6-astra", alias="PLANNER_MODEL")
    planner_reasoning_effort: str = Field(default="high", alias="PLANNER_REASONING_EFFORT")
    evaluator_model: str = Field(default="gpt-6-astra", alias="EVALUATOR_MODEL")
    evaluator_reasoning_effort: str = Field(default="high", alias="EVALUATOR_REASONING_EFFORT")

    # Postgres (from docker-compose.yml).
    database_url: str = Field(
        default="postgresql+asyncpg://interview:interview@localhost:5432/interview",
        alias="DATABASE_URL",
    )

    # PII retention: conversations (resume, job offer, transcript) are purged
    # once older than this. 0 disables the purge.
    retention_days: int = Field(default=30, alias="RETENTION_DAYS")

    # Interview session limits and wiring. The global cap clamps every
    # interview's own cap (derived from its interview_length); 25 is the
    # longest shipped length ("deep"), so the default lets every option run
    # to its planned length. Lowering it is allowed: the planner then plans
    # for the clamped time (see prompts.fit_length).
    interview_max_minutes: int = Field(default=25, alias="INTERVIEW_MAX_MINUTES")
    # Soft cap on simultaneous interviews: each one burns LLM/STT/TTS budget,
    # so /token returns 429 once this many are in progress.
    max_concurrent_interviews: int = Field(default=3, alias="MAX_CONCURRENT_INTERVIEWS")
    # End the interview after this long with no conversation items at all
    # (e.g. the candidate walked away leaving the tab open).
    interview_idle_minutes: int = Field(default=3, alias="INTERVIEW_IDLE_MINUTES")
    interview_reconnect_seconds: int = Field(
        default=10, alias="INTERVIEW_RECONNECT_SECONDS", ge=1, le=300
    )
    worker_drain_minutes: int = Field(default=30, alias="WORKER_DRAIN_MINUTES", ge=1, le=60)
    livekit_agent_name: str = Field(default="interviewer", alias="LIVEKIT_AGENT_NAME")
    # Where the worker reaches the FastAPI app to auto-trigger evaluation.
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")

    # STT via LiveKit Inference. The transcription language is pinned to the
    # interview language chosen in the in-app Settings screen; the TTS model
    # and voice come from the same screen (see interview_agent/voices.py).
    stt_model: str = Field(default="assemblyai/universal-3-6-pro", alias="STT_MODEL")

    # LangSmith: the key is the consent to send each interview's content (CV,
    # offer, plan, turns, prompts, answers, evaluation and audio); without one
    # nothing is traced. The app never deletes what LangSmith keeps.
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY", repr=False)
    langsmith_project: str = Field(default="interview-agent", alias="LANGSMITH_PROJECT")
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT"
    )
    # How long process manifests (each process's effective configuration) are kept.
    metrics_detail_days: int = Field(default=30, alias="METRICS_DETAIL_DAYS", ge=1)
    # Anonymous metrics over OTLP/HTTP (see otel_metrics); empty: none leave the
    # process. Read here: pydantic-settings never exports .env to os.environ.
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_exporter_otlp_headers: str = Field(
        default="", alias="OTEL_EXPORTER_OTLP_HEADERS", repr=False
    )
    closing_timeout_seconds: int = Field(default=20, alias="CLOSING_TIMEOUT_SECONDS", ge=5, le=60)
    # Error reports without interview content (see error_reporting); empty
    # DSN: nothing is reported.
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN", repr=False)
    sentry_environment: str = Field(default="development", alias="SENTRY_ENVIRONMENT")

    # LiveKit: key/secret auth the Inference gateway (STT/TTS); the server URL
    # is where the worker and the browser join interview rooms.
    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")

    # Accounts. "neon" (the default, so production never runs open by accident)
    # verifies Neon Auth JWTs against NEON_AUTH_URL's JWKS; "local" makes every
    # request the fixed user "local-dev", for development without a login.
    auth_mode: Literal["local", "neon"] = Field(default="neon", alias="AUTH_MODE")
    neon_auth_url: str = Field(default="", alias="NEON_AUTH_URL")
    # Shared secret of server-to-server calls (the worker's evaluation trigger,
    # the scheduled maintenance); empty rejects every such call.
    internal_api_token: str = Field(default="", alias="INTERNAL_API_TOKEN", repr=False)
    # JWT `sub` values with unlimited interviews and the runtime manifest. The
    # token's own `role` claim never grants anything.
    admin_user_ids: Annotated[list[str], NoDecode] = Field(default=[], alias="ADMIN_USER_IDS")
    # Interviews a non-admin account can ever start (deleting them gives
    # nothing back), and all non-admin accounts together per calendar month.
    lifetime_interviews_per_user: int = Field(default=3, alias="LIFETIME_INTERVIEWS_PER_USER", ge=0)
    guest_interviews_per_month: int = Field(default=2, alias="GUEST_INTERVIEWS_PER_MONTH", ge=0)
    # Browser origins allowed to call the API cross-origin; empty adds no CORS.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default=[], alias="CORS_ALLOWED_ORIGINS"
    )

    def require_keys(self) -> Settings:
        """Fail fast with a clear message if any required API key is missing."""
        missing = [
            name
            for name, value in (
                ("OPENAI_API_KEY", self.openai_api_key),
                ("LIVEKIT_API_KEY", self.livekit_api_key),
                ("LIVEKIT_API_SECRET", self.livekit_api_secret),
                ("LIVEKIT_URL", self.livekit_url),
                ("DATABASE_URL", self.database_url),
                *(
                    (
                        ("NEON_AUTH_URL", self.neon_auth_url),
                        ("INTERNAL_API_TOKEN", self.internal_api_token),
                    )
                    if self.auth_mode == "neon"
                    else ()
                ),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing variables in .env: {', '.join(missing)}.")
        return self

    @field_validator("stt_model")
    @classmethod
    def _verified_stt_model(cls, value):
        # New interviews only start on models whose drain contract (final
        # results plus provider finalized/closed signals) was observed.
        # Deepgram Flux returned finals without session.finalized/closed
        # within five seconds, so it stays a benchmark-only arm.
        if value not in VERIFIED_STT_MODELS:
            raise ValueError(
                f"STT_MODEL must be one of {sorted(VERIFIED_STT_MODELS)}; "
                "other models have no verified transcript drain contract"
            )
        return value

    @field_validator("admin_user_ids", "cors_allowed_origins", mode="before")
    @classmethod
    def _comma_separated(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _explicit_origins(cls, value: list[str]) -> list[str]:
        # Starlette reads "*" as every origin: the list must name each one.
        if "*" in value:
            raise ValueError("CORS_ALLOWED_ORIGINS must list explicit origins, never *")
        return value

    @field_validator("neon_auth_url")
    @classmethod
    def _without_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _check_bounds(self) -> Settings:
        if self.interview_reconnect_seconds > self.worker_drain_minutes * 60:
            raise ValueError("Reconnect time cannot exceed the worker drain period.")
        if not 0.0 <= self.interviewer_temperature <= 2.0:
            raise ValueError("INTERVIEWER_TEMPERATURE must be between 0.0 and 2.0.")
        return self


settings = Settings()

# pydantic-settings does NOT export .env values to the process environment,
# but LiveKit reads its credentials from os.environ — mirror them back here.
# LangChain's tracer takes its project from there too, not from configure().
_mirrored = {
    "LIVEKIT_URL": settings.livekit_url,
    "LIVEKIT_API_KEY": settings.livekit_api_key,
    "LIVEKIT_API_SECRET": settings.livekit_api_secret,
}
if settings.langsmith_api_key:
    _mirrored |= {
        "LANGSMITH_API_KEY": settings.langsmith_api_key,
        "LANGSMITH_PROJECT": settings.langsmith_project,
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
    }
for _name, _value in _mirrored.items():
    if _value and not os.environ.get(_name):
        os.environ[_name] = _value

# asyncpg verifies the server for ssl=verify-full (infra/neon's DATABASE_URL)
# only against PGSSLROOTCERT, never the system store, and refuses to connect
# without one. certifi's bundle is there on every host (API, worker image, CI);
# looser modes such as the local Postgres's never read it.
os.environ.setdefault("PGSSLROOTCERT", certifi.where())

# The only tracing switch: it overrides LANGSMITH_TRACING, so without a key
# nothing is traced even if the environment asks for it.
langsmith.configure(enabled=bool(settings.langsmith_api_key))
