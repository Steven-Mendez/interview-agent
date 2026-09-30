# Interview Agent

**English** | [Español](README.es.md)

An AI voice job-interview simulator. Upload your resume (PDF) and paste a job offer — an AI agent plans a tailored interview, conducts it with you **by voice in the browser**, and scores you when it's over.

## How it works

Three agents, one flow:

1. **Planner** — reads the resume and the job offer, designs the interview (interviewer persona, milestones to cover) in the language you configured.
2. **Interviewer** — a real-time voice agent that runs the interview in the browser over LiveKit, checks off milestones as it goes, and uses the full resume and job offer in its context to ground its questions.
3. **Evaluator** — when the interview ends (plan complete, time cap, or you close the tab), it scores the transcript automatically: hired or not, score, strengths and weaknesses.

## Tech stack

- **LiveKit Agents** — real-time audio pipeline (STT, TTS, turn detection)
- **LangGraph** — the interviewer's brain (ReAct graph + tools)
- **OpenAI** — LLMs for planning, interviewing and evaluation
- **PostgreSQL** — conversations, milestones, transcripts, evaluations
- **FastAPI** — the API, under `/api` (also serves the built frontend)
- **TanStack Start + shadcn/ui** — React frontend in `web/`, built as a SPA

## Requirements

- Docker
- An OpenAI API key
- A free LiveKit Cloud project (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Run it

```bash
cp .env.example .env         # fill in your keys
docker compose up -d --build
```

That starts the whole stack: Postgres, the API + frontend, and the LiveKit worker (migrations run automatically). The resume is stored as text in Postgres; all three agents receive it directly, without embeddings or a vector database.

Open <http://localhost:8000>: upload a resume PDF, paste the job offer, wait for the plan (~30–60 s), then start the voice interview. When it ends, the evaluation appears on the same page.

The **History** screen lists every interview you have run, newest first, with its score, verdict and how far the topics got — filterable by state. Opening one shows its scorecard and, folded away, the full transcript. **Repeat** runs the same role again: a brand-new interview off the stored resume and job offer, at the same level and length, with freshly planned questions. The original is left untouched, and every re-run stays grouped under it.

The **Settings** screen configures the agent globally: its name, the interview language (English or Spanish, with a feminine and a masculine voice per language), an optional interviewer persona and custom instructions. Changes apply to interviews created afterwards.

## Voice context and recovery

The extracted resume is limited to 30,000 characters and the job offer to 20,000. Oversized sources receive HTTP 413 before planning or starting a voice session, including stored interviews created before these limits. Documents are never silently truncated. These bounds keep the full-document context suitable for interactive voice use; actual response latency still depends on the model and conversation length.

Speculative generation is disabled because the LangGraph tools persist milestones and signal interview closure. Interrupting an already confirmed response does not roll back tools that have run. AssemblyAI duplicate finals are filtered only when their normalized text and audio boundaries identify previously received speech. When timing evidence is absent, speech is kept and the worker logs one warning per STT stream. Historical text fixtures use synthetic timing in tests; real provider metadata and microphone behavior remain to be verified.

Failed or abandoned transcription streams keep any received text marked **Incomplete transcription**. Brief reconnects can continue the existing stream; a reader that receives no data for 30 seconds is cancelled and marked incomplete without ending the interview. The **Enable audio** control appears when the browser blocks playback.

User bubbles follow conversation turns rather than individual STT sentences. The worker forwards cumulative text on `interview.user_transcription` under one id until `conversation_item_added` confirms the exact reply. Repeated sentences are preserved; separate confirmed turns keep separate ids. Agent speech continues using LiveKit's synchronized `lk.transcription` stream. Upgrade worker and frontend together for this protocol.

If the SDK never confirms a user turn before an assistant message, the orphaned text is retained as incomplete and the next answer gets a fresh id. Failed final publications are retried once with the same id; persistent transport failures still leave the partial incomplete, while the confirmed text remains in Postgres. Publisher shutdown drains for at most 10.1 seconds.

The minimum endpointing delay is 1.5 seconds; the longer hesitation delay remains 2.5 seconds. This covers the 1.17-second late STT final observed in the September 30 voice test, with a margin, and costs up to 1.2 seconds more waiting than the previous minimum. Local SDK regression tests reproduce the old split and verify that this tail stays in one turn. Longer provider stalls can still require further measurement; post-change microphone tests in Chrome and Safari remain pending.

## Upgrading from the Qdrant version

Removing Qdrant from Compose does not delete existing containers or the `qdrant_data` volume. The current retention job only manages Postgres; legacy vector data needs a one-time cleanup by the deployment operator.

Identify the old Qdrant container and volume belonging to this deployment with `docker ps -a` and `docker volume ls` (Compose labels identify the project, service and volume). If the old vector data is no longer needed, remove only those Qdrant resources:

```bash
docker rm -f <legacy-qdrant-container>
docker volume rm <legacy-qdrant-volume>
```

Keep the Postgres container and volume: they store the resumes, interviews and evaluations used by this version. Do not use a blanket volume cleanup. This upgrade does not automatically delete legacy Qdrant data.

## Development (local)

To iterate with hot reload, run only Postgres in Docker and the app with [uv](https://docs.astral.sh/uv/) (Python 3.12+) and [pnpm](https://pnpm.io/) (Node 22+):

```bash
uv sync
docker compose up -d postgres           # Postgres (:5432)
uv run alembic upgrade head             # create the schema

# Terminal 1: the LiveKit worker (the interviewer)
uv run python main.py dev

# Terminal 2: the API
uv run uvicorn interview_agent.server.app:app --port 8000

# Terminal 3: the frontend dev server (HMR, proxies /api to :8000)
cd web && pnpm install && pnpm dev
```

Open <http://localhost:3000> for the dev frontend. (The uvicorn on :8000 serves the last `pnpm build` output, if any — production behavior.)

> **Note on language and voice:** the interview language, the agent's name and its voice are set in the in-app Settings screen (not in `.env`). Speech recognition and synthesis are pinned to the configured language; the voice catalog lives in `interview_agent/voices.py`.
