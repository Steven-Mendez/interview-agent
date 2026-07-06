"""Voice catalog for the interviewer, served through LiveKit Inference.

The Settings screen offers two languages and, per language, two curated
voices (one feminine, one masculine). The database stores only the catalog
KEY; the concrete tts_model/tts_voice pair is resolved here and snapshotted
onto the conversation at creation time, so editing this catalog never
changes an interview already planned.
"""

from __future__ import annotations

SUPPORTED_LANGUAGES = ("en", "es")

DEFAULT_AGENT_NAME = "Emma"  # feminine, matches the default female voice
DEFAULT_LANGUAGE = "en"
DEFAULT_VOICE = "en_female"

# Curated from LiveKit Inference's suggested voices
# (https://docs.livekit.io/agents/models/tts). Cartesia voices are UUIDs,
# Inworld voices are names.
VOICES: dict[str, dict[str, str]] = {
    "en_female": {
        "label": "Jacqueline",
        "gender": "female",
        "language": "en",
        "tts_model": "cartesia/sonic-3",
        "tts_voice": "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
    },
    "en_male": {
        "label": "Blake",
        "gender": "male",
        "language": "en",
        "tts_model": "cartesia/sonic-3",
        "tts_voice": "a167e0f3-df7e-4d52-a9c3-f949145efdab",
    },
    "es_female": {
        "label": "Daniela",
        "gender": "female",
        "language": "es",
        "tts_model": "cartesia/sonic-3",
        "tts_voice": "5c5ad5e7-1020-476b-8b91-fdcbe9cc313c",
    },
    "es_male": {
        "label": "Diego",
        "gender": "male",
        "language": "es",
        "tts_model": "inworld/inworld-tts-2",
        "tts_voice": "Diego",
    },
}


def voices_by_language() -> dict[str, list[dict[str, str]]]:
    """The catalog grouped for the frontend's language → voice selector."""
    return {
        language: [
            {"id": key, "label": voice["label"], "gender": voice["gender"]}
            for key, voice in VOICES.items()
            if voice["language"] == language
        ]
        for language in SUPPORTED_LANGUAGES
    }
