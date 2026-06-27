"""Tests for the GPT-5-vs-legacy model tuning switch."""

from interview_agent.llm import chat_model_tuning


def test_gpt5_family_gets_reasoning_not_temperature():
    tuning = chat_model_tuning("gpt-5.4-mini", reasoning_effort="none", temperature=0.7)
    assert tuning == {"reasoning": {"effort": "none"}}


def test_pre_gpt5_gets_temperature_not_reasoning():
    tuning = chat_model_tuning("gpt-4o", reasoning_effort="high", temperature=0.3)
    assert tuning == {"temperature": 0.3}


def test_never_both_keys():
    for model in ("gpt-5.5", "gpt-5.4-mini", "gpt-4o", "gpt-4.1-mini"):
        tuning = chat_model_tuning(model, reasoning_effort="low", temperature=1.0)
        assert not ({"reasoning", "temperature"} <= tuning.keys())
        assert len(tuning) == 1
