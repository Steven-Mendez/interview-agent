"""Configuration loaded and validated from `.env` via pydantic-settings."""

from __future__ import annotations

import os

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unrelated vars in .env
    )

    # LLM (OpenAI via LangGraph)
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    # The realtime voice model: it drives the interviewer, where
    # time-to-first-token matters most. Planner and evaluator have their own
    # quality-first models below.
    interviewer_model: str = Field(
        default="gpt-5.4-mini",
        alias="INTERVIEWER_MODEL",
        description="Low-latency chat model for the realtime voice loop.",
    )
    # GPT-5-family models take reasoning effort ("none" = no reasoning, lowest
    # time-to-first-token — what a voice agent wants) and reject temperature;
    # pre-GPT-5 models take temperature and ignore reasoning effort.
    interviewer_reasoning_effort: str = Field(
        default="none", alias="INTERVIEWER_REASONING_EFFORT"
    )
    interviewer_temperature: float = Field(default=0.7, alias="INTERVIEWER_TEMPERATURE")

    # Interview planner/evaluator: run once per interview with no latency
    # pressure, so quality-first models with high reasoning effort.
    planner_model: str = Field(default="gpt-5.5", alias="PLANNER_MODEL")
    planner_reasoning_effort: str = Field(default="high", alias="PLANNER_REASONING_EFFORT")
    evaluator_model: str = Field(default="gpt-5.5", alias="EVALUATOR_MODEL")
    evaluator_reasoning_effort: str = Field(
        default="high", alias="EVALUATOR_REASONING_EFFORT"
    )

    # RAG storage. text-embedding-3-small → 1536 dims (must match the Qdrant
    # collection's vector size).
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=1536, alias="EMBEDDING_DIMENSIONS")
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_collection: str = Field(default="resumes", alias="QDRANT_COLLECTION")

    # Postgres (from docker-compose.yml).
    database_url: str = Field(
        default="postgresql+asyncpg://interview:interview@localhost:5432/interview",
        alias="DATABASE_URL",
    )

    # Interview session limits and wiring.
    interview_max_minutes: int = Field(default=15, alias="INTERVIEW_MAX_MINUTES")
    # Soft cap on simultaneous interviews: each one burns LLM/STT/TTS budget,
    # so /token returns 429 once this many are in progress.
    max_concurrent_interviews: int = Field(default=3, alias="MAX_CONCURRENT_INTERVIEWS")
    # End the interview after this long with no conversation items at all
    # (e.g. the candidate walked away leaving the tab open).
    interview_idle_minutes: int = Field(default=3, alias="INTERVIEW_IDLE_MINUTES")
    livekit_agent_name: str = Field(default="interviewer", alias="LIVEKIT_AGENT_NAME")
    # Where the worker reaches the FastAPI app to auto-trigger evaluation.
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")

    # STT via LiveKit Inference. Empty STT_LANGUAGE lets the multilingual model
    # auto-detect the spoken language per utterance; pin a code (e.g. "es") if
    # transcriptions come out garbled in your language.
    stt_model: str = Field(
        default="assemblyai/universal-streaming-multilingual", alias="STT_MODEL"
    )
    stt_language: str = Field(default="", alias="STT_LANGUAGE")

    # TTS via LiveKit Inference. Default is xAI "Eve x1", multilingual with
    # language auto-detection (other xAI voices: ara, leo, rex). Cartesia
    # Sonic 3 is a faster-to-start alternative (~0.2s vs ~0.7s ttfb).
    tts_model: str = Field(default="xai/tts-1", alias="TTS_MODEL")
    tts_voice: str = Field(default="eve", alias="TTS_VOICE")

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

    @model_validator(mode="after")
    def _clamp_temperature(self) -> Settings:
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
