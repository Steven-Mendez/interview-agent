# Interview Agent

**English** | [Español](README.es.md)

An AI voice job-interview simulator. Upload your resume (PDF) and paste a job offer — an AI agent plans a tailored interview, conducts it with you **by voice in the browser**, and scores you when it's over.

## How it works

Three agents, one flow:

1. **Planner** — reads the resume and the job offer, designs the interview (interviewer persona, language, milestones to cover).
2. **Interviewer** — a real-time voice agent that runs the interview in the browser over LiveKit, checks off milestones as it goes, and can search your resume mid-conversation (RAG).
3. **Evaluator** — when the interview ends (plan complete, time cap, or you close the tab), it scores the transcript automatically: hired or not, score, strengths and weaknesses.

## Tech stack

- **LiveKit Agents** — real-time audio pipeline (STT, TTS, turn detection)
- **LangGraph** — the interviewer's brain (ReAct graph + tools)
- **OpenAI** — LLMs and embeddings
- **Qdrant** — vector search over the resume
- **PostgreSQL** — conversations, milestones, transcripts, evaluations
- **FastAPI** — API + plain HTML/CSS/JS frontend

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Docker
- An OpenAI API key
- A free LiveKit Cloud project (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Run it

```bash
uv sync
cp .env.example .env                    # fill in your keys
docker compose up -d                    # Postgres (:5433) + Qdrant (:6335)
uv run alembic upgrade head             # create the schema
uv run python main.py download-files    # one-time: VAD/turn-detector models

# Terminal 1: the LiveKit worker (the interviewer)
uv run python main.py dev

# Terminal 2: the API + frontend
uv run uvicorn interview_agent.server.app:app --port 8000
```

Open <http://localhost:8000>: upload a resume PDF, paste the job offer, wait for the plan (~30–60 s), then start the voice interview. When it ends, the evaluation appears on the same page.

> **Note on language:** the interview language is picked by the planner from the job offer. STT/TTS auto-detect the spoken language; if transcriptions come out garbled, pin `STT_LANGUAGE` (e.g. `es`) in `.env`.
