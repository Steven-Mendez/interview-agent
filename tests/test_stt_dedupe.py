"""Dedupe of AssemblyAI's redundant aggregate STT finals.

Every fixture below is a verbatim transcript from `logs/agent.log`, room
`interview-e477d357-34e2-46f6-bac0-12b295aba5b9`, so these tests double as the
record of the incident. In that interview AssemblyAI emitted 12 finals for the
candidate, 4 of them redundant aggregates.
"""

from interview_agent.agent import _make_duplicate_final_filter, _normalize_final

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
    "EBay. Aproveché la naturaleza nativa de Icing Await FCPI para paralizar "
    "estas peticiones web."
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
    keep = _make_duplicate_final_filter()
    assert [keep(t) for t in (T4_A, T4_B, T4_AGGREGATE)] == [True, True, False]


def test_drops_aggregate_arriving_before_the_turn_commit():
    keep = _make_duplicate_final_filter()
    assert [keep(t) for t in (T1_A, T1_B, T1_C, T1_AGGREGATE)] == [True, True, True, False]


def test_drops_aggregate_spanning_a_sentence_boundary():
    keep = _make_duplicate_final_filter()
    assert [keep(t) for t in (T2_A, T2_B, T2_AGGREGATE)] == [True, True, False]


def test_drops_aggregate_covering_a_single_final():
    keep = _make_duplicate_final_filter()
    assert [keep(t) for t in (T3_A, T3_AGGREGATE)] == [True, False]


def test_whole_session_drops_exactly_the_four_aggregates():
    keep = _make_duplicate_final_filter()
    dropped = [t for t in WHOLE_SESSION if not keep(t)]
    assert dropped == [T1_AGGREGATE, T2_AGGREGATE, T3_AGGREGATE, T4_AGGREGATE]


def test_keeps_short_affirmations_repeated():
    keep = _make_duplicate_final_filter()
    assert [keep(t) for t in ("Sí.", "Sí", "Correcto.", "No, no.", "Claro que sí.")] == [True] * 5


def test_keeps_repetition_below_the_word_floor():
    keep = _make_duplicate_final_filter()
    seven_words = "Trabajé con FastAPI, Redis y Postgres."
    assert [keep(seven_words), keep(seven_words)] == [True, True]


def test_keeps_a_prefix_of_earlier_speech():
    # Containment would wrongly drop this; suffix matching does not.
    keep = _make_duplicate_final_filter()
    assert keep("uno dos tres cuatro cinco seis siete ocho nueve diez") is True
    assert keep("uno dos tres cuatro cinco seis siete ocho") is True


def test_keeps_a_genuinely_different_long_answer():
    keep = _make_duplicate_final_filter()
    assert keep(T4_A) is True
    assert keep(T2_A) is True


def test_keeps_repetition_outside_the_recency_window():
    # The interviewer asked the candidate to repeat: seconds pass in between.
    now, skip = fake_clock()
    keep = _make_duplicate_final_filter(now=now)
    assert keep(T3_A) is True
    skip(30.0)
    assert keep(T3_AGGREGATE) is True


def test_forgets_finals_older_than_the_history_window():
    now, skip = fake_clock()
    keep = _make_duplicate_final_filter(now=now)
    assert keep(T3_A) is True
    skip(90.0)
    assert keep(T3_AGGREGATE) is True


def test_dropped_finals_are_not_recorded():
    # The buffer must mirror what went downstream, so a dropped aggregate does
    # not become the yardstick for the next one.
    keep = _make_duplicate_final_filter()
    assert [keep(t) for t in (T3_A, T3_AGGREGATE, T3_AGGREGATE)] == [True, False, False]


def test_keeps_empty_and_whitespace_finals():
    keep = _make_duplicate_final_filter()
    assert [keep(""), keep("   "), keep("...")] == [True, True, True]


def test_normalization_folds_case_punctuation_and_digit_grouping():
    assert _normalize_final("de los 5. 000 a 10. 000 productos.") == _normalize_final(
        "DE LOS 5 000 A 10 000 PRODUCTOS"
    )
    assert _normalize_final("Aproveché, sí.") == _normalize_final("aproveche si")
    assert _normalize_final("  ") == []
