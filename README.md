# interview-agent

A voice-based AI job-interview simulator. Upload a resume (PDF) and a job
offer; a **planner** agent designs the interview (persona, language,
milestones), a **voice interviewer** conducts it in the browser over
**LiveKit**, and when it ends an **evaluator** agent scores the candidate
automatically (hired or not, score, strengths/weaknesses).

Built on **LiveKit Agents** (audio pipeline), **LangGraph** (interviewer
brain), **OpenAI** (LLMs + embeddings), **Qdrant** (resume RAG), **Postgres**
(conversations, milestones, transcript, evaluations) and **FastAPI**.

## Architecture

- **RAG hybrid**: the planner and evaluator get the FULL resume markdown in
  their prompt; the interviewer uses a `search_resume` tool (Qdrant, filtered
  by conversation).
- **Models**: `gpt-5.5` (reasoning `high`) for planning/evaluation — quality
  over latency; `gpt-5.4-mini` (reasoning `none`) for the voice interviewer —
  lowest time-to-first-token; `text-embedding-3-small` for embeddings.
- **End of interview**: the interviewer marks milestones via tools and calls
  `end_interview` when done; a hard `INTERVIEW_MAX_MINUTES` cap (wrap-up cue
  at T−2 min) is the safety net. Evaluation triggers automatically on any end
  path (plan complete, timeout, candidate closed the tab).

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/), Docker
- An OpenAI API key
- A LiveKit Cloud project (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Run it

```bash
uv sync
cp .env.example .env                    # fill in your keys
docker compose up -d                    # Postgres (:5432) + Qdrant (:6333)
uv run alembic upgrade head             # create the schema
uv run python main.py download-files    # one-time: VAD/turn-detector models

# Terminal 1: the LiveKit worker (the interviewer)
uv run python main.py dev

# Terminal 2: the API + frontend
uv run uvicorn interview_agent.server.app:app --port 8000
```

Open <http://localhost:8000>: upload a resume PDF, paste the job offer, wait
for the plan (~30–60 s), then start the voice interview. When it ends you get
the evaluation on the same page.

## API

| Endpoint | What it does |
|---|---|
| `POST /interviews` | multipart `resume` (PDF) + `job_offer` (text) → converts with markitdown, indexes in Qdrant, runs the planner, returns the plan |
| `GET /interviews/{id}` | status, plan, milestones, evaluation (the frontend polls this) |
| `GET /interviews/{id}/token` | LiveKit token; dispatches the interviewer agent to the room with the conversation id |
| `POST /interviews/{id}/evaluate` | runs the evaluator over the transcript; auto-triggered by the worker, re-invocable |

## Project layout

```
main.py                        # LiveKit worker CLI entry point
docker-compose.yml             # Postgres + Qdrant
alembic/                       # DB migrations
frontend/                      # plain HTML/CSS/JS frontend
interview_agent/
├── config.py                  # settings from .env
├── llm.py                     # chat-model factory + streaming helpers
├── prompts.py                 # all LLM prompts in one place
├── agent.py                   # LiveKit worker: runs the interview session
├── interview/
│   ├── db.py                  # SQLAlchemy async models + queries
│   ├── rag.py                 # markitdown → chunks → embeddings → Qdrant
│   ├── models.py              # planner/evaluator structured-output schemas
│   ├── planner.py             # designs the interview
│   ├── evaluator.py           # scores the finished interview
│   └── interviewer_graph.py   # per-session ReAct graph + tools
└── server/                    # FastAPI app + routes
```

## Language

The interview language is decided by the planner from the job offer. STT/TTS
are multilingual with auto-detection; if transcriptions come out garbled, pin
`STT_LANGUAGE` (e.g. `es`) in `.env`.
