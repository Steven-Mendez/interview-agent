resource "neon_project" "this" {
  name       = var.project_name
  region_id  = var.region_id
  pg_version = var.pg_version
  org_id     = var.org_id

  # The Free plan keeps at most 6 hours of restore history; the provider's
  # default of one day is rejected there.
  history_retention_seconds = 21600

  branch {
    database_name = var.database_name
    role_name     = var.role_name
  }
}
