output "project_id" {
  description = "Neon project ID (for the Neon CLI's --project-id)."
  value       = neon_project.this.id
}

output "database_host" {
  description = "Direct (unpooled) host of the default branch's compute."
  value       = neon_project.this.database_host
}

# The app keeps its own connection pool and asyncpg's prepared statements
# break behind PgBouncer, so it connects to the direct host, not the pooler.
# asyncpg takes ssl=... and rejects libpq's sslmode and channel_binding.
# verify-full checks Neon's certificate and host name against the CA bundle in
# PGSSLROOTCERT (the app defaults it to certifi's; asyncpg ignores the system
# store, and ssl=require without a bundle would accept any certificate).
output "database_url" {
  description = "DATABASE_URL for the API, the worker and Alembic."
  value       = "postgresql+asyncpg://${urlencode(neon_project.this.database_user)}:${urlencode(neon_project.this.database_password)}@${neon_project.this.database_host}/${neon_project.this.database_name}?ssl=verify-full"
  sensitive   = true
}
