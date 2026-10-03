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

In the normal closing flow, `AgentSession.say()` speaks the localized farewell through the same audio track as the questions, so it enters the session history and LangSmith recording. A browser-readiness RPC prevents speaking to an incompatible or audio-blocked tab. The worker checks the speech handle for errors, interruption and audio playout, then drains recognition and writes an immutable transcript seal before results become available. Native `played` is explicitly labelled `agent_playout`: it confirms the agent finished, not complete playback at the browser or physical speaker. Historical WAV acknowledgements remain labelled `browser_playback`; no new WAV is sent. **Enable audio** resumes the existing session audio. Worker loss, abandonment and incomplete recording remain visible, and missing audio is not treated as a wrong answer.

The results distinguish coverage from ability and show criterion evidence and suggested practice. **Request new assessment** creates a separate request while retaining the earlier feedback; an uncertain HTTP reply keeps the same request identity on retry. **Evaluation history** includes earlier results and failed attempts. **Review saved answers** supports explicit incident decisions and new transcript versions; it preserves old versions and assessments, and a confirmed omission keeps the affected old global score hidden. These controls are optional and do not require a manual review to conduct an interview.

The current endpointing delays are 1.5 seconds minimum and 2.5 seconds for longer hesitation. SDK and synthetic-audio integration tests cover the implemented contracts; they do not establish physical microphone behavior, audible latency or improved interview quality. Latency, spend and closing outcomes are exported as anonymous OpenTelemetry metrics (categories and numbers only) to Grafana: the compose `lgtm` service serves it on <http://localhost:3001> with the dashboards in `infra/grafana/dashboards`. Local logs (rotating files under `LOG_DIR`, `logs/` by default; empty keeps the console as the only output) never contain interview content.

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

Set `AUTH_MODE=local` in `.env` for local development (`.env.example` does; the Docker Compose stack defaults to it): there is no login and every request is the `local-dev` user, an admin when listed in `ADMIN_USER_IDS`. To try the login locally (the web app, built without `VITE_NEON_AUTH_URL`, asks `GET /api/auth/config` and shows a username and password form), list accounts in `LOCAL_ACCOUNTS` (`admin:<password>,guest:<password>`, with ids `local:<username>`, e.g. `ADMIN_USER_IDS=local-dev,local:admin`) and set `INTERNAL_API_TOKEN`: the API then signs its own tokens and every user route requires one. The browser keeps that token in `localStorage`, acceptable only because this mode never runs outside development: with any `AUTH_MODE=local` the API refuses to start when `DATABASE_URL` points at a remote host or `SENTRY_ENVIRONMENT=production`. The default, `AUTH_MODE=neon`, verifies Neon Auth tokens and ignores `LOCAL_ACCOUNTS`. The web app's `/privacy` and `/terms` pages need no sign-in and are linked from the sign-in card: they are the privacy policy and terms of service URLs the Google OAuth consent screen links to, so update their text when what the app stores or sends, or its limits, change. The build prerenders them to `privacy/index.html` and `terms/index.html`, which Vercel serves ahead of the SPA rewrite, so both read without JavaScript. Interviews created before accounts existed have no owner and stay hidden until you assign them: `uv run python scripts/claim_interviews.py --owner local-dev`. Each user's profile keeps the email and name their sign-in carries, and when they were last seen, outside the interview retention purge (admins list users at `GET /api/admin/users`, in the web app at `/admin/users`; everyone sees their own at `/profile`); the Grafana dashboard **Interview Agent users** shows counts only (users, activity, quotas, sign-ins), never who.

Who is an admin is decided by `ADMIN_USER_IDS` alone: a comma-separated list of user ids (the JWT `sub`; `local:<username>` for a local account) read when the API starts. Admins have no interview quota and see the user list; everyone else is a guest, with `LIFETIME_INTERVIEWS_PER_USER` interviews each and `GUEST_INTERVIEWS_PER_MONTH` between them. Nothing in the token or in Neon Auth grants the role, so an account cannot promote itself. To add an admin: they sign in once, you read their id from `/admin/users` (or from `user_profiles`), add it to the variable and restart the API. A Neon Auth user keeps the same id everywhere the same Neon Auth project is used, so the id found in development is the one to put in production.

Tests need only Docker running: `uv run pytest` starts a throwaway Postgres 16 container for the session and removes it afterwards (no `.env`, no running database). To use an existing database instead, set `TEST_DATABASE_URL` to one whose name ends in `_test` (CI does this with its service container).

> **Note on language and voice:** the interview language, the agent's name and its voice are set in the in-app Settings screen (not in `.env`). Speech recognition and synthesis are pinned to the configured language; the voice catalog lives in `interview_agent/voices.py`.

## Deploy

Each piece runs on a managed service. [`infra/README.md`](infra/README.md) is the runbook for a first setup, step by step and in order: the accounts, Neon and Neon Auth, the Google and GitHub sign-in apps, Sentry, Grafana Cloud, LangSmith, the three platforms below and the repository secrets, then what to set once both origins exist and how to verify the result. This section covers what the application expects from each of them.

- **The API** on [FastAPI Cloud](https://fastapicloud.com). `fastapi deploy` (from the dev group) uploads only what `.fastapicloudignore` lets through, and FastAPI Cloud runs the entrypoint in `[tool.fastapi]` of `pyproject.toml`. Its variables are set with `fastapi cloud env set`, with `LOG_DIR` empty: the filesystem there is ephemeral, so the logs go to the console only. An idle API scales to zero, and the web app says so while the first request wakes it.
- **The web app** on [Vercel](https://vercel.com), as a project with Root Directory `web`; `web/vercel.json` holds the build settings and the rewrite a SPA needs. Vercel builds it with Node.js 24, as CI does, and the pnpm version that `packageManager` in `web/package.json` pins. Its build variables, listed in `web/.env.example`, are `VITE_API_BASE_URL` (the API's origin followed by `/api`), `VITE_NEON_AUTH_URL` and `VITE_SENTRY_DSN`; they are baked into the bundle, so changing one takes a new build. Vercel's Git integration builds every push to `main`.
- **The worker** on [LiveKit Cloud](https://cloud.livekit.io), built from the Dockerfile's last stage. The first deployment is by hand: `lk agent create --secrets-file <file>` with the worker's variables as the agent's secrets (LiveKit Cloud supplies `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` itself). It writes `livekit.toml`, which names the agent. The one committed here names this deployment's agent, and `lk agent` refuses to run against another project while it is there: a fresh setup deletes it before `create` and commits its own in its place before the next push to `main`. Without it in the repository the workflow below cannot deploy the worker, and since the worker also checks the schema revision, a migration would leave it refusing every interview until you run `lk agent deploy` by hand.
- **Postgres with Neon Auth** on Neon and **the dashboards** on Grafana Cloud, provisioned with Terraform under `infra/`.

From then on, `main` deploys itself. Once CI passes on a push to `main`, `.github/workflows/deploy.yml` checks out the commit CI tested and applies the migrations to Neon first: the API refuses to start on a schema revision other than the one its code expects, so the schema must be ahead of the code. Then it deploys the API, checking that it answers `/api/healthz` afterwards, and the worker in parallel; in a checkout without `livekit.toml` the worker's job only leaves a warning. A manual run from the Actions tab deploys only from `main`, and deploys the head of `main` as it is, without waiting for CI. The web app is not part of it: Vercel builds it on its own. The workflow needs these repository secrets: `DATABASE_URL` (Neon's, from `infra/neon`), `FASTAPI_CLOUD_TOKEN` (a deploy token) and `FASTAPI_CLOUD_APP_ID`, `API_ORIGIN` (the API's origin, without a trailing slash), and `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` for the LiveKit Cloud project.

`.github/workflows/maintenance.yml` calls `POST /api/internal/maintenance` every day at 04:00 UTC: the retention purge and a sweep of the interviews' lifecycle. The API also purges on its own daily loop, but an API scaled to zero runs no loop. It needs `API_ORIGIN` too, and one more secret: `INTERNAL_API_TOKEN` (the API's own value).

The production variables differ from `.env` in these (`.env.example` documents each one; the runbook lists which process takes which):

- `DATABASE_URL` is Neon's, the `database_url` output of `infra/neon` (the direct host, not the pooler).
- `INTERNAL_API_TOKEN` is a fresh random value (`openssl rand -hex 32`). Neon mode requires it, and the API, the worker's secrets and the `INTERNAL_API_TOKEN` repository secret must all hold the same one: the worker sends it to start the evaluation, the daily maintenance call sends it, and the API checks it.
- `AUTH_MODE=neon` with `NEON_AUTH_URL`, and `ADMIN_USER_IDS` listing Neon Auth ids.
- `CORS_ALLOWED_ORIGINS` with the Vercel origin, since the web app calls the API from another origin.
- `APP_BASE_URL` is the API's origin: the worker calls it there to start the evaluation.
- `SENTRY_DSN` with `SENTRY_ENVIRONMENT=production`, `LOG_DIR` empty on the API, and the Grafana Cloud `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`.
- A `LIVEKIT_AGENT_NAME` of its own, such as `interviewer-prod`, set identically in the API and the worker, so a developer's local worker on the same LiveKit project never picks up a production interview.
- With LangSmith on, `LANGSMITH_API_KEY`, a `LANGSMITH_PROJECT` of its own (such as `interview-agent-prod`) and `LANGSMITH_ENDPOINT`, on both the API (planner and evaluator) and the worker (dialogue turns and the voice session). That sends every user's interview content and session audio to LangSmith, as described above, so the privacy page must say so, and it does.

After the first deploy, the web app's origin still has to be trusted by Neon Auth and allowed by the API's CORS, and the first admin set: the runbook's [After the first deploy](infra/README.md#after-the-first-deploy) has the steps. While the Google sign-in app is in Testing, only its listed test users can sign in; publishing it asks for the app's home page, privacy policy and terms of service. Those are the web app's `/privacy` and `/terms`, which the build prerenders, so they read without JavaScript and without signing in (see [Development](#development-local)).
