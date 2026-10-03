# Infrastructure

The runbook for a first production setup, from fresh accounts to a working
interview, in the order the steps depend on each other. Neon (`neon/`) and the
Grafana Cloud dashboards (`grafana/`) are Terraform roots with local state; the
rest is set up with each platform's console or CLI. The dashboards in
`grafana/dashboards/` are the same files the local `lgtm` container
provisions. What runs where, the application's production variables and the
workflows that deploy the API and the worker and run the daily maintenance
are in the README's [Deploy](../README.md#deploy) section.

Every value below is a placeholder: `<api-origin>` is the API's origin on
FastAPI Cloud and `<web-origin>` the web app's on Vercel, both `https://...`
without a trailing slash.

## Prerequisites

- Accounts on Neon, Google Cloud, GitHub, Sentry, Grafana Cloud, FastAPI
  Cloud, Vercel, LiveKit Cloud and OpenAI, plus LangSmith if production traces
  interviews (see [LangSmith](#langsmith-optional))
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.6 or later
- [uv](https://docs.astral.sh/uv/) and `uv sync` in the repository: the dev
  group brings the FastAPI Cloud CLI (`uv run fastapi ...`) and Alembic
- Node.js, for the Neon and Vercel CLIs through `npx`. Below, `neon` stands
  for `npx -y neonctl@latest` (or `npm i -g neonctl`)
- The [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/) (`lk`)
- The [GitHub CLI](https://cli.github.com) (`gh`), for the repository secrets

## Where the values go

The setup produces a handful of credentials and URLs that the later steps
need. Keep them in one file outside the repository, readable only by you, so
none of them lands in a commit, a shell history or a chat:

```bash
mkdir -p ~/.config/interview-agent
touch ~/.config/interview-agent/secrets.prod.env
chmod 600 ~/.config/interview-agent/secrets.prod.env
```

Fill it in as you go; each entry says where its value comes from:

```bash
OPENAI_API_KEY=               # OpenAI: API keys
LIVEKIT_URL=                  # LiveKit Cloud project: Settings > Keys (wss://...)
LIVEKIT_API_KEY=              # the same page's key pair
LIVEKIT_API_SECRET=
NEON_API_KEY=                 # Neon: organization API key
NEON_PROJECT_ID=              # terraform -chdir=infra/neon output -raw project_id
NEON_AUTH_URL=                # neon neon-auth status: base_url
GOOGLE_OAUTH_CLIENT_ID=       # Google Auth Platform: Clients
GOOGLE_OAUTH_CLIENT_SECRET=
GITHUB_OAUTH_CLIENT_ID=       # GitHub: OAuth Apps
GITHUB_OAUTH_CLIENT_SECRET=
GRAFANA_URL=                  # https://<stack>.grafana.net
GRAFANA_AUTH=                 # Grafana service account token (Admin role)
OTEL_EXPORTER_OTLP_ENDPOINT=  # https://otlp-gateway-<region>.grafana.net/otlp
OTLP_INSTANCE_ID=             # the stack's OpenTelemetry instance id
OTLP_TOKEN=                   # access policy token with metrics:write
OTEL_EXPORTER_OTLP_HEADERS=   # Authorization=Basic%20<base64 of id:token>
SENTRY_DSN=                   # Sentry: the Python project (API and worker)
VITE_SENTRY_DSN=              # Sentry: the React project (web app)
VERCEL_TOKEN=                 # Vercel: Account Settings > Tokens
API_ORIGIN=                   # <api-origin>
WEB_ORIGIN=                   # <web-origin>
INTERNAL_API_TOKEN=           # openssl rand -hex 32, production's own
ADMIN_USER_IDS=               # Neon Auth ids, after the first sign-in
```

`OTLP_INSTANCE_ID`, `OTLP_TOKEN`, `API_ORIGIN` and `WEB_ORIGIN` are names for
this file only. `DATABASE_URL` is not kept here: it stays in the Neon
Terraform state and is read from it when needed.

The commands below read the file's names as shell variables (`$NEON_PROJECT_ID`,
`$WEB_ORIGIN` and so on). Run them in a terminal of their own with the file
loaded, and load it again after adding a value:

```bash
set -a; . ~/.config/interview-agent/secrets.prod.env; set +a
```

Never develop from that terminal, and close it when you are done: the app
reads these names from the environment ahead of `.env`, so a local API started
there would report to production's Sentry and Grafana. For a single command
elsewhere, load the file in a subshell instead:

```bash
( set -a; . ~/.config/interview-agent/secrets.prod.env; set +a; <command> )
```

## Neon

Create an organization API key in the Neon Console (Organization settings >
API keys), put it in the secrets file as `NEON_API_KEY` and load the file;
with a personal key, also pass `-var org_id=org-...` to `apply`.

```bash
terraform -chdir=infra/neon init
terraform -chdir=infra/neon apply
terraform -chdir=infra/neon output -raw project_id   # NEON_PROJECT_ID
```

Put the project id in the secrets file as `NEON_PROJECT_ID` and load the file
again: the Neon Auth commands below need it.

The project runs Postgres 16 on the Free plan's limits: 6 hours of restore
history, and the compute suspends after 5 idle minutes (the API pings pooled
connections before reusing them). Apply the migrations against it:

```bash
DATABASE_URL="$(terraform -chdir=infra/neon output -raw database_url)" \
  uv run alembic upgrade head
```

`database_url` is the `DATABASE_URL` for the API and the worker too. It holds
the database password, so pass it straight from `terraform output` into the
command or the platform that needs it, and never print it into a chat or a
log. It points at the direct host, not the pooler: the app keeps its own pool
and asyncpg's prepared statements break behind PgBouncer. It asks for
`ssl=verify-full`, so asyncpg checks Neon's certificate and host name against
the CA bundle in `PGSSLROOTCERT` and refuses to connect without one. For a
`verify-ca` or `verify-full` URL the app defaults it to certifi's bundle
(`interview_agent/config.py`), so nothing needs exporting; asyncpg does not use
the system store, and `sslrootcert` cannot go in this URL.

### Neon Auth

Neon Auth is set up with the Neon CLI, which reads `NEON_API_KEY` from the
environment. Enable it on the app's database and read its URL:

```bash
neon neon-auth enable --project-id "$NEON_PROJECT_ID" --database-name interview
neon neon-auth status --project-id "$NEON_PROJECT_ID" -o json
```

The `base_url` that `status` prints is `NEON_AUTH_URL` for the API and
`VITE_NEON_AUTH_URL` for the web build; put it in the secrets file. The Google and GitHub apps below need
it for their callback URLs. Once they exist, give Neon Auth their credentials
and turn off email and password sign-in, which the app does not offer:

```bash
neon neon-auth oauth-provider add --project-id "$NEON_PROJECT_ID" --provider-id google \
  --oauth-client-id "$GOOGLE_OAUTH_CLIENT_ID" --oauth-client-secret "$GOOGLE_OAUTH_CLIENT_SECRET"
neon neon-auth oauth-provider add --project-id "$NEON_PROJECT_ID" --provider-id github \
  --oauth-client-id "$GITHUB_OAUTH_CLIENT_ID" --oauth-client-secret "$GITHUB_OAUTH_CLIENT_SECRET"
neon neon-auth config email-password update --project-id "$NEON_PROJECT_ID" --enabled false
```

When `neon neon-auth oauth-provider list` already shows a provider (Neon's
shared app for Google, say), run `oauth-provider update` with the same flags
instead of `add`: it replaces the shared credentials with yours.

Trusting the web app's origin waits until it exists: see
[After the first deploy](#after-the-first-deploy).

## Google sign-in

In a Google Cloud project of its own, open **Google Auth Platform**:

1. **Branding**: the app name, a support email and a developer contact.
   The links are the web app's: home page `<web-origin>`, privacy policy
   `<web-origin>/privacy` and terms of service `<web-origin>/terms` (both
   pages are public and prerendered, so Google can read them). **Authorized
   domains** lists the top private domain of every one of those URLs and of
   the redirect URI: `neon.tech` for Neon Auth's, and the web app's own
   domain. A `<name>.vercel.app` origin counts as its own top private domain,
   because `vercel.app` is a public suffix. The links can wait until the web
   app has its origin.
2. **Audience**: External. While the app is in Testing, only the test users
   listed there can sign in; add yourself. **Publish app** once the branding
   links work: the app asks only for `openid`, `email` and `profile`, which
   need no verification review.
3. **Clients** > Create client > Web application. Authorized redirect URI:
   `<NEON_AUTH_URL>/callback/google`. No JavaScript origins: the browser never
   talks to Google directly, Neon Auth does. Copy the client id and secret
   into the secrets file, then into Neon Auth (above).

## GitHub sign-in

On GitHub, Settings > Developer settings > OAuth Apps > New OAuth App:

- **Homepage URL**: `<web-origin>`, or `NEON_AUTH_URL`'s origin until the web
  app exists (change it afterwards).
- **Authorization callback URL**: `<NEON_AUTH_URL>/callback/github`.

Register it, generate a client secret, and copy the client id and secret into
the secrets file, then into Neon Auth (above).

## Sentry

Create two projects in one Sentry organization: a **FastAPI** (Python) one for
the API and the worker, and a **React** one for the web app. Their DSNs, from
each project's Settings > Client Keys (DSN), go to `SENTRY_DSN` on the API and
the worker and to `VITE_SENTRY_DSN` on Vercel. A DSN only lets a client send
events, never read them, so it is not a secret; it is still kept with the
rest so every value lives in one place. Set `SENTRY_ENVIRONMENT=production` on
the API and the worker; the web app reports its build mode, `production` on
Vercel. What the reports carry, and what the code scrubs from them, is in
`.env.example` under Error reporting.

## Grafana Cloud

Create a stack; its slug makes `GRAFANA_URL`, `https://<stack>.grafana.net`.
In the stack, Administration > Users and access > Service accounts, add a
service account with the **Admin** role and a token for it: that token is
`GRAFANA_AUTH`. Editor is not enough: it cannot read folders through the API,
and Terraform fails with a 403. With both in the secrets file and the file
loaded (the Grafana provider reads them from the environment):

```bash
terraform -chdir=infra/grafana init
terraform -chdir=infra/grafana apply
```

This creates the **Interview Agent** folder with every dashboard in
`grafana/dashboards/`; edit the JSON files and apply again to update them.

The metrics reach the stack over OTLP. From the Grafana Cloud portal, the
stack's **OpenTelemetry** page gives the endpoint
(`https://otlp-gateway-<region>.grafana.net/otlp`, which is
`OTEL_EXPORTER_OTLP_ENDPOINT`) and the instance id, and generates an access
policy token; it needs the `metrics:write` scope. The header the app sends is
built from both, as `.env.example` shows:

```bash
printf 'Authorization=Basic%%20%s\n' \
  "$(printf '%s' "$OTLP_INSTANCE_ID:$OTLP_TOKEN" | base64 | tr -d '\n')"
# -> OTEL_EXPORTER_OTLP_HEADERS
```

Check that the endpoint accepts the credentials before handing them to the
API and the worker. An empty metrics request answers 200; a wrong instance id
or token answers 401:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/x-protobuf' \
  -H "Authorization: Basic $(printf '%s' "$OTLP_INSTANCE_ID:$OTLP_TOKEN" | base64 | tr -d '\n')" \
  --data-binary '' "$OTEL_EXPORTER_OTLP_ENDPOINT/v1/metrics"
```

## LangSmith (optional)

Production may trace every interview to LangSmith, which is how its runs are
inspected and the interviewer improved. Setting `LANGSMITH_API_KEY` sends
each user's full interview content there, the session audio recording
included (the README's LangSmith paragraph lists what goes where), so the
privacy page at `web/src/routes/privacy.tsx` must say so before the key goes
in. Leaving the key unset sends nothing.

Create an API key in LangSmith's settings and a project of its own
for production, such as `interview-agent-prod`, so its traces never mix with
development's. Set the three variables on **both** the API (planner and
evaluator traces) and the worker (dialogue turns and the voice session with
the recording): `LANGSMITH_API_KEY` as a secret, `LANGSMITH_PROJECT` and
`LANGSMITH_ENDPOINT` (`https://api.smith.langchain.com`, or your region's).
The content then follows that project's retention, and the app never deletes
it.

## FastAPI Cloud: the API

Log in, then create the app and link this directory to it. The link lives in
`.fastapicloud/cloud.json`, which is git-ignored.

```bash
uv run fastapi login          # or: uvx --from fastapi-cloud-cli --with 'fastapi[standard]' fastapi login
uv run fastapi cloud teams list
uv run fastapi cloud apps create --team-id <team-id> --name <app-name> --link
```

Set the variables before the first deploy: the API refuses to start without
the required ones. Pass each value on stdin, so it stays out of the shell
history and the process list, with `--no-redeploy` so the app deploys once at
the end, and `--secret` for the secret ones (it applies when a variable is
created; an existing variable keeps its status):

```bash
printf '%s' "$OPENAI_API_KEY" | uv run fastapi cloud env set OPENAI_API_KEY --value-stdin --no-redeploy --secret
```

The rest follow the same pattern, `--secret` where the table says secret:

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | secret |
| `LIVEKIT_URL` | the LiveKit Cloud project's URL |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | secret, the project's key pair |
| `DATABASE_URL` | secret, `terraform -chdir=infra/neon output -raw database_url` |
| `INTERNAL_API_TOKEN` | secret, production's own (`openssl rand -hex 32`) |
| `AUTH_MODE` | `neon` |
| `NEON_AUTH_URL` | Neon Auth's `base_url` |
| `ADMIN_USER_IDS` | set after the first sign-in (see below) |
| `LIVEKIT_AGENT_NAME` | one of its own, such as `interviewer-prod`; the worker's must match |
| `APP_BASE_URL` | `<api-origin>`, once known; the worker uses it, the API keeps the same value |
| `SENTRY_DSN` | the Python project's DSN |
| `SENTRY_ENVIRONMENT` | `production` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | the Grafana Cloud OTLP endpoint |
| `OTEL_EXPORTER_OTLP_HEADERS` | secret, the Basic header above |
| `LOG_DIR` | empty (`printf ''`): the filesystem is ephemeral, logs go to the console |
| `CORS_ALLOWED_ORIGINS` | `<web-origin>`, once the web app exists |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | optional, see [LangSmith](#langsmith-optional); the key is secret |

Any tuning variable you changed from its `.env.example` default (models,
quotas, limits) goes in the same way. `INTERVIEW_IDLE_MINUTES`,
`INTERVIEW_RECONNECT_SECONDS` and `CLOSING_TIMEOUT_SECONDS` must match the
worker's: the API's reconciliation and the worker's timers both use them to
decide when an interview is idle or finished. Then deploy once by hand and
read the app's id and origin:

```bash
uv run fastapi deploy
uv run fastapi cloud apps get --json      # id is <app-id> (FASTAPI_CLOUD_APP_ID), url is <api-origin>
```

The app runs within FastAPI Cloud's default limits, 500 MB of memory and
0.5 vCPU, and scales to zero when idle: the first request after a quiet spell
wakes it, and the web app says so while it waits.

Later deploys come from the workflow, which needs a deploy token:

```bash
uv run fastapi cloud tokens create --app-id <app-id> --name github-actions \
  --output-file <token-file> --json
```

The token becomes the `FASTAPI_CLOUD_TOKEN` repository secret and the app's id
`FASTAPI_CLOUD_APP_ID` (see [GitHub](#github)); delete the token file
afterwards. It expires after 365 days by default. `uv run fastapi cloud
setup-ci --secrets-only` does the same in one interactive step, with prompts.

## Vercel: the web app

Import the GitHub repository as a Vercel project with **Root Directory**
`web`; the rest comes from `web/vercel.json`. Vercel builds with Node.js 24.x,
its default and the version CI uses, and the pnpm that `packageManager` in
`web/package.json` pins. Vercel refuses to build a release of TanStack Start
with a known vulnerability, so keep that dependency current.

The CLI commands need `web/` linked to the project. Run them from `web/`, with
the token, starting with the link:

```bash
cd web
npx -y vercel@latest link --project <vercel-project> --yes --token "$VERCEL_TOKEN"
```

`link` writes `.vercel/` and a `.env.local` with a `VERCEL_OIDC_TOKEN`, both
git-ignored. The build variables are baked into the bundle. Add them for
production and again for preview, with `--type config`: Vercel otherwise
refuses `VITE_` names that look like credentials, since everything `VITE_`
ends up public.

```bash
printf '%s' "$API_ORIGIN/api" | npx -y vercel@latest env add VITE_API_BASE_URL production \
  --type config --yes --token "$VERCEL_TOKEN"
printf '%s' "$NEON_AUTH_URL" | npx -y vercel@latest env add VITE_NEON_AUTH_URL production \
  --type config --yes --token "$VERCEL_TOKEN"
printf '%s' "$VITE_SENTRY_DSN" | npx -y vercel@latest env add VITE_SENTRY_DSN production \
  --type config --yes --token "$VERCEL_TOKEN"
# the same three again with preview in place of production
```

The Git integration builds every push to `main`, so there is no CLI deploy:
after adding the variables, redeploy the latest build from the dashboard or
push a commit. A CLI deploy from the repository root would upload the whole
tree and hit the free plan's file limit. `npx -y vercel@latest project ls
--token "$VERCEL_TOKEN"` shows the production URL, `<web-origin>`; it prints
the table on stderr. The steps after this one run from the repository root
again (`cd ..`).

## LiveKit Cloud: the worker

Production uses the same LiveKit Cloud project as development; its own
`LIVEKIT_AGENT_NAME` keeps a local worker from taking production interviews.
On a fresh account, create the project at cloud.livekit.io; its URL and key
pair are under Settings > Keys (`LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET` in the secrets file). Save the project for the CLI:

```bash
lk project add <name> --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" \
  --api-secret "$LIVEKIT_API_SECRET" --default
```

Check that agent observability is turned off in the project's settings. With
LangSmith on, the worker records each session's audio, and with observability
on LiveKit Cloud would keep a copy too, which the privacy page does not
disclose.

Write the worker's variables to a file outside the repository, such as
`~/.config/interview-agent/worker.prod.env` (chmod 600), one `KEY=value` per
line: `OPENAI_API_KEY`, the model variables (`INTERVIEWER_MODEL`,
`INTERVIEWER_REASONING_EFFORT`, `PLANNER_MODEL`, `EVALUATOR_MODEL` and their
efforts, if they differ from the defaults), `STT_MODEL`,
`INTERVIEW_MAX_MINUTES`, `DATABASE_URL`, `APP_BASE_URL` (`<api-origin>`),
`INTERNAL_API_TOKEN`, `LIVEKIT_AGENT_NAME`, `SENTRY_DSN`,
`SENTRY_ENVIRONMENT=production`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, with LangSmith the three `LANGSMITH_*`, and any
lifecycle tuning you changed on the API (`INTERVIEW_IDLE_MINUTES`,
`INTERVIEW_RECONNECT_SECONDS`, `CLOSING_TIMEOUT_SECONDS`), with the same
values. `DATABASE_URL`, `INTERNAL_API_TOKEN` and `LIVEKIT_AGENT_NAME` hold the
API's values. Leave out `LIVEKIT_URL`, `LIVEKIT_API_KEY` and
`LIVEKIT_API_SECRET`: LiveKit Cloud injects them.

The `livekit.toml` committed in the repository names this deployment's
project and agent, and every cloud `lk agent` command refuses to run when it
names another project (`project does not match agent subdomain`). On a fresh
setup, delete it first; the one `create` writes replaces it in the next
commit. Then create the agent from the repository root:

```bash
rm livekit.toml
lk agent create --project <name> --secrets-file <worker-env-file> --region us-east --yes .
```

LiveKit Cloud builds the Dockerfile, whose last stage is the worker.
`--region` is required when the CLI runs without prompts. The command writes
`livekit.toml`, which names the agent: commit it before the next push to
`main`, or the deploy workflow cannot update the worker (the README's Deploy
section explains why that matters after a migration). From then on, the
workflow runs `lk agent deploy` for code changes; variables change with
`lk agent update-secrets --secrets-file <worker-env-file>`, which restarts the
agent. `lk agent status` shows whether it is running and `lk agent logs` tails
it (`--log-type build` for the build).

## GitHub

The workflows need these repository secrets (the repository's Settings >
Secrets and variables > Actions, or `gh secret set`):

| Secret | Used by | Value |
| --- | --- | --- |
| `DATABASE_URL` | `deploy.yml` (migrations) | Neon's `database_url` |
| `FASTAPI_CLOUD_TOKEN` | `deploy.yml` (API) | the deploy token |
| `FASTAPI_CLOUD_APP_ID` | `deploy.yml` (API) | the app's id |
| `API_ORIGIN` | both | `<api-origin>` |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | `deploy.yml` (worker) | the LiveKit Cloud project's |
| `INTERNAL_API_TOKEN` | `maintenance.yml` | the API's value |

Feed each value on stdin, never as an argument:

```bash
terraform -chdir=infra/neon output -raw database_url | gh secret set DATABASE_URL
gh secret set FASTAPI_CLOUD_TOKEN < <token-file>
printf '%s' "$API_ORIGIN" | gh secret set API_ORIGIN
```

`deploy.yml` runs after CI passes on `main`: migrations first, then the API
and the worker. `maintenance.yml` calls the daily retention purge and
lifecycle sweep. Run **Maintenance** once by hand from the Actions tab: a
green run proves `API_ORIGIN`, `INTERNAL_API_TOKEN` and the API together.

## After the first deploy

With both origins known:

1. Set `CORS_ALLOWED_ORIGINS=<web-origin>` on the API (and `APP_BASE_URL`, if
   it is still unset), this time without `--no-redeploy`, so the API restarts
   with it.
2. Trust the web app's origin in Neon Auth, and stop trusting localhost once
   local development no longer signs in against this Auth project:

   ```bash
   neon neon-auth domain add --project-id "$NEON_PROJECT_ID" "$WEB_ORIGIN"
   neon neon-auth domain allow-localhost disable --project-id "$NEON_PROJECT_ID"
   ```

3. Fill in the Google branding links and authorized domains, publish the app,
   and set the GitHub OAuth app's homepage to `<web-origin>`.
4. Sign in once from `<web-origin>` and read your id: `/admin/users` shows it
   only to an admin, so the first one comes from the database (the Neon
   Console's SQL Editor: `select owner_id, email from user_profiles;`), or
   from development if it signs in against this same Neon Auth project. Set
   `ADMIN_USER_IDS` on the API with a redeploy: the API reads it at startup.
   Later admins sign in once and are read from `/admin/users`.

## Verification

- `curl <api-origin>/api/healthz` answers 200, and
  `curl <api-origin>/api/auth/config` answers `{"mode":"neon"}`.
- `curl -i <api-origin>/api/me` without a token answers 401.
- A CORS preflight from the web origin is allowed:
  `curl -si -X OPTIONS <api-origin>/api/me -H 'Origin: <web-origin>' -H 'Access-Control-Request-Method: GET' -H 'Access-Control-Request-Headers: authorization'`
  answers 200 with `access-control-allow-origin: <web-origin>`.
- Signing in with Google and with GitHub from `<web-origin>` works.
- Grafana Cloud's Explore shows `interview_agent_*` series, and the
  **Interview Agent** dashboards fill in.
- A test event reaches the Sentry Python project, and the same with
  `VITE_SENTRY_DSN` in place of `SENTRY_DSN` reaches the React one:

  ```bash
  uv run python -c 'import os, sentry_sdk; sentry_sdk.init(os.environ["SENTRY_DSN"], environment="production"); sentry_sdk.capture_message("setup check"); sentry_sdk.flush()'
  ```

  That only proves the DSN. To prove it reached the Vercel build, run
  `setTimeout(() => { throw new Error("setup check") })` in the browser
  console on `<web-origin>`: an `Error` (its message scrubbed) shows up under
  the React project with environment `production`.
- LiveKit Cloud's project settings show agent observability off.

- `lk agent status` shows the agent running.
- A full voice interview, from the plan to the results; with LangSmith, its
  traces appear in the production project.

## State

Terraform state stays in each directory (`terraform.tfstate`) and is
git-ignored, as are `*.tfvars` and `.terraform/`. Keep the Neon state safe: it
contains the database password. The `.terraform.lock.hcl` files are tracked so
every machine uses the same provider builds.
