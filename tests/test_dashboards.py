"""Invariants of the provisioned Grafana dashboards that a typo in PromQL would silently break."""

import json
import re
from pathlib import Path

DASHBOARDS = Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"
CLOSING = re.compile(r"interview_agent_closing_(?:playback_confirmed|reconciled)(\{[^}]*\})?")


def test_server_reconciled_closings_ignore_the_interview_filters():
    # The lifecycle sweeper records closings without interview dimensions, so a
    # filter such as language=en must not drop them from some closings panels
    # while the others keep counting them.
    dashboard = json.loads((DASHBOARDS / "closings-and-deletions.json").read_text())
    checked = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            selectors = [match.group(1) or "" for match in CLOSING.finditer(target["expr"])]
            if not selectors:
                continue
            checked += 1
            filtered = [selector for selector in selectors if "$language" in selector]
            # Filtered selectors only ever pick the worker's own samples...
            assert all('source=""' in selector for selector in filtered), panel["title"]
            # ...and the server's samples are always selected without filters.
            assert any("$" not in selector for selector in selectors), panel["title"]
    assert checked >= 5


def test_speech_to_text_latency_is_plotted_by_model_from_the_transcript_delay():
    # LiveKit reports 0 as the request duration of streaming recognition, so only
    # the end-of-utterance delays, labelled with the STT model, measure its latency.
    dashboard = json.loads((DASHBOARDS / "latency-and-cost.json").read_text())
    exprs = [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]
    assert not any("interview_agent_stt_duration_seconds" in expr for expr in exprs)
    transcript = [expr for expr in exprs if "interview_agent_eou_transcription_seconds" in expr]
    assert len(transcript) >= 4
    assert all("sum by (model)" in expr and 'model=~"$model"' in expr for expr in transcript)
