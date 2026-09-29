"""Dedupe of AssemblyAI's redundant aggregate STT finals.

Every fixture below is a verbatim transcript from `logs/agent.log`, room
`interview-e477d357-34e2-46f6-bac0-12b295aba5b9`, so these tests double as the
record of the incident. In that interview AssemblyAI emitted 12 finals for the
candidate, 4 of them redundant aggregates.
"""

import logging

import pytest
from livekit.agents import Agent, ModelSettings, stt

from interview_agent.agent import InterviewAgent, _make_duplicate_final_filter, _normalize_final

# --- Turn 1 (16:03:17-27): the aggregate arrived BEFORE the turn commit, so
# livekit concatenated it into the open transcript — it landed *inside* DB row
# seq 1 rather than as an extra row.
T1_A = "Hola Daniela, un gusto, claro que sí."
T1_B = (
    "En mi rol para Adopt Contribui una Pinface PI encargada de servir datos "
    "de productos en tiempo real."
)
T1_C = "Tanto para una extensión del navegador como para una app mobile"
T1_AGGREGATE = (
    "hola daniela un gusto claro que sí en mi rol para adopt contribui una "
    "pinface pi encargada de servir datos de productos en tiempo real tanto "
    "para una extensión del navegador como para una app mobile"
)

# --- Turn 2 (16:03:40-45): aggregate spanning two finals across a sentence
# boundary ("…Amazon Walmart." + "EBay. Aproveché…").
T2_A = (
    "Sobre la concurrencia principal desafío era que dependíamos de múltiples "
    "llamados de input output a proveedor externo como Bright Data, Oxilapse, "
    "Laby de Kipa para tener precio y disponibilidad en Amazon Walmart."
)
T2_B = (
    "EBay. Aproveché la naturaleza nativa de Icing Await FCPI para paralizar estas peticiones web."
)
T2_AGGREGATE = (
    "sobre la concurrencia principal desafío era que dependíamos de múltiples "
    "llamados de input output a proveedor externo como bright data oxilapse "
    "laby de kipa para tener precio y disponibilidad en amazon walmart ebay "
    "aproveché la naturaleza nativa de icing await fcpi para paralizar estas "
    "peticiones web"
)

# --- Turn 3 (16:03:51): aggregate covering a single final. This is the one
# that became DB row seq 2, a pure lowercase copy of the tail of seq 1.
T3_A = (
    "Lo que nos permitió manejar múltiples solicitudes simultáneas de los "
    "clientes sin bloquear el hilo principal del servidor."
)
T3_AGGREGATE = (
    "lo que nos permitió manejar múltiples solicitudes simultáneas de los "
    "clientes sin bloquear el hilo principal del servidor"
)

# --- Turn 4 (16:04:21-32): the aggregate arrived AFTER the turn commit, so it
# opened a second turn — DB row seq 5, the extra bubble in the screenshot.
T4_A = (
    "Sobre la latencia, hacer scraping o llamar a API de tercero en tiempo real "
    "es costoso y lento para solucionarlo y implementar una estrategia de "
    "cachares y utilizando Redis y persistencia en PostgreSeqDow. Sin usuario "
    "consulta uno de los 5. 000 a 10. 000 productos que ya tenemos indexados. "
    "Servíamos el dato de la caché en milisegundos. Si el dato no estaba, "
    "entraba el sistema de Fow Pax asíncrono. Además, para no hacer esperar al "
    "cliente en procesos largos, implementamos SOC, Socketall."
)
T4_B = (
    "Para emitir eventos en tiempo real, hacer fronten una vez que la "
    "información extra como review terminaba de procesarse en el fondo."
)
T4_AGGREGATE = (
    "sobre la latencia hacer scraping o llamar a api de tercero en tiempo real "
    "es costoso y lento para solucionarlo y implementar una estrategia de "
    "cachares y utilizando redis y persistencia en postgreseqdow sin usuario "
    "consulta uno de los 5 000 a 10 000 productos que ya tenemos indexados "
    "servíamos el dato de la caché en milisegundos si el dato no estaba entraba "
    "el sistema de fow pax asíncrono además para no hacer esperar al cliente en "
    "procesos largos implementamos soc socketall para emitir eventos en tiempo "
    "real hacer fronten una vez que la información extra como review terminaba "
    "de procesarse en el fondo"
)

WHOLE_SESSION = [
    T1_A,
    T1_B,
    T1_C,
    T1_AGGREGATE,
    T2_A,
    T2_B,
    T2_AGGREGATE,
    T3_A,
    T3_AGGREGATE,
    T4_A,
    T4_B,
    T4_AGGREGATE,
]


# Synthetic audio boundaries for algorithm tests, NOT recovered provider
# metadata. The historical logs only establish the text fixtures above.
_INTERVALS = {
    T1_A: (0, 1),
    T1_B: (1, 2),
    T1_C: (2, 3),
    T1_AGGREGATE: (0, 3),
    T2_A: (4, 5),
    T2_B: (5, 6),
    T2_AGGREGATE: (4, 6),
    T3_A: (7, 8),
    T3_AGGREGATE: (7, 8),
    T4_A: (9, 10),
    T4_B: (10, 11),
    T4_AGGREGATE: (9, 11),
}
MODEL = "assemblyai/universal-streaming-multilingual"


def final(text=T3_A, *, start=7.0, end=8.0, request_id="stream-a"):
    return stt.SpeechEvent(
        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
        request_id=request_id,
        alternatives=[stt.SpeechData(language="es", text=text, start_time=start, end_time=end)],
    )


def text_filter(**kwargs):
    """Run historical text fixtures with explicitly synthetic audio evidence."""
    keep = _make_duplicate_final_filter(model=MODEL, **kwargs)

    def keep_text(text):
        start, end = _INTERVALS.get(text, (20, 21))
        return keep(final(text, start=start, end=end))

    return keep_text


def fake_clock(step: float = 1.0):
    """A clock that advances `step` seconds per reading, plus a way to skip ahead."""
    state = {"t": 0.0}

    def now() -> float:
        state["t"] += step
        return state["t"]

    def skip(seconds: float) -> None:
        state["t"] += seconds

    return now, skip


def test_drops_aggregate_arriving_after_the_turn_commit():
    keep = text_filter()
    assert [keep(t) for t in (T4_A, T4_B, T4_AGGREGATE)] == [True, True, False]


def test_drops_aggregate_arriving_before_the_turn_commit():
    keep = text_filter()
    assert [keep(t) for t in (T1_A, T1_B, T1_C, T1_AGGREGATE)] == [True, True, True, False]


def test_drops_aggregate_spanning_a_sentence_boundary():
    keep = text_filter()
    assert [keep(t) for t in (T2_A, T2_B, T2_AGGREGATE)] == [True, True, False]


def test_drops_aggregate_covering_a_single_final():
    keep = text_filter()
    assert [keep(t) for t in (T3_A, T3_AGGREGATE)] == [True, False]


def test_whole_session_drops_exactly_the_four_aggregates():
    keep = text_filter()
    dropped = [t for t in WHOLE_SESSION if not keep(t)]
    assert dropped == [T1_AGGREGATE, T2_AGGREGATE, T3_AGGREGATE, T4_AGGREGATE]


def test_keeps_short_affirmations_repeated():
    keep = text_filter()
    assert [keep(t) for t in ("Sí.", "Sí", "Correcto.", "No, no.", "Claro que sí.")] == [True] * 5


def test_keeps_repetition_below_the_word_floor():
    keep = text_filter()
    seven_words = "Trabajé con FastAPI, Redis y Postgres."
    assert [keep(seven_words), keep(seven_words)] == [True, True]


def test_keeps_a_prefix_of_earlier_speech():
    # Containment would wrongly drop this; suffix matching does not.
    keep = text_filter()
    assert keep("uno dos tres cuatro cinco seis siete ocho nueve diez") is True
    assert keep("uno dos tres cuatro cinco seis siete ocho") is True


def test_keeps_a_genuinely_different_long_answer():
    keep = text_filter()
    assert keep(T4_A) is True
    assert keep(T2_A) is True


def test_keeps_repetition_outside_the_recency_window():
    # The interviewer asked the candidate to repeat: seconds pass in between.
    now, skip = fake_clock()
    keep = text_filter(now=now)
    assert keep(T3_A) is True
    skip(30.0)
    assert keep(T3_AGGREGATE) is True


def test_forgets_finals_older_than_the_history_window():
    now, skip = fake_clock()
    keep = text_filter(now=now)
    assert keep(T3_A) is True
    skip(90.0)
    assert keep(T3_AGGREGATE) is True


def test_dropped_finals_are_not_recorded():
    # The buffer must mirror what went downstream, so a dropped aggregate does
    # not become the yardstick for the next one.
    keep = text_filter()
    assert [keep(t) for t in (T3_A, T3_AGGREGATE, T3_AGGREGATE)] == [True, False, False]


def test_keeps_empty_and_whitespace_finals():
    keep = text_filter()
    assert [keep(""), keep("   "), keep("...")] == [True, True, True]


def test_normalization_folds_case_punctuation_and_digit_grouping():
    assert _normalize_final("de los 5. 000 a 10. 000 productos.") == _normalize_final(
        "DE LOS 5 000 A 10 000 PRODUCTOS"
    )
    assert _normalize_final("Aproveché, sí.") == _normalize_final("aproveche si")
    assert _normalize_final("  ") == []


@pytest.mark.parametrize("delay", [0.1, 4.5])
def test_keeps_real_repetition_with_new_audio_even_inside_recency_window(delay):
    clock = [0.0]
    keep = _make_duplicate_final_filter(model=MODEL, now=lambda: clock[0])
    assert keep(final())
    clock[0] = delay
    assert keep(final(start=8.5, end=9.5))


@pytest.mark.parametrize(
    "start,end", [(0, 0), (-1, 8), (8, 7), (7, float("nan")), (float("inf"), 8)]
)
def test_keeps_missing_or_invalid_audio_evidence(start, end):
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(final())
    assert keep(final(start=start, end=end))
    # Ambiguous speech also breaks the otherwise consecutive history.
    assert keep(final())


def test_only_drops_matching_audio_boundaries_with_one_millisecond_tolerance():
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(final())
    assert not keep(final(start=7.0009, end=8.0009))
    assert keep(final(start=7.002, end=8.002))


def test_request_id_scopes_history_but_does_not_prove_duplication():
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(final())
    assert keep(final(request_id="stream-b"))
    assert not keep(final(request_id="stream-b"))
    assert keep(final(start=9, end=10, request_id="stream-b"))


def test_audio_clock_regression_resets_history():
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(final())
    assert keep(final(start=1, end=2))
    assert keep(final())


@pytest.mark.parametrize("model", ["deepgram/nova-3", "assemblyai/universal-streaming"])
def test_other_models_are_never_filtered(model):
    keep = _make_duplicate_final_filter(model=model)
    assert keep(final())
    assert keep(final())


def test_interims_and_empty_alternatives_pass_through():
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT))
    event = final()
    event.type = stt.SpeechEventType.INTERIM_TRANSCRIPT
    assert keep(event)
    assert keep(final())


def test_logs_reasons_without_candidate_text(caplog):
    caplog.set_level(logging.DEBUG, logger="interview_agent")
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(final())
    assert not keep(final())
    assert keep(final(start=0, end=0))
    assert keep(final(start=0, end=0))
    assert "matching text and audio boundaries" in caplog.text
    assert "insufficient audio timing evidence" in caplog.text
    assert caplog.text.count("insufficient audio timing evidence") == 1
    assert T3_A not in caplog.text


def test_logs_request_changes_and_ambiguous_boundaries_without_text(caplog):
    caplog.set_level(logging.DEBUG, logger="interview_agent")
    keep = _make_duplicate_final_filter(model=MODEL)
    assert keep(final())
    assert keep(final(start=9, end=10))
    assert keep(final(start=9, end=10, request_id="stream-b"))
    assert "matching text but different audio boundaries" in caplog.text
    assert "request changed; preserving speech" in caplog.text
    assert T3_A not in caplog.text


@pytest.mark.parametrize("model,expected", [(MODEL, 1), ("deepgram/nova-3", 2)])
async def test_new_stt_node_resets_history_and_other_models_pass_through(
    monkeypatch, model, expected
):
    async def default_node(*args, **kwargs):
        yield final()
        yield final()

    monkeypatch.setattr(Agent.default, "stt_node", default_node)
    agent = InterviewAgent(instructions="Test", stt_model=model)
    for _ in range(2):
        events = [event async for event in agent.stt_node(None, ModelSettings())]
        assert len(events) == expected
