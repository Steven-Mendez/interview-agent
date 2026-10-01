"""Configuration loaded and validated from `.env` via pydantic-settings."""

from __future__ import annotations

import os

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Content stays in Postgres. External traces contain metadata only.
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY", repr=False)
    langsmith_project: str = Field(default="interview-agent", alias="LANGSMITH_PROJECT")
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT"
    )
    metrics_retention_days: int = Field(default=365, alias="METRICS_RETENTION_DAYS", ge=1)
    metrics_detail_days: int = Field(default=30, alias="METRICS_DETAIL_DAYS", ge=1)
    closing_timeout_seconds: int = Field(default=20, alias="CLOSING_TIMEOUT_SECONDS", ge=5, le=60)

    # LiveKit: key/secret auth the Inference gateway (STT/TTS); the server URL
    # is where the worker and the browser join interview rooms.
    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")

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
for _name, _value in {
    "LIVEKIT_URL": settings.livekit_url,
    "LIVEKIT_API_KEY": settings.livekit_api_key,
    "LIVEKIT_API_SECRET": settings.livekit_api_secret,
}.items():
    if _value and not os.environ.get(_name):
        os.environ[_name] = _value
