"""Configuration gates for unverified providers and voice metric samples."""

import pytest
from pydantic import ValidationError

from interview_agent.agent import voice_metric_samples
from interview_agent.config import VERIFIED_STT_MODELS, Settings


@pytest.mark.parametrize("model", sorted(VERIFIED_STT_MODELS))
def test_verified_stt_models_are_accepted(model):
    assert Settings(_env_file=None, STT_MODEL=model).stt_model == model


@pytest.mark.parametrize("model", ["deepgram/flux-general-multi", "deepgram/nova-3", ""])
def test_unverified_stt_models_cannot_be_selected_for_new_interviews(model):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, STT_MODEL=model)


def test_voice_metrics_keep_durations_and_drop_timestamps_flags_and_ids():
    samples = voice_metric_samples(
        {
            "started_speaking_at": 1_790_000_000.5,
            "stopped_speaking_at": 1_790_000_003.0,
            "transcription_delay": 0.42,
            "end_of_turn_delay": 1,
            "stt_confirmed": True,
            "stt_turn_id": "turn-1",
            "e2e_latency": float("nan"),
        }
    )
    assert samples == {"transcription_delay": 0.42, "end_of_turn_delay": 1}
