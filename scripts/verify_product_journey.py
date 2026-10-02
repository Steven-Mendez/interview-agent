"""Full product journey in a native headless Chrome against isolated services.

Setup wizard (PDF upload, resume review, typed limits) -> real planner -> real
worker over LiveKit RTC with production STT/TTS -> closing, farewell playback
and ACK -> seal -> real evaluator -> results and history screens.

Only the microphone is synthetic: getUserMedia returns a Web Audio stream that
plays a fixed answer, synthesized with the production TTS, once the first
question is delivered.
Requires --live; uses a fresh migrated interview_benchmark_*_test database and
its own worker name, so the primary database and worker are never touched.
OTLP metrics are never exported. LangSmith traces are exported only with
--langsmith-project, to that separate project, so synthetic interviews never
reach the shared observability backends.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import httpx
from alembic.config import Config
from livekit import api
from livekit.agents import inference
from livekit.agents.types import APIConnectOptions
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.runtime import validate_database_revision
from interview_agent.voices import VOICES

ROOT = Path(__file__).resolve().parents[1]

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE = "/opt/homebrew/bin/node"
API_PORT = 8791
CDP_PORT = 9333
TERMINAL = {"evaluated", "evaluation_failed", "error"}
OFFER = {
    "en": (
        "Junior backend engineer\n\nBuild small Python HTTP APIs, validate input, "
        "write SQL queries and use Git. No leadership duties."
    ),
    "es": (
        "Ingeniero backend junior\n\nConstruir pequeñas APIs HTTP en Python, validar "
        "entradas, escribir consultas SQL y usar Git. Sin funciones de liderazgo."
    ),
}
ANSWER = {
    "en": "I validate the input before writing to the database.",
    "es": "Valido la entrada antes de escribir en la base de datos.",
}
RESUME_LINES = {
    "en": [
        "Alex Rivera - Junior Python developer",
        "Internship: built a small Flask API with input validation.",
        "Wrote SQL queries for a reporting script; uses Git daily.",
    ],
    "es": [
        "Alex Rivera - Desarrollador Python junior",
        "Practicas: construyo una API Flask con validacion de entrada.",
        "Escribio consultas SQL para informes; usa Git a diario.",
    ],
}

# Synthetic microphone and DOM helpers, installed before any page script.
PAGE_SCRIPT = r"""
(() => {
  const ctx = new AudioContext({ sampleRate: 48000 })
  const dest = ctx.createMediaStreamDestination()
  const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    const tracks = []
    if (constraints && constraints.video) {
      const video = await original({ video: constraints.video })
      tracks.push(...video.getVideoTracks())
    }
    if (constraints && constraints.audio) {
      await ctx.resume()
      tracks.push(dest.stream.getAudioTracks()[0].clone())
    }
    return new MediaStream(tracks)
  }
  window.__answer = async (b64) => {
    await ctx.resume()
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const buffer = await ctx.decodeAudioData(bytes.buffer)
    const source = ctx.createBufferSource()
    source.buffer = buffer
    source.connect(dest)
    source.start()
    return buffer.duration
  }
  window.__h = {
    click(prefix) {
      const button = [...document.querySelectorAll("button")].find(
        (b) => b.textContent.trim().startsWith(prefix) && !b.disabled
      )
      if (!button) return false
      button.click()
      return true
    },
    set(selector, value) {
      const el = document.querySelector(selector)
      if (!el) return false
      const proto = el.tagName === "TEXTAREA"
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value)
      el.dispatchEvent(new Event("input", { bubbles: true }))
      el.dispatchEvent(new Event("change", { bubbles: true }))
      return true
    },
    value(selector) {
      const el = document.querySelector(selector)
      return el ? el.value : null
    },
    text() { return document.body ? document.body.innerText : "" },
  }
})()
"""


def journey_url(value):
    url = make_url(value)
    if (
        url.drivername != "postgresql+asyncpg"
        or not url.database
        or not re.fullmatch(r"interview_benchmark_[a-z0-9_]+_test", url.database)
    ):
        raise ValueError("Use a dedicated interview_benchmark_*_test PostgreSQL database")
    return url


@asynccontextmanager
async def journey_database(value):
    """Create and migrate the disposable database; never the primary one."""
    url = journey_url(value)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": url.database}
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()
    engine, sessions = db.create_engine_and_sessionmaker(url.render_as_string(hide_password=False))
    try:
        async with engine.begin() as connection:

            def upgrade(sync):
                config = Config()
                config.set_main_option("script_location", str(ROOT / "alembic"))
                config.attributes["connection"] = sync
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
        await validate_database_revision(sessions)
        yield sessions
    finally:
        await engine.dispose()


async def synthesize_answer(settings, language):
    """The candidate's fixed answer as WAV bytes, using the production TTS."""
    voice = VOICES[f"{language}_female"]
    async with aiohttp.ClientSession() as http:
        tts = inference.TTS(
            model=voice["tts_model"],
            voice=voice["tts_voice"],
            language=language,
            sample_rate=24000,
            encoding="pcm_s16le",
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
            http_session=http,
            conn_options=APIConnectOptions(max_retry=0, timeout=20),
        )
        frames = []
        async with asyncio.timeout(45), tts.synthesize(ANSWER[language]) as stream:
            async for item in stream:
                frames.append(item.frame)
    if not frames:
        raise ValueError("Answer synthesis produced no audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(frames[0].num_channels)
        wav.setsampwidth(2)
        wav.setframerate(frames[0].sample_rate)
        for frame in frames:
            wav.writeframes(bytes(frame.data))
    return output.getvalue()


def synthetic_pdf(lines):
    """A valid text PDF without third-party writers; content is synthetic."""
    body = b"BT /F1 11 Tf 40 760 Td 14 TL"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        body += b" (" + escaped.encode("latin-1") + b") Tj T*"
    body += b" ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        data += f"{offset:010d} 00000 n \n".encode()
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


class CDP:
    def __init__(self, socket):
        self.socket = socket
        self.next_id = 0

    async def send(self, method, **params):
        self.next_id += 1
        ident = self.next_id
        await self.socket.send_json({"id": ident, "method": method, "params": params})
        while True:
            message = await asyncio.wait_for(self.socket.receive_json(), 30)
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed")
                return message.get("result", {})

    async def js(self, expression):
        result = await self.send(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in result:
            raise RuntimeError("Page script raised")
        return result["result"].get("value")


async def until(predicate, timeout, interval=0.5, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {what}")


def stop(process, sig=signal.SIGTERM, timeout=60):
    if process is None or process.poll() is not None:
        return None if process is None else process.returncode
    process.send_signal(sig)
    try:
        return process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(10)


async def postconditions(sessions, interview_id):
    async with sessions() as session:
        row = await session.get(db.Conversation, interview_id)
        evaluation = await session.scalar(
            select(db.Evaluation).where(db.Evaluation.conversation_id == interview_id)
        )
        messages = list(
            await session.scalars(
                select(db.Message)
                .where(db.Message.conversation_id == interview_id)
                .order_by(db.Message.created_at)
            )
        )
        attempts = list(
            await session.scalars(
                select(db.QuestionAttempt.status)
                .join(db.QuestionDelivery, db.QuestionDelivery.id == db.QuestionAttempt.question_id)
                .where(db.QuestionDelivery.conversation_id == interview_id)
            )
        )
        seals = await session.scalar(
            select(func.count())
            .select_from(db.TranscriptSeal)
            .where(db.TranscriptSeal.conversation_id == interview_id)
        )
        result = (evaluation.result or {}) if evaluation else {}
        return {
            "status": row.status,
            "ended_reason": row.ended_reason,
            "farewell_status": row.farewell_status,
            "transcript_integrity": row.transcript_integrity,
            "seals": seals,
            "question_attempts": sorted(attempts),
            "candidate_messages": sum(m.role == "user" for m in messages),
            "interviewer_messages": sum(m.role == "assistant" for m in messages),
            "evaluation_status": result.get("evaluation_status"),
            "evaluation_has_criteria": bool(result.get("criteria")),
            "seniority": row.seniority,
            "question_limit": row.question_limit,
            "followup_limit": row.followup_limit,
            "run_config_models": (row.run_config or {}).get("models"),
            "token_usage_roles": sorted((row.token_usage or {}).keys()),
            # The timings themselves are anonymous OTLP metrics, not rows.
            "response_onset_samples": row.response_onset_samples,
        }


async def journey(args):
    if args.output.exists():
        raise FileExistsError("Never silently repeat a charged product journey")
    base = Settings()
    if not all((base.livekit_url, base.livekit_api_key, base.livekit_api_secret)):
        raise ValueError("LiveKit configuration is required")
    if not base.openai_api_key:
        raise ValueError("OpenAI configuration is required for real planner/evaluator calls")
    tag = uuid.uuid4().hex[:10]
    name = f"interview_benchmark_product_{tag}_test"
    database = make_url(base.database_url).set(database=name).render_as_string(hide_password=False)
    journey_url(database)
    report = {
        "purpose": "full_product_journey_native_chrome",
        "language": args.language,
        "database": name,
        "real_planner": True,
        "real_interviewer": True,
        "real_evaluator": True,
        "real_rtc_stt_tts": True,
        "synthetic_microphone": "web_audio_stream_from_production_tts_answer",
        "physical_audio_verified": False,
        "primary_app_started": False,
        "provider_cost_usd": None,
        "langsmith_project": args.langsmith_project,
        "events": [],
    }

    def save():
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2, default=str) + "\n")
        temporary.replace(args.output)

    def event(name, **fields):
        report["events"].append({"event": name, "t": round(time.monotonic() - started, 2)} | fields)
        save()
        print(json.dumps({"event": name, **fields}, default=str), flush=True)

    started = time.monotonic()
    save()
    env = os.environ | {
        "DATABASE_URL": database,
        "LIVEKIT_AGENT_NAME": f"product-probe-{tag}",
        "LANGSMITH_API_KEY": base.langsmith_api_key if args.langsmith_project else "",
        "LANGSMITH_PROJECT": args.langsmith_project or base.langsmith_project,
        "LANGSMITH_TRACING": "true" if args.langsmith_project else "false",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "OTEL_EXPORTER_OTLP_HEADERS": "",
        "APP_BASE_URL": f"http://127.0.0.1:{API_PORT}",
        "WORKER_DRAIN_MINUTES": "1",
        "INTERVIEW_RECONNECT_SECONDS": "10",
    }
    work = Path(tempfile.mkdtemp(prefix="product-journey-"))
    os.chmod(work, 0o700)
    pdf = work / "synthetic-resume.pdf"
    pdf.write_bytes(synthetic_pdf(RESUME_LINES[args.language]))
    answer = await synthesize_answer(base, args.language)
    report["answer_audio_sha256"] = hashlib.sha256(answer).hexdigest()
    api_process = worker = chrome = None
    interview_id = None
    client = api.LiveKitAPI(
        base.livekit_url,
        base.livekit_api_key,
        base.livekit_api_secret,
        timeout=aiohttp.ClientTimeout(total=10),
    )
    logs = args.output.parent / f"{args.output.stem}-logs"
    logs.mkdir(mode=0o700, exist_ok=False)
    try:
        async with journey_database(database) as sessions:
            event("database_migrated")
            subprocess.run(
                [NODE, "node_modules/vite/bin/vite.js", "build"],
                cwd=ROOT / "web",
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            event("frontend_built")
            api_process = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "uvicorn",
                    "interview_agent.server.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(API_PORT),
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=env,
                stdout=(logs / "api.out").open("w"),
                stderr=subprocess.STDOUT,
            )
            worker = subprocess.Popen(
                ["uv", "run", "python", "main.py", "start"],
                cwd=ROOT,
                env=env,
                stdout=(logs / "worker.out").open("w"),
                stderr=subprocess.STDOUT,
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{API_PORT}", timeout=10
            ) as http:

                async def healthy():
                    with contextlib.suppress(httpx.HTTPError):
                        return (await http.get("/api/healthz")).status_code == 200
                    return False

                await until(healthy, 60, what="API health")
                # Registration is not logged (content-free formatter); an
                # explicit dispatch stays pending until this worker registers.
                await asyncio.sleep(15)
                if worker.poll() is not None:
                    raise RuntimeError("Worker exited during startup")
                event("services_ready")
                voice = f"{args.language}_female"
                settings_body = {
                    "agent_name": "Emma",
                    "language": args.language,
                    "voice": voice,
                    "persona": "",
                    "custom_instructions": "",
                }
                assert (await http.put("/api/settings", json=settings_body)).status_code == 200

                profile = work / "chrome"
                chrome = subprocess.Popen(
                    [
                        CHROME,
                        "--headless=new",
                        f"--remote-debugging-port={CDP_PORT}",
                        f"--user-data-dir={profile}",
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                        "--autoplay-policy=no-user-gesture-required",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "about:blank",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                async def target():
                    with contextlib.suppress(httpx.HTTPError):
                        pages = (await http.get(f"http://127.0.0.1:{CDP_PORT}/json")).json()
                        return next((p for p in pages if p.get("type") == "page"), None)
                    return None

                page = await until(target, 30, what="Chrome target")
                async with (
                    aiohttp.ClientSession() as cdp_http,
                    cdp_http.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as socket,
                ):
                    cdp = CDP(socket)
                    await cdp.send("Page.enable")
                    await cdp.send("Runtime.enable")
                    await cdp.send("Page.addScriptToEvaluateOnNewDocument", source=PAGE_SCRIPT)
                    report["user_agent"] = (await cdp.send("Browser.getVersion")).get("product")
                    await cdp.send("Page.navigate", url=f"http://127.0.0.1:{API_PORT}/new")

                    async def has(selector):
                        return await cdp.js(f"!!document.querySelector({json.dumps(selector)})")

                    await until(lambda: has("#resume"), 30, what="setup form")
                    root = await cdp.send("DOM.getDocument")
                    node = await cdp.send(
                        "DOM.querySelector", nodeId=root["root"]["nodeId"], selector="#resume"
                    )
                    await cdp.send("DOM.setFileInputFiles", nodeId=node["nodeId"], files=[str(pdf)])
                    assert await cdp.js(
                        f"__h.set('#job_offer', {json.dumps(OFFER[args.language])})"
                    )
                    assert await cdp.js("__h.click('Continue')")
                    resume_text = await until(
                        lambda: cdp.js("__h.value('#resume_text')"), 30, what="resume preview"
                    )
                    event("resume_previewed", characters=len(resume_text))
                    assert await cdp.js("__h.click('Continue')")
                    await until(lambda: has("#question_limit"), 15, what="calibration step")
                    assert await cdp.js(f"__h.set('#question_limit', '{args.questions}')")
                    assert await cdp.js("__h.set('#followup_limit', '0')")
                    assert await cdp.js("__h.click('Continue')")
                    await until(
                        lambda: cdp.js("__h.text().includes('Prepare interview')"),
                        15,
                        what="interviewer step",
                    )
                    assert await cdp.js("__h.click('Prepare interview')")
                    event("plan_requested")

                    async def interview_url():
                        href = await cdp.js("location.pathname")
                        return href if href.startswith("/interviews/") else None

                    path = await until(interview_url, 240, what="planned interview page")
                    interview_id = uuid.UUID(path.rsplit("/", 1)[1])
                    report["interview_id"] = str(interview_id)
                    planned = (await http.get(f"/api/interviews/{interview_id}")).json()
                    event(
                        "planned",
                        status=planned["status"],
                        milestones=len(planned["milestones"]),
                        question_limit=planned["question_limit"],
                        followup_limit=planned["followup_limit"],
                    )
                    await until(
                        lambda: cdp.js("__h.text().includes('Start interview')"),
                        30,
                        what="pre-join panel",
                    )
                    assert await cdp.js("__h.click('Start interview')")
                    event("join_clicked")

                    async def delivered():
                        body = (await http.get(f"/api/interviews/{interview_id}/question")).json()
                        question = body.get("question")
                        return (
                            question if question and question["status"] == "sdk_completed" else None
                        )

                    question = await until(delivered, 120, interval=1, what="first question")
                    answered = set()

                    async def answer_question(question):
                        answered.add(question["id"])
                        event("question_delivered", words=len(question["text"].split()))
                        seconds = await cdp.js(
                            f"__answer({json.dumps(base64.b64encode(answer).decode())})"
                        )
                        event("answer_played_into_microphone", seconds=seconds)

                    await answer_question(question)

                    async def status():
                        return (await http.get(f"/api/interviews/{interview_id}")).json()

                    seen = set()

                    async def finished():
                        row = await status()
                        if row["status"] not in seen:
                            seen.add(row["status"])
                            event("status", status=row["status"])
                        if row["status"] == "interviewing":
                            pending = await delivered()
                            if pending and pending["id"] not in answered:
                                await answer_question(pending)
                        return row if row["status"] in TERMINAL else None

                    final = await until(finished, 420, interval=1, what="evaluation")
                    # The page polls every 2 s; wait for the rendered result.
                    with contextlib.suppress(TimeoutError):
                        await until(
                            lambda: cdp.js("__h.text().includes('Evaluator rationale')"),
                            20,
                            what="rendered results",
                        )
                    shot = await cdp.send("Page.captureScreenshot", format="png")
                    (logs / "results.png").write_bytes(base64.b64decode(shot["data"]))
                    text = await cdp.js("__h.text()")
                    report["results_screen"] = {
                        "path": await cdp.js("location.pathname"),
                        "shows_rationale": "Evaluator rationale" in text,
                        "shows_criteria": "Try this next" in text or "criteria" in text.lower(),
                        "shows_ending_note": "How this interview ended" in text,
                    }
                    event("results_rendered", final_status=final["status"])
                    await cdp.send("Page.navigate", url=f"http://127.0.0.1:{API_PORT}/interviews")
                    await until(
                        lambda: cdp.js(f"__h.text().includes({json.dumps(final['title'])})"),
                        30,
                        what="history row",
                    )
                    event("history_listed")
                report["postconditions"] = await postconditions(sessions, interview_id)
                report["passed"] = (
                    report["postconditions"]["status"] == "evaluated"
                    and report["postconditions"]["farewell_status"] == "played"
                    and report["postconditions"]["seals"] >= 1
                    and report["postconditions"]["candidate_messages"] >= 1
                    and report["results_screen"]["shows_rationale"]
                )
                save()
    except BaseException as exc:
        report["passed"] = False
        report["error_type"] = type(exc).__name__
        report["error_message"] = str(exc)[:200] if isinstance(exc, TimeoutError) else None
        raise
    finally:
        report["chrome_exit"] = stop(chrome)
        report["worker_exit"] = stop(worker, signal.SIGINT, 90)
        report["api_exit"] = stop(api_process, signal.SIGINT, 30)
        if interview_id is not None:
            room = f"interview-{interview_id}"
            try:
                rooms = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
                if any(r.name == room for r in rooms.rooms):
                    await client.room.delete_room(api.DeleteRoomRequest(room=room))
                    report["room_cleanup"] = "deleted"
                else:
                    report["room_cleanup"] = "already_absent"
            except Exception as exc:
                report["room_cleanup"] = f"error:{type(exc).__name__}"
        await client.aclose()
        shutil.rmtree(work, ignore_errors=True)
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--language", choices=("en", "es"), default="en")
    parser.add_argument("--questions", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--langsmith-project", help="trace to this LangSmith project (never the main one)"
    )
    args = parser.parse_args()
    if args.langsmith_project and args.langsmith_project == Settings().langsmith_project:
        parser.error("--langsmith-project must not be the main LangSmith project")
    if not args.live:
        print(json.dumps({"mode": "dry_run", "provider_calls": False}))
        return
    asyncio.run(journey(args))


if __name__ == "__main__":
    main()
