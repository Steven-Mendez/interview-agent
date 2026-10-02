// Where the API lives. Every endpoint is under the `/api` prefix — the dev
// proxy (vite.config.ts) and the prod SPA fallback both key off that single
// prefix. A build for an API on another origin names it in VITE_API_BASE_URL
// (ending in that same `/api`). Its own module so that api.ts, auth.ts and
// local-auth.ts can share it without importing each other.

export const API_BASE =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/+$/, "") || "/api"
