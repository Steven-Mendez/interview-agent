# Interview Agent

**English** | [Español](README.es.md)

An AI voice job-interview simulator. Upload your resume (PDF) and paste a job offer — an AI agent plans a tailored interview, conducts it with you **by voice in the browser**, and scores you when it's over.

## How it works

Three agents, one flow:

1. **Planner** — reads the resume and the job offer, designs the interview (interviewer persona, milestones to cover) in the language you configured.
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

- Docker
- An OpenAI API key
- A free LiveKit Cloud project (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Run it

```bash
cp .env.example .env         # fill in your keys
docker compose up -d --build
```

That starts the whole stack: Postgres, Qdrant, the API + frontend, and the LiveKit worker (migrations run automatically).

Open <http://localhost:8000>: upload a resume PDF, paste the job offer, wait for the plan (~30–60 s), then start the voice interview. When it ends, the evaluation appears on the same page.

The **Settings** screen configures the agent globally: its name, the interview language (English or Spanish, with a feminine and a masculine voice per language), an optional interviewer persona and custom instructions. Changes apply to interviews created afterwards.

## Development (local)

To iterate with hot reload, run only the databases in Docker and the app with [uv](https://docs.astral.sh/uv/) (Python 3.12+):

```bash
uv sync
docker compose up -d postgres qdrant    # Postgres (:5432) + Qdrant (:6333)
uv run alembic upgrade head             # create the schema

# Terminal 1: the LiveKit worker (the interviewer)
uv run python main.py dev

# Terminal 2: the API + frontend
uv run uvicorn interview_agent.server.app:app --port 8000
```

> **Note on language and voice:** the interview language, the agent's name and its voice are set in the in-app Settings screen (not in `.env`). Speech recognition and synthesis are pinned to the configured language; the voice catalog lives in `interview_agent/voices.py`.
