"""Real provider smoke checks using synthetic data; never production transcripts.

Run: uv run python scripts/verify_providers.py --text --voice --output /tmp/smoke/report.json
These checks prove API/voice compatibility, not superiority or human-rated quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from langchain_core.callbacks import UsageMetadataCallbackHandler
from livekit import rtc
from livekit.agents import inference, stt

from interview_agent.closing import synthesize_farewell
from interview_agent.config import settings
from interview_agent.interview.models import InterviewLength, Seniority
from interview_agent.interview.planner import run_planner
from interview_agent.llm import summarize_usage
from interview_agent.observability import LLMObserver, Telemetry, execution_config
from interview_agent.voices import VOICES


class ArtifactTelemetry(Telemetry):
    def __init__(self, configuration):
        super().__init__(
            None, uuid.uuid4(), settings.model_copy(update={"langsmith_api_key": ""}), configuration
        )
        self.records = []

    async def record(self, component, name, value, *, turn_id=None, dimensions=None):
        self.records.append(
            {
                "component": component,
                "name": name,
                "value": value,
                "turn_id": turn_id,
                "dimensions": self.dimensions(dimensions),
            }
        )


async def text_check(model):
    effective = settings.model_copy(
        update={"planner_model": model, "planner_reasoning_effort": "high"}
    )
    configuration = execution_config(
        effective,
        language="es",
        voice=VOICES["es_female"],
        seniority="junior",
        interview_length="short",
        question_limit=1,
        max_minutes=8,
    )
    telemetry = ArtifactTelemetry(configuration)
    observer = LLMObserver(telemetry, "planner", model, "high")
    usage = UsageMetadataCallbackHandler()
    started = time.monotonic()
    result = {
        "component": "planner",
        "requested_model": model,
        "configuration": configuration,
        "human_review": "pending",
        "synthetic_input": True,
    }
    try:
        plan = await asyncio.wait_for(
            run_planner(
                effective,
                "Synthetic CV: six months building small Python APIs during an internship.",
                "Junior backend engineer. Build small Python APIs, use SQL queries and Git. "
                "No leadership duties.",
                language="es",
                agent_name="Emma",
                seniority=Seniority.JUNIOR,
                interview_length=InterviewLength.SHORT,
                max_minutes=8,
                question_limit=1,
                usage_callback=usage,
                telemetry_callback=observer,
            ),
            timeout=180,
        )
        result.update(status="passed", output=plan.model_dump(mode="json"))
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
    finally:
        await telemetry.drain()
    result.update(
        duration_seconds=time.monotonic() - started,
        usage=summarize_usage(usage.usage_metadata),
        metrics=telemetry.records,
    )
    return result


async def voice_checks(output):
    results = []
    async with aiohttp.ClientSession() as http_session:
        for key, voice in VOICES.items():
            started = time.monotonic()
            result = {
                "component": "tts",
                "voice_key": key,
                "model": voice["tts_model"],
                "voice": voice["tts_voice"],
                "language": voice["language"],
                "synthetic_input": True,
                "human_review": "pending",
            }
            model = inference.TTS(
                model=voice["tts_model"],
                voice=voice["tts_voice"],
                language=voice["language"],
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
                http_session=http_session,
            )
            try:
                audio = await asyncio.wait_for(synthesize_farewell(model, voice["language"]), 60)
                clip = output.parent / f"farewell-{key}.wav"
                clip.write_bytes(audio)
                result.update(status="passed", bytes=len(audio), artifact=clip.name)
            except Exception as exc:
                result.update(status="failed", error_type=type(exc).__name__)
            finally:
                await model.aclose()
            result["duration_seconds"] = time.monotonic() - started
            results.append(result)
    return results


async def stt_checks(output):
    results = []
    async with aiohttp.ClientSession() as http_session:
        for requested_model in ("assemblyai/universal-3-6-pro", "deepgram/flux-general-multi"):
            for language in ("en", "es"):
                clip = output.parent / f"farewell-{language}_female.wav"
                with wave.open(str(clip)) as audio:
                    rate, channels, width = (
                        audio.getframerate(),
                        audio.getnchannels(),
                        audio.getsampwidth(),
                    )
                    pcm = audio.readframes(audio.getnframes())
                assert channels == 1 and width == 2
                model = inference.STT(
                    model=requested_model,
                    language=language,
                    sample_rate=rate,
                    api_key=settings.livekit_api_key,
                    api_secret=settings.livekit_api_secret,
                    http_session=http_session,
                )
                finals = []
                arrived = asyncio.Event()
                started = time.monotonic()

                async def transcribe(
                    model=model, finals=finals, arrived=arrived, rate=rate, pcm=pcm
                ):
                    async with model.stream() as stream:

                        async def receive():
                            async for event in stream:
                                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                                    finals.extend(
                                        [
                                            {
                                                "text": alternative.text,
                                                "start_time": alternative.start_time,
                                                "end_time": alternative.end_time,
                                            }
                                            for alternative in event.alternatives
                                        ]
                                    )
                                    arrived.set()

                        receiver = asyncio.create_task(receive())
                        try:
                            frame_bytes = rate // 20 * 2
                            for start in range(0, len(pcm), frame_bytes):
                                chunk = pcm[start : start + frame_bytes]
                                stream.push_frame(
                                    rtc.AudioFrame(
                                        data=chunk,
                                        sample_rate=rate,
                                        num_channels=1,
                                        samples_per_channel=len(chunk) // 2,
                                    )
                                )
                                await asyncio.sleep(len(chunk) / (rate * 2))
                            for _ in range(30):
                                stream.push_frame(
                                    rtc.AudioFrame(
                                        data=b"\x00" * frame_bytes,
                                        sample_rate=rate,
                                        num_channels=1,
                                        samples_per_channel=rate // 20,
                                    )
                                )
                                await asyncio.sleep(0.05)
                            stream.end_input()
                            await asyncio.wait_for(arrived.wait(), 15)
                            await asyncio.sleep(0.5)
                        finally:
                            await stream.aclose()
                            await receiver

                result = {
                    "component": "stt",
                    "model": requested_model,
                    "language": language,
                    "source_artifact": clip.name,
                    "synthetic_input": True,
                    "human_review": "pending",
                }
                try:
                    await asyncio.wait_for(transcribe(), 45)
                    result.update(
                        status="passed"
                        if any(final["text"].strip() for final in finals)
                        else "failed",
                        finals=finals,
                    )
                except Exception as exc:
                    result.update(status="failed", error_type=type(exc).__name__, finals=finals)
                finally:
                    await model.aclose()
                result["duration_seconds"] = time.monotonic() - started
                results.append(result)
                print(
                    json.dumps(
                        {key: result[key] for key in ("component", "model", "language", "status")}
                    ),
                    flush=True,
                )
    return results


async def main(args):
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "provider_compatibility_smoke",
        "quality_improvement_proven": False,
        "results": [],
    }
    if output.exists():
        report = json.loads(output.read_text())
    if args.text:
        for model in ("gpt-6-astra", "gpt-6.1-sol"):
            result = await text_check(model)
            report["results"].append(result)
            output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            print(
                json.dumps(
                    {
                        "component": "planner",
                        "model": model,
                        "status": result["status"],
                        "duration_seconds": result["duration_seconds"],
                        "usage": result["usage"],
                    }
                ),
                flush=True,
            )
    if args.voice:
        results = await voice_checks(output)
        report["results"].extend(results)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        for result in results:
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in ("component", "voice_key", "status", "duration_seconds")
                    }
                ),
                flush=True,
            )
    if args.stt:
        report["results"].extend(await stt_checks(output))
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--stt", action="store_true")
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
