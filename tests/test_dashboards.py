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


USERS_GAUGES = {
    "interview_agent_accounts_users",
    "interview_agent_accounts_active_users",
    "interview_agent_accounts_guests_out_of_interviews",
    "interview_agent_accounts_guest_interviews_this_month",
    "interview_agent_accounts_guest_interviews_monthly_limit",
}
USERS_EVENTS = {
    "interview_agent_accounts_new_user",
    "interview_agent_accounts_interview_reserved",
    "interview_agent_accounts_quota_rejected",
    "interview_agent_auth_sign_in",
    "interview_agent_auth_rejected",
}


def users_targets() -> list[dict]:
    dashboard = json.loads((DASHBOARDS / "users-and-access.json").read_text())
    return [target for panel in dashboard["panels"] for target in panel.get("targets", [])]


def test_the_users_dashboard_reads_exactly_the_account_metrics():
    names = {
        name
        for target in users_targets()
        for name in re.findall(r"interview_agent_[a-z_]+", target["expr"])
    }
    assert names == USERS_GAUGES | USERS_EVENTS


def test_the_users_dashboard_never_groups_or_labels_by_who():
    dashboard = json.loads((DASHBOARDS / "users-and-access.json").read_text())
    # Counts only: no interview filters, and no label that could name someone.
    assert [variable["name"] for variable in dashboard["templating"]["list"]] == ["datasource"]
    forbidden = {"owner_id", "user", "user_id", "email", "sub", "conversation_id", "id"}
    for target in users_targets():
        words = set(re.findall(r"[A-Za-z_]+", target["expr"] + " " + target["legendFormat"]))
        assert not words & forbidden, target["expr"]


def latest_report(metric: str, label: str | None) -> str:
    """The newest value any API instance reported in the range, by label."""
    by = f"by ({label}) " if label else ""
    reported_at = f"last_over_time(timestamp({metric})[$__range:])"
    return (
        f"max {by}(last_over_time({metric}[$__range]) and "
        f"({reported_at} == on ({label or ''}) group_left max {by}({reported_at})))"
    )


def test_gauges_take_the_latest_report_and_events_add_up():
    # Each API instance reports the same database counts: adding them up would
    # multiply the users by the instances. Every process is a new series, so
    # over a range the highest last value could be an instance that stopped
    # before a lower count (a new month, fewer active users): the stat panels
    # take the most recent report instead, and the time series the highest of
    # each interval's.
    stats = {
        latest_report(metric, label)
        for metric, label in (
            ("interview_agent_accounts_users", "role"),
            ("interview_agent_accounts_active_users", "window"),
            ("interview_agent_accounts_guests_out_of_interviews", None),
            ("interview_agent_accounts_guest_interviews_this_month", None),
            ("interview_agent_accounts_guest_interviews_monthly_limit", None),
        )
    }
    over_time = {
        "max by (window) (last_over_time(interview_agent_accounts_active_users[$__interval]))"
    }
    gauges = set()
    for target in users_targets():
        expr = target["expr"]
        if any(name in expr for name in USERS_GAUGES):
            gauges.add(expr)
            assert "sum" not in expr, expr
        else:
            assert expr.startswith("histogram_count(sum "), expr
            assert "offset" in expr and "last_over_time" in expr, expr
    assert gauges == stats | over_time


def test_dashboard_uids_are_unique():
    uids = [json.loads(path.read_text())["uid"] for path in sorted(DASHBOARDS.glob("*.json"))]
    assert len(uids) == len(set(uids)) >= 3
    assert "interview-agent-users" in uids
