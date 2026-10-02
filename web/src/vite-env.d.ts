/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Browser error reports (see lib/error-reporting); unset: none are sent.
  readonly VITE_SENTRY_DSN?: string
  // Neon Auth endpoint (see lib/auth); unset: no sign-in, for an API running
  // AUTH_MODE=local.
  readonly VITE_NEON_AUTH_URL?: string
  // The API on another origin, ending in /api (see lib/api); unset: "/api"
  // on this one.
  readonly VITE_API_BASE_URL?: string
}
