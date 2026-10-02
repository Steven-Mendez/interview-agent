# Interview Agent

**English** | [Español](README.es.md)

An AI voice job-interview simulator. Upload your resume (PDF) and paste a job offer — an AI agent plans a tailored interview, conducts it with you **by voice in the browser**, and scores you when it's over.

## How it works

Three agents, one flow:

1. **Planner** — reads the resume and the job offer, designs the interview (interviewer persona, milestones to cover) in the language you configured.
2. **Interviewer** — a real-time voice agent that runs the interview in the browser over LiveKit, checks off milestones as it goes, and uses the full resume and job offer in its context to ground its questions.
3. **Evaluator** — after closure and an immutable transcript seal, it assesses each criterion with evidence from the candidate. Partial or insufficient interviews have no global score or verdict. Recoverable requests preserve their attempts and previous results.

## Tech stack

- **LiveKit Agents** — real-time audio pipeline (STT, TTS, turn detection)
- **LangGraph** — validated interview decisions with transactional persistence
- **OpenAI** — LLMs for planning, interviewing and evaluation
- **PostgreSQL** — conversations, milestones, transcripts, evaluations
- **FastAPI** — the API, under `/api` (also serves the built frontend)
- **TanStack Start + shadcn/ui** — React frontend in `web/`, built as a SPA

## Requirements

- Docker
- An OpenAI API key
- A LiveKit Cloud project (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Run it

```bash
cp .env.example .env         # fill in your keys
docker compose up -d --build
```

That starts the whole stack: Postgres, the API + frontend, and the LiveKit worker (migrations run automatically). The resume is stored as text in Postgres; all three agents receive it directly, without embeddings or a vector database.

Open <http://localhost:8000> and choose **New interview**: upload a resume PDF, paste the job offer and wait for the plan (~30–60 s). The preparation room lets you check your microphone and camera before you start the voice interview. When it ends, the results open in the app as soon as the evaluation is ready.

The **History** screen lists every interview you have run, newest first, with its score, verdict and how far the topics got — filterable by state. Opening one shows its scorecard and, folded away, the full transcript. **Repeat** runs the same role again: a brand-new interview off the stored resume and job offer, at the same level and length, with freshly planned questions. The original is left untouched, and every re-run stays grouped under it.

The **Settings** screen configures the agent globally: its name, the interview language (English or Spanish, with a feminine and a masculine voice per language), an optional interviewer persona and custom instructions. Changes apply to interviews created afterwards.

## Voice context and recovery

The extracted resume is limited to 30,000 characters and the job offer to 20,000. Oversized sources receive HTTP 413 before planning or starting a voice session. Documents are never silently truncated. Review the extracted PDF text before submitting it; the reviewed text remains bound to the uploaded PDF's hash.

Each interview saves its effective level, language, voice, models, question limits and duration. Reconnecting retains the original start and consumed budget. The live counter uses elapsed time calculated by PostgreSQL and a local monotonic interval between updates; an unavailable value is shown as `--:--`.

Candidate replies have stable logical identities and immutable text versions. Confirmed text and provider provenance are saved before decisions use them. Corrections and late or conflicting captures remain identifiable; repeated wording alone does not establish duplication. Unconfirmed transcription is retained as incomplete. Speculative decisions are disabled, and validated decisions commit before question delivery.

A persisted question that never started can resume under the same ID. An uncertain or interrupted delivery is not repeated automatically. **Listen again** (under **Current question**) explicitly requests the saved question without spending another question or model decision, provided no confirmed answer already prevents replay.

In the normal closing flow, the browser plays the localized farewell and sends a durable playback acknowledgement. The worker drains recognition and writes an immutable transcript seal before results become available. A timeout or missing acknowledgement is not presented as successful playback. **Enable audio** appears when the browser blocks playback. Worker loss, abandonment and incomplete recording remain visible, and missing audio is not treated as a wrong answer.

The results distinguish coverage from ability and show criterion evidence and suggested practice. **Request new assessment** creates a separate request while retaining the earlier feedback; an uncertain HTTP reply keeps the same request identity on retry. **Evaluation history** includes earlier results and failed attempts. **Review saved answers** supports explicit incident decisions and new transcript versions; it preserves old versions and assessments, and a confirmed omission keeps the affected old global score hidden. These controls are optional and do not require a manual review to conduct an interview.

The current endpointing delays are 1.5 seconds minimum and 2.5 seconds for longer hesitation. SDK and synthetic-audio integration tests cover the implemented contracts; they do not establish physical microphone behavior, audible latency or improved interview quality. Latency, spend and closing outcomes are exported as anonymous OpenTelemetry metrics (categories and numbers only) to Grafana: the compose `lgtm` service serves it on <http://localhost:3001> with the dashboards in `infra/grafana/dashboards`. Local logs never contain interview content.

LangSmith is optional, and setting `LANGSMITH_API_KEY` is the consent to send it each interview's full **content**: resume, job offer, plan, turns, prompts, model answers, evaluation and the session audio. Without the key nothing is sent. With it, the app uses LangSmith's native tracing, and each interview's traces are grouped in one LangSmith thread:

- **`planner`**: resume, job offer and the interview's options in; the plan out.
- **`dialogue_turn`**, one per turn: the decision graph and the model call with its prompt, answer and tokens.
- **The voice session**, one per worker run, from LangSmith's official LiveKit integration: its root carries the **stereo audio recording** (candidate and interviewer) and the conversation, and below it each turn with its STT/LLM/TTS latencies.
- **`evaluator`**: resume, job offer, plan and transcript in; score, evidence and comments out.

To find an interview, open the project's **Threads** tab and filter by `thread_id` (the original interview's ID, shared by its repeats) or by the `interview_id` metadata. LangSmith only shows cost for priced models: under **Settings > Model Pricing**, add rows for `gpt-6-astra`, `gpt-6.1-sol`, `gpt-5.5` and `gpt-5.4-mini`, cached-token price included. Grafana remains the source of truth for spend.

The content then follows the LangSmith project's own retention (base 14 days, extended 400; anything added to a dataset or experiment can live longer), and the app never deletes it. With a LiveKit Cloud project that has agent observability enabled, LiveKit Cloud also receives the recording, under LiveKit's own retention.

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

Set `AUTH_MODE=local` in `.env` for local development (`.env.example` does; the Docker Compose stack defaults to it): there is no login and every request is the `local-dev` user, an admin when listed in `ADMIN_USER_IDS`. To try the login locally (the web app, built without `VITE_NEON_AUTH_URL`, asks `GET /api/auth/config` and shows a username and password form), list accounts in `LOCAL_ACCOUNTS` (`admin:<password>,guest:<password>`, with ids `local:<username>`, e.g. `ADMIN_USER_IDS=local-dev,local:admin`) and set `INTERNAL_API_TOKEN`: the API then signs its own tokens and every user route requires one. The browser keeps that token in `localStorage`, acceptable only because this mode never runs outside development: with any `AUTH_MODE=local` the API refuses to start when `DATABASE_URL` points at a remote host or `SENTRY_ENVIRONMENT=production`. The default, `AUTH_MODE=neon`, verifies Neon Auth tokens and ignores `LOCAL_ACCOUNTS`. Interviews created before accounts existed have no owner and stay hidden until you assign them: `uv run python scripts/claim_interviews.py --owner local-dev`. Each user's profile keeps the email and name their sign-in carries, and when they were last seen, outside the interview retention purge (admins list users at `GET /api/admin/users`, in the web app at `/admin/users`; everyone sees their own at `/profile`); the Grafana dashboard **Interview Agent users** shows counts only (users, activity, quotas, sign-ins), never who.

Tests need only Docker running: `uv run pytest` starts a throwaway Postgres 16 container for the session and removes it afterwards (no `.env`, no running database). To use an existing database instead, set `TEST_DATABASE_URL` to one whose name ends in `_test` (CI does this with its service container).

> **Note on language and voice:** the interview language, the agent's name and its voice are set in the in-app Settings screen (not in `.env`). Speech recognition and synthesis are pinned to the configured language; the voice catalog lives in `interview_agent/voices.py`.
