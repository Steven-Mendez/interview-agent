/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Browser error reports (see lib/error-reporting); unset: none are sent.
  readonly VITE_SENTRY_DSN?: string
}
