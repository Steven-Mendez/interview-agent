"""LiveKit's own usage/timing events, without conflating defaults with known zeroes."""

TIMINGS = {
    "duration": "duration_seconds",
    "ttft": "ttft_seconds",
    "ttfb": "ttfb_seconds",
    "audio_duration": "audio_duration_seconds",
    "acquire_time": "acquire_seconds",
    "end_of_utterance_delay": "end_of_utterance_seconds",
    "transcription_delay": "transcription_seconds",
    "on_user_turn_completed_delay": "user_turn_completed_seconds",
    "inference_duration_total": "inference_seconds",
}
COUNTERS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_audio_tokens",
    "characters_count",
    "inference_count",
    "cancelled",
    "connection_reused",
)


def record_voice_metrics(telemetry, metric, *, stt_model, tts_model):
    component = metric.type.removesuffix("_metrics")
    component = "voice_llm" if component == "llm" else component
    model = stt_model if component == "stt" else tts_model if component == "tts" else None
    metadata = getattr(metric, "metadata", None)
    dimensions = {"source": "livekit"}
    if model:
        dimensions["model"] = model
        dimensions["resolved_model"] = getattr(metadata, "model_name", None) or model
        dimensions["provider"] = getattr(metadata, "model_provider", None) or model.split("/")[0]
    turn_id = getattr(metric, "speech_id", None)
    for field, name in TIMINGS.items():
        if field in type(metric).model_fields:
            value = getattr(metric, field) if field in metric.model_fields_set else None
            telemetry.emit(component, name, value, turn_id=turn_id, dimensions=dimensions)
    for field in COUNTERS:
        if field in type(metric).model_fields:
            value = getattr(metric, field) if field in metric.model_fields_set else None
            telemetry.emit(
                component,
                field,
                float(value) if value is not None else None,
                turn_id=turn_id,
                dimensions=dimensions,
            )
    if component in ("stt", "tts"):
        # Audio usage is measured; billing rates are unknown until reconciled.
        telemetry.emit(
            component, "estimated_cost_usd", None, turn_id=turn_id, dimensions=dimensions
        )
