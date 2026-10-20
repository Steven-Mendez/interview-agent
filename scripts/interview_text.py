#!/usr/bin/env python3
"""Exercise production interview reasoning with text and real LLMs, without LiveKit.

uv run python scripts/interview_text.py --suite
uv run python scripts/interview_text.py --interactive --resume cv.md --job-offer role.txt
uv run python scripts/interview_text.py --replay logs/text-interview/vague.json

The suite uses fixed synthetic facts and answers. Replay evaluates exactly the
saved input, so prompt changes can be compared without a changing conversation.
No STT, TTS, room, or voice worker is used. Only --from-interview reads the
database. API calls spend real tokens.
"""

# ruff: noqa: E402 -- bootstrap repo imports and disable QA telemetry before config loads

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Synthetic QA stays in the local report rather than configured telemetry sinks.
os.environ["LANGSMITH_API_KEY"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
os.environ["SENTRY_DSN"] = ""

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai.chat_models.base import OpenAIRefusalError
from openai import APIError

from interview_agent.config import Settings
from interview_agent.interview.budgets import resolve_budget
from interview_agent.interview.decision import (
    DECISION_SECONDS,
    MAX_INVOCATIONS,
    request_decision,
    system_prompt,
    timeout_decision,
    validate_decision,
)
from interview_agent.interview.evaluator import run_evaluator
from interview_agent.interview.models import (
    InterviewBudget,
    InterviewLength,
    InterviewObjective,
    InterviewPreferences,
    InterviewType,
    Seniority,
)
from interview_agent.interview.planner import run_planner
from interview_agent.llm import build_chat_model, close_chat_model, summarize_usage
from interview_agent.voices import DEFAULT_AGENT_NAME


async def next_decision(settings, context, usage):
    if context["remaining_seconds"] <= 0:
        return timeout_decision(context), []
    deadline = time.monotonic() + DECISION_SECONDS
    errors, repair = [], None
    for _ in range(MAX_INVOCATIONS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Interview decision deadline exhausted")
        model = build_chat_model(
            settings,
            model=settings.interviewer_model,
            reasoning_effort=settings.interviewer_reasoning_effort,
            max_retries=1,
            timeout_seconds=remaining,
        )
        decision = None
        try:
            try:
                async with asyncio.timeout(remaining):
                    decision = await request_decision(
                        model, context, callbacks=[usage], repair=repair
                    )
                validate_decision(decision, context)
                return decision, errors
            except OpenAIRefusalError:
                raise
            except ValueError as exc:
                errors.append(str(exc))
                repair = {
                    "invalid_decision": decision.model_dump(mode="json") if decision else {},
                    "error": str(exc),
                }
        finally:
            try:
                async with asyncio.timeout(1):
                    await close_chat_model(model)
            except TimeoutError:
                pass
    raise ValueError(f"Decision failed after one repair: {errors}")


async def read_console_answer(remaining_seconds):
    """Nonblocking stdin on macOS/Linux: timeout and Ctrl+C leave no input thread behind."""
    if remaining_seconds <= 0:
        raise TimeoutError("Interview time exhausted")
    print("Tú (/end para terminar): ", end="", flush=True)
    loop = asyncio.get_running_loop()
    descriptor = sys.stdin.fileno()
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        line = sys.stdin.readline()
    else:
        future = loop.create_future()
        buffer = bytearray()

        def ready():
            if not future.done():
                try:
                    # Read exactly one byte: TextIOWrapper can prefetch the next
                    # piped answer, hiding it from the next readiness notification.
                    chunk = os.read(descriptor, 1)
                    buffer.extend(chunk)
                    if not chunk or chunk == b"\n":
                        future.set_result(buffer.decode(sys.stdin.encoding or "utf-8"))
                except Exception as exc:
                    future.set_exception(exc)

        loop.add_reader(descriptor, ready)
        try:
            async with asyncio.timeout(remaining_seconds):
                line = await future
        finally:
            loop.remove_reader(descriptor)
    return None if line == "" else line.rstrip("\r\n")


def apply_decision(context, decision):
    targets = {m["id"]: m for m in context["milestones"]}
    for update in decision.updates:
        targets[update.milestone_id].update(
            lifecycle=update.status,
            close_reason=update.close_reason,
        )
        targets[update.milestone_id].setdefault("evidence", []).extend(
            ref.model_dump(mode="json") for ref in update.evidence
        )
    if decision.action != "close":
        target = targets[decision.target_milestone_id]
        target["lifecycle"] = "active"
        counter = {
            "question": "primary_questions",
            "advance": "primary_questions",
            "followup": "followups",
            "clarification": "clarifications",
        }[decision.action]
        target[counter] += 1
        context["primary_questions"] = sum(m["primary_questions"] for m in targets.values())
        context["previous_questions"].append(decision.spoken_text)
    context["state_revision"] += 1


def candidate_record(context, role, text):
    context["transcript"].append(
        {
            "id": str(len(context["transcript"]) + 1),
            "version": 1,
            "role": role,
            "content": text,
            "interrupted": False,
        }
    )


def model_config(settings):
    return {
        "models": {
            component: {
                "model": getattr(settings, f"{component}_model"),
                "reasoning_effort": getattr(settings, f"{component}_reasoning_effort"),
            }
            for component in ("planner", "interviewer", "evaluator")
        }
    }


def settings_for_run(settings, config):
    return settings.model_copy(
        update={
            f"{component}_{key}": value
            for component in ("planner", "interviewer", "evaluator")
            for key, value in config.get("models", {}).get(component, {}).items()
            if key in ("model", "reasoning_effort")
        }
    )


def write_json(path, data):
    """Atomically replace a checkpoint, including the currently unanswered question."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def load_frontend_interview(settings, interview_id):
    """Copy the saved setup into a new local session; never resume its live state."""
    from sqlalchemy import text

    from interview_agent.interview import db

    engine, sessionmaker = db.create_engine_and_sessionmaker(settings.database_url)
    try:
        async with sessionmaker() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            conversation = await db.get_conversation(session, interview_id)
            if conversation is None:
                raise ValueError(f"Interview {interview_id} does not exist")
            if (
                not conversation.plan
                or not conversation.milestones
                or not conversation.agent_settings
            ):
                raise ValueError("The saved interview has no complete plan/settings snapshot")
            budget = InterviewBudget(
                max_minutes=conversation.max_minutes,
                question_limit=conversation.question_limit,
                followup_limit=conversation.followup_limit,
            )
            plan = deepcopy(conversation.plan)
            plan["budget"] = budget.model_dump(mode="json")
            if conversation.interview_preferences:
                plan["preferences"] = deepcopy(conversation.interview_preferences)
            fixture = {
                "source_interview_id": str(conversation.id),
                "resume": conversation.resume_markdown,
                "job_offer": conversation.job_offer,
                "seniority": conversation.seniority,
                "seniority_source": conversation.seniority_source,
                "seniority_evidence": conversation.seniority_evidence,
                "interview_length": conversation.interview_length,
                "language": conversation.agent_settings["language"],
                "agent_settings": deepcopy(conversation.agent_settings),
                "persona": conversation.persona,
                "custom_instructions": conversation.custom_instructions,
                "run_config": deepcopy(conversation.run_config or {}),
                "budget": budget.model_dump(mode="json"),
                "plan": plan,
                "milestones": [
                    {
                        "id": str(m.id),
                        "title": m.title,
                        "description": m.description,
                        "expected_evidence": m.expected_evidence,
                        "essential": m.essential,
                        "competency": m.competency,
                        "planning_context": deepcopy(m.planning_context),
                    }
                    for m in conversation.milestones
                ],
            }
            if not all(
                fixture["run_config"].get("models", {}).get(component, {}).get("model")
                for component in ("interviewer", "evaluator")
            ):
                raise ValueError("The saved interview has no interviewer/evaluator model snapshot")
            return fixture
    finally:
        await engine.dispose()


async def plan_console_interview(settings, args, fixture):
    resume = args.resume.read_text(encoding="utf-8") if args.resume else fixture["resume"]
    job_offer = (
        args.job_offer.read_text(encoding="utf-8") if args.job_offer else fixture["job_offer"]
    )
    if not resume.strip() or not job_offer.strip():
        raise ValueError("Resume and job offer must contain text")
    language, agent_name = args.language or "es", args.agent_name or DEFAULT_AGENT_NAME
    seniority = None if args.seniority in (None, "auto") else Seniority(args.seniority)
    length = InterviewLength(args.length or "standard")
    usage = UsageMetadataCallbackHandler()
    print("Preparando el plan de entrevista…", flush=True)
    plan = await run_planner(
        settings,
        resume,
        job_offer,
        language,
        agent_name,
        seniority=seniority,
        interview_length=length,
        question_limit=args.questions,
        max_minutes=args.minutes,
        followup_limit=args.followups,
        persona=args.persona,
        custom_instructions=args.custom_instructions,
        usage_callback=usage,
        preferences=InterviewPreferences(
            interview_type=args.interview_type or "technical",
            objective=args.objective or "simulation",
            practice_focus=args.practice_focus or "",
        ),
    )
    effective_seniority = seniority or plan.detected_seniority
    if effective_seniority is None:
        raise ValueError("The planner did not classify the role's seniority")
    budget = resolve_budget(
        length,
        effective_seniority,
        server_max_minutes=settings.interview_max_minutes,
        max_minutes=plan.budget.max_minutes,
        question_limit=min(plan.budget.question_limit, len(plan.milestones)),
        followup_limit=plan.budget.followup_limit,
    )
    return {
        "resume": resume,
        "job_offer": job_offer,
        "language": language,
        "agent_settings": {"agent_name": agent_name, "language": language},
        "persona": args.persona,
        "custom_instructions": args.custom_instructions or "",
        "seniority": effective_seniority.value,
        "seniority_source": "explicit" if seniority else "detected",
        "seniority_evidence": plan.seniority_evidence,
        "interview_length": length.value,
        "plan": {
            **plan.model_dump(
                mode="json", exclude={"milestones", "detected_seniority", "seniority_evidence"}
            ),
            "language": language,
            "budget": budget.model_dump(mode="json"),
        },
        "milestones": [
            {
                **m.model_dump(mode="json", exclude={"requirement_id", "resume_quotes"}),
                "id": f"topic-{i + 1}",
                "planning_context": {
                    "requirement_id": m.requirement_id,
                    "resume_quotes": m.resume_quotes,
                },
            }
            for i, m in enumerate(plan.milestones)
        ],
        "run_config": model_config(settings),
        "planner_usage": summarize_usage(usage.usage_metadata),
    }


def show_configuration(settings, fixture):
    budget = fixture["plan"]["budget"]
    preferences = fixture["plan"].get("preferences", {})
    print(
        f"\n{fixture['agent_settings']['agent_name']} · {fixture['language']} · "
        f"{fixture['seniority']} ({fixture['seniority_source']})\n"
        f"{preferences.get('interview_type', 'technical')} · "
        f"{preferences.get('objective', 'simulation')} · "
        f"{budget['max_minutes']} min · {budget['question_limit']} preguntas · "
        f"{budget['followup_limit']} repreguntas por tema\n"
        "Responde en una línea y pulsa Enter. /end, Ctrl+D o Ctrl+C terminan la entrevista.\n",
        flush=True,
    )
    for component, config in model_config(settings)["models"].items():
        print(
            f"{component}: {config['model']} (reasoning: {config['reasoning_effort']})", flush=True
        )


async def interview(settings, fixture, name, *, interactive=False, followups=None, checkpoint=None):
    settings = settings_for_run(settings, fixture.get("run_config") or {})
    saved_budget = fixture.get("budget") or fixture["plan"].get("budget") or {}
    budget = (
        InterviewBudget.model_validate(saved_budget)
        if saved_budget
        else resolve_budget(
            fixture.get("interview_length", "standard"),
            fixture["seniority"],
            server_max_minutes=settings.interview_max_minutes,
            question_limit=len(fixture["milestones"]),
            followup_limit=followups,
        )
    )
    agent_settings = fixture.get("agent_settings") or {
        "language": fixture["language"],
        "agent_name": DEFAULT_AGENT_NAME,
    }
    conversation = SimpleNamespace(
        seniority=fixture["seniority"],
        plan=fixture["plan"],
        resume_markdown=fixture["resume"],
        job_offer=fixture["job_offer"],
        agent_settings=agent_settings,
        persona=fixture.get("persona"),
        custom_instructions=fixture.get("custom_instructions", ""),
    )
    context = {
        "milestones": [
            dict(
                deepcopy(m),
                lifecycle="pending",
                close_reason=None,
                primary_questions=0,
                followups=0,
                clarifications=0,
                evidence=[],
            )
            for m in fixture["milestones"]
        ],
        "transcript": [],
        "state_revision": 0,
        "remaining_seconds": budget.max_minutes * 60,
        "question_limit": budget.question_limit,
        "followup_limit": budget.followup_limit,
        "primary_questions": 0,
        "previous_questions": [],
        "language": fixture["language"],
        "system_prompt": system_prompt(conversation),
    }
    usage = UsageMetadataCallbackHandler()
    decisions = []
    started = time.monotonic()
    closed = None
    end_input = None

    def snapshot():
        return {
            "scenario": name,
            "mode": "interactive" if interactive else "synthetic",
            "source_interview_id": fixture.get("source_interview_id"),
            "resume_markdown": fixture["resume"],
            "job_offer": fixture["job_offer"],
            "plan": fixture["plan"],
            "milestones": context["milestones"],
            "transcript": context["transcript"],
            "ended_reason": closed,
            "end_input": end_input,
            "seniority": fixture["seniority"],
            "seniority_source": fixture.get("seniority_source", "explicit"),
            "seniority_evidence": fixture.get("seniority_evidence"),
            "interview_length": fixture.get("interview_length", "standard"),
            "language": fixture["language"],
            "agent_settings": agent_settings,
            "persona": fixture.get("persona"),
            "custom_instructions": fixture.get("custom_instructions", ""),
            "run_config": fixture.get("run_config") or model_config(settings),
            "planner_usage": fixture.get("planner_usage"),
            "budget": budget.model_dump(mode="json"),
            "decisions": decisions,
            "interviewer_usage": summarize_usage(usage.usage_metadata),
        }

    def remaining():
        # Scripted cases measure reasoning independently of provider latency.
        elapsed = time.monotonic() - started if interactive else len(decisions) * 30
        return max(0, budget.max_minutes * 60 - elapsed)

    def persist():
        if checkpoint:
            write_json(checkpoint, snapshot())

    def expire():
        context["remaining_seconds"] = 0
        decision = timeout_decision(context)
        apply_decision(context, decision)
        decisions.append(
            {"decision": decision.model_dump(mode="json"), "seconds": 0, "repairs": []}
        )

    persist()
    try:
        for _ in range(len(fixture["milestones"]) * (2 + budget.followup_limit) + 1):
            context["remaining_seconds"] = remaining()
            before = time.monotonic()
            try:
                decision, repairs = await next_decision(settings, context, usage)
            except TimeoutError:
                if not interactive or remaining() > 0:
                    raise
                expire()
                closed = "timeout"
                break
            step = {
                "decision": decision.model_dump(mode="json"),
                "seconds": round(time.monotonic() - before, 2),
                "repairs": repairs,
            }
            # Match the controller's persistence-time clock check: a late model
            # question must never be shown or counted as delivered.
            if interactive and remaining() <= 0:
                step["discarded_decision"] = step["decision"]
                context["remaining_seconds"] = 0
                decision = timeout_decision(context)
                step["decision"] = decision.model_dump(mode="json")
            decisions.append(step)
            apply_decision(context, decision)
            if decision.action == "close":
                closed = decision.close_reason
                break
            print(f"\n{agent_settings['agent_name']}: {decision.spoken_text}", flush=True)
            candidate_record(context, "assistant", decision.spoken_text)
            persist()
            if interactive:
                while True:
                    try:
                        answer = await read_console_answer(remaining())
                    except TimeoutError:
                        expire()
                        closed = "timeout"
                        break
                    if answer is None or answer.strip() == "/end":
                        closed = "candidate_left" if answer is None else "candidate_requested"
                        end_input = "eof" if answer is None else "/end"
                        break
                    if answer.strip():
                        break
                    print("Escribe una respuesta o /end para terminar.", flush=True)
                if closed:
                    break
            else:
                answer = fixture["scenarios"][name]["answers"][decision.target_milestone_id]
                print(f"[{name}] Candidato: {answer}", flush=True)
            candidate_record(context, "user", answer)
            persist()
    except asyncio.CancelledError:
        if not interactive:
            raise
        closed, end_input = "candidate_requested", "keyboard_interrupt"
        print("\nEntrevista terminada por el usuario.", flush=True)
    finally:
        persist()
    if closed is None:
        raise ValueError("Interview did not close within the bounded question budget")
    return snapshot()


async def evaluate(settings, run):
    settings = settings_for_run(settings, run.get("run_config") or {})
    usage = UsageMetadataCallbackHandler()
    started = time.monotonic()
    result = await run_evaluator(
        settings,
        **{
            key: run[key]
            for key in (
                "resume_markdown",
                "job_offer",
                "plan",
                "milestones",
                "transcript",
                "ended_reason",
                "seniority",
                "language",
            )
        },
        custom_instructions=run.get("custom_instructions"),
        usage_callback=usage,
    )
    return {
        **run,
        "evaluation": result.model_dump(mode="json"),
        "evaluator_seconds": round(time.monotonic() - started, 2),
        "evaluator_usage": summarize_usage(usage.usage_metadata),
        "evaluation_config": {
            "model": settings.evaluator_model,
            "reasoning_effort": settings.evaluator_reasoning_effort,
            "source_hashes": {
                name: hashlib.sha256((ROOT / "interview_agent" / name).read_bytes()).hexdigest()
                for name in ("interview/evaluator.py", "interview/models.py", "prompts.py")
            },
        },
    }


def feedback_markdown(run):
    ev = run["evaluation"]
    lines = [
        "## Feedback",
        f"Estado: {ev['evaluation_status']} · Alcance: {ev.get('assessment_scope', 'interview')} · "
        f"Cobertura: {ev.get('coverage', 0):.0%}",
        (
            f"Puntuación: {ev['score']}/100 · Contratado (simulación): {ev['hired']}"
            if ev["score"] is not None
            else "Sin puntuación ni veredicto global."
        ),
        "",
        ev["rationale"],
        "",
    ]
    if ev.get("score_gap"):
        lines += [ev["score_gap"], ""]
    for label, key in (
        ("Fortalezas", "strengths"),
        ("A mejorar", "weaknesses"),
        ("Requisitos no cubiertos", "uncovered_requirements"),
    ):
        if ev.get(key):
            lines += [f"### {label}", *[f"- {item}" for item in ev[key]], ""]
    titles = {m["id"]: m["title"] for m in run["milestones"]}
    lines += ["### Criterios", ""]
    for criterion in ev["criteria"]:
        lines += [
            f"- **{titles.get(criterion['milestone_id'], criterion['milestone_id'])} "
            f"({criterion['assessment']})**: {criterion['rationale']}",
        ]
        if criterion["practice"]:
            lines += ["  Práctica: " + criterion["practice"]]
    for revision in ev.get("answer_revisions", []):
        lines += [
            "### Cómo responder mejor",
            "",
            revision.get("question", ""),
            "```diff",
            *["- " + line for line in revision["original"]["quote"].splitlines()],
            *["+ " + line for line in revision["suggested_answer"].splitlines()],
            "```",
            "",
            revision["explanation"],
            "",
            *["- " + detail for detail in revision["missing_details"]],
            "",
        ]
    return "\n".join(lines) + "\n"


def report_markdown(run):
    description = (
        "Entrevista interactiva por consola con modelos reales."
        if run.get("mode") == "interactive"
        else "Prueba sintética con modelos reales."
    )
    lines = [
        f"# Entrevista por texto: {run['scenario']}",
        "",
        description,
        "",
        feedback_markdown(run),
        "## Conversación",
        "",
    ]
    agent_name = run.get("agent_settings", {}).get("agent_name", DEFAULT_AGENT_NAME)
    for message in run["transcript"]:
        lines += [
            f"**{agent_name if message['role'] == 'assistant' else 'Candidato'}**: "
            + message["content"],
            "",
        ]
    return "\n".join(lines) + "\n"


def save_report(output, run):
    output.mkdir(parents=True, exist_ok=True)
    path = output / run["scenario"]
    write_json(path.with_suffix(".json"), run)
    path.with_suffix(".md").write_text(report_markdown(run), encoding="utf-8")
    print(f"Reporte: {path.with_suffix('.md')}", flush=True)


async def main(args):
    settings = Settings()
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required; no LiveKit credentials are needed")
    fixture = json.loads(args.fixture.read_text())
    if args.replay:
        saved = json.loads(args.replay.read_text())
        if not saved.get("ended_reason"):
            raise ValueError("Saved interview did not finish; run that scenario again")
        run = await evaluate(settings, saved)
        save_report(args.output, run)
        print(feedback_markdown(run), flush=True)
        return
    if args.interactive:
        fixture = (
            await load_frontend_interview(settings, args.from_interview)
            if args.from_interview
            else await plan_console_interview(settings, args, fixture)
        )
        settings = settings_for_run(settings, fixture["run_config"])
        show_configuration(settings, fixture)
        write_json(args.output / "setup.json", fixture)
        print(f"Sesión: {args.output}\n", flush=True)
    names = (
        list(fixture["scenarios"])
        if args.suite
        else ["interactive" if args.interactive else args.scenario]
    )
    runs = []
    for name in names:
        args.output.mkdir(parents=True, exist_ok=True)
        path = args.output / f"{name}.json"
        if args.continue_run and path.exists():
            cached = json.loads(path.read_text())
            if cached.get("evaluation"):
                print(f"Using completed report: {path}", flush=True)
                runs.append(cached)
                continue
        source = args.reuse_transcripts / f"{name}.json" if args.reuse_transcripts else None
        if (
            source
            and source.exists()
            and (saved := json.loads(source.read_text())).get("ended_reason")
        ):
            run = {
                k: v
                for k, v in saved.items()
                if k
                not in ("evaluation", "evaluator_usage", "evaluator_seconds", "evaluation_config")
            }
            print(f"Reevaluating identical transcript: {source}", flush=True)
        else:
            run = await interview(
                settings, fixture, name, interactive=args.interactive, checkpoint=path
            )
        # Save even if the subsequent evaluator fails, allowing an exact replay.
        write_json(path, run)
        if args.interactive:
            print(f"\nFin de la entrevista: {run['ended_reason']}. Generando feedback…", flush=True)
        run = await evaluate(settings, run)
        save_report(args.output, run)
        if args.interactive:
            print(feedback_markdown(run), flush=True)
        runs.append(run)
    if args.suite:
        # Coverage controls: missing answers are not incorrect answers.
        for name, transcript in (("unobserved", []), ("partial", runs[0]["transcript"][:2])):
            path = args.output / f"{name}.json"
            if args.continue_run and path.exists():
                cached = json.loads(path.read_text())
                if cached.get("evaluation"):
                    runs.append(cached)
                    continue
            run = dict(
                runs[0],
                scenario=name,
                transcript=transcript,
                ended_reason="candidate_requested",
                decisions=[],
            )
            run = await evaluate(settings, run)
            save_report(args.output, run)
            runs.append(run)
        checks = suite_checks(runs)
        (args.output / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
        for name, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'}: {name}", flush=True)
        if not all(checks.values()):
            raise SystemExit(1)


def suite_checks(runs):
    results = {r["scenario"]: r["evaluation"] for r in runs}
    good = results["concise_correct"]
    incorrect = next(r for r in runs if r["scenario"] == "incorrect")
    misconception_topics = {m["id"] for m in incorrect["milestones"]}
    probed_topics = {
        step["decision"]["target_milestone_id"]
        for step in incorrect["decisions"]
        if step["decision"]["action"] == "followup"
    }
    return {
        "brief_correct_answers_pass": good["hired"] is True and not good["weaknesses"],
        "no_rewrite_of_sufficient_answers": not good.get("answer_revisions"),
        "correct_scores_above_vague_and_incorrect": all(
            good["score"] > results[name]["score"] for name in ("vague", "incorrect", "unknown")
        ),
        "no_verdict_for_missing_coverage": all(
            results[name]["score"] is None and results[name]["hired"] is None
            for name in ("partial", "unobserved")
        ),
        "unobserved_has_no_weaknesses": not results["unobserved"]["weaknesses"],
        "actionable_answer_comparisons": all(
            results[name].get("answer_revisions") for name in ("vague", "incorrect", "unknown")
        ),
        "misconceptions_are_probed_before_moving_on": misconception_topics <= probed_topics,
        "technical_corrections_are_not_just_rewording": all(
            revision["basis"] == "knowledge_example"
            for revision in results["incorrect"].get("answer_revisions", [])
        ),
    }


def parse_args(argv=None, *, interactive_default=False):
    parser = argparse.ArgumentParser(
        description=(
            "Interview by text with the app's planner, interviewer and evaluator. "
            "Uses real models configured in .env. Supply --resume and --job-offer, "
            "--demo, or --from-interview UUID."
            if interactive_default
            else __doc__
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--suite", action="store_true")
    mode.add_argument("--interactive", action="store_true")
    mode.add_argument("--replay", type=Path)
    parser.add_argument(
        "--scenario", default="vague", choices=("vague", "concise_correct", "incorrect", "unknown")
    )
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / "scripts/fixtures/text_interview.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "logs/text-interview")
    parser.add_argument(
        "--reuse-transcripts",
        type=Path,
        help="Reevaluate completed suite transcripts from an earlier output folder",
    )
    parser.add_argument(
        "--continue-run",
        action="store_true",
        help="Keep completed output reports and continue the remaining suite cases",
    )
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--job-offer", type=Path)
    parser.add_argument(
        "--demo", action="store_true", help="Use the bundled synthetic CV and offer"
    )
    parser.add_argument(
        "--from-interview",
        type=uuid.UUID,
        help="Start a fresh console interview with a web interview's saved setup (read-only DB)",
    )
    parser.add_argument(
        "--seniority",
        choices=["auto", *[str(s) for s in Seniority]],
        help="Role level; default: auto, inferred once from the job offer",
    )
    parser.add_argument("--language", choices=("es", "en"), help="Default: es")
    parser.add_argument("--agent-name", help=f"Default: {DEFAULT_AGENT_NAME}")
    parser.add_argument("--persona")
    parser.add_argument("--custom-instructions")
    parser.add_argument(
        "--length", choices=[str(x) for x in InterviewLength], help="Default: standard"
    )
    parser.add_argument("--questions", type=int, choices=range(1, 13))
    parser.add_argument("--minutes", type=int, choices=range(1, 26))
    parser.add_argument("--followups", type=int, choices=range(3))
    parser.add_argument(
        "--interview-type", choices=[str(x) for x in InterviewType], help="Default: technical"
    )
    parser.add_argument(
        "--objective", choices=[str(x) for x in InterviewObjective], help="Default: simulation"
    )
    parser.add_argument("--practice-focus")
    args = parser.parse_args(argv)
    if args.from_interview and (args.suite or args.replay):
        parser.error("--from-interview is an interactive mode")
    if args.from_interview or (interactive_default and not args.suite and not args.replay):
        args.interactive = True
    if (args.reuse_transcripts or args.continue_run) and not args.suite:
        parser.error("--reuse-transcripts and --continue-run require --suite")
    planning_options = (
        "resume",
        "job_offer",
        "seniority",
        "language",
        "agent_name",
        "persona",
        "custom_instructions",
        "length",
        "questions",
        "minutes",
        "followups",
        "interview_type",
        "objective",
        "practice_focus",
    )
    if not args.interactive and (
        args.demo or any(getattr(args, key) is not None for key in planning_options)
    ):
        parser.error("Planning options require --interactive (or scripts/interview_console.py)")
    if args.from_interview:
        if args.demo or any(getattr(args, key) is not None for key in planning_options):
            parser.error("--from-interview reuses the saved setup; omit planning overrides")
    elif args.interactive:
        if args.demo:
            if args.resume or args.job_offer:
                parser.error("Use --demo or your --resume and --job-offer files")
        elif not (args.resume and args.job_offer):
            parser.error(
                "Provide --resume cv.md --job-offer role.txt, --demo, or --from-interview UUID"
            )
        if args.practice_focus and args.objective != "practice":
            parser.error("--practice-focus requires --objective practice")
        if args.agent_name is not None and not args.agent_name.strip():
            parser.error("--agent-name cannot be empty")
    if args.interactive:
        suffix = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        args.output = args.output / suffix
    return args


def cli(argv=None, *, interactive_default=False):
    args = parse_args(argv, interactive_default=interactive_default)
    try:
        asyncio.run(main(args))
    except (APIError, ValueError, OSError) as exc:
        print(f"Prueba incompleta: {exc}\nReportes guardados en: {args.output}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(f"\nInterrumpido. Directorio de sesión: {args.output}", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    cli()
