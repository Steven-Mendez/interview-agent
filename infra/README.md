# Infrastructure

Terraform for the managed services: the Postgres database on Neon (`neon/`)
and the dashboards on Grafana Cloud (`grafana/`). Each directory is its own
Terraform root with local state. The dashboards in `grafana/dashboards/` are
the same files the local `lgtm` container provisions. The application side
(the API, the web app and the worker, and the workflow that deploys them) is
in the README's [Deploy](../README.md#deploy) section.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) 1.6 or later
- A Neon API key (an organization key; with a personal key, also pass
  `-var org_id=org-...` to `apply`)
- A Grafana Cloud stack and a service account token for it with permission to
  manage folders and dashboards
- Node.js, for the Neon CLI (`npx neonctl@latest`, or `npm i -g neonctl` for
  the `neon` command used below)

## Neon

```bash
export NEON_API_KEY=...
terraform -chdir=infra/neon init
terraform -chdir=infra/neon apply
```

The project runs Postgres 16 on the Free plan's limits: 6 hours of restore
history, and the compute suspends after 5 idle minutes (the API pings pooled
connections before reusing them). Apply the migrations against it:

```bash
DATABASE_URL="$(terraform -chdir=infra/neon output -raw database_url)" \
  uv run alembic upgrade head
```

`database_url` is the `DATABASE_URL` for the API and the worker too. It points
at the direct host, not the pooler: the app keeps its own pool and asyncpg's
prepared statements break behind PgBouncer. It asks for `ssl=verify-full`, so
asyncpg checks Neon's certificate and host name against the CA bundle in
`PGSSLROOTCERT` and refuses to connect without one. For a `verify-ca` or
`verify-full` URL the app defaults it to certifi's bundle
(`interview_agent/config.py`), so nothing needs exporting; asyncpg does not use
the system store, and `sslrootcert` cannot go in this URL.

### Neon Auth

Neon Auth is set up with the Neon CLI:

```bash
PROJECT_ID="$(terraform -chdir=infra/neon output -raw project_id)"
npx neonctl@latest neon-auth enable --project-id "$PROJECT_ID" --database-name interview
neon neon-auth oauth-provider add --project-id "$PROJECT_ID" \
  --provider-id google --oauth-client-id ... --oauth-client-secret ...
neon neon-auth oauth-provider add --project-id "$PROJECT_ID" \
  --provider-id github --oauth-client-id ... --oauth-client-secret ...
neon neon-auth config email-password update --project-id "$PROJECT_ID" --enabled false
neon neon-auth status --project-id "$PROJECT_ID" -o json
```

The `base_url` that `status` prints is `NEON_AUTH_URL` for the API and
`VITE_NEON_AUTH_URL` for the frontend build. In production, trust only the
frontend's origin:

```bash
neon neon-auth domain add --project-id "$PROJECT_ID" https://<frontend-origin>
neon neon-auth domain allow-localhost disable --project-id "$PROJECT_ID"
```

## Grafana Cloud

```bash
export GRAFANA_URL=https://<stack>.grafana.net
export GRAFANA_AUTH=<service account token>
terraform -chdir=infra/grafana init
terraform -chdir=infra/grafana apply
```

This creates the **Interview Agent** folder with every dashboard in
`grafana/dashboards/`; edit the JSON files and apply again to update them.
Sending the metrics there is configured in `.env` (`OTEL_EXPORTER_OTLP_*`, see
`.env.example`).

## State

Terraform state stays in each directory (`terraform.tfstate`) and is
git-ignored, as are `*.tfvars` and `.terraform/`. Keep the Neon state safe: it
contains the database password. The `.terraform.lock.hcl` files are tracked so
every machine uses the same provider builds.
