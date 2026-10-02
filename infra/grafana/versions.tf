terraform {
  required_version = ">= 1.6"

  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.47"
    }
  }
}

# Configured from the environment: GRAFANA_URL is the stack URL and
# GRAFANA_AUTH a service account token.
provider "grafana" {}
