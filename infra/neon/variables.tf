variable "project_name" {
  description = "Neon project name."
  type        = string
  default     = "interview-agent"
}

variable "region_id" {
  description = "Neon region the project runs in."
  type        = string
  default     = "aws-us-east-2"
}

variable "pg_version" {
  description = "Postgres major version, the same as local development and CI."
  type        = number
  default     = 16
}

variable "database_name" {
  description = "Database the API and the worker connect to."
  type        = string
  default     = "interview"
}

variable "role_name" {
  description = "Role that owns the database."
  type        = string
  default     = "interview"
}

variable "org_id" {
  description = "Neon organization to create the project in. Required with a personal API key; an organization API key implies it."
  type        = string
  default     = null
}
