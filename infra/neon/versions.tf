terraform {
  required_version = ">= 1.6"

  required_providers {
    neon = {
      source  = "kislerdm/neon"
      version = "~> 0.18"
    }
  }
}

# Authenticates with NEON_API_KEY from the environment.
provider "neon" {}
