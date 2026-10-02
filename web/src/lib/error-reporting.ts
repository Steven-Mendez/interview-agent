import * as Sentry from "@sentry/react"
import type { ErrorEvent } from "@sentry/react"

// Browser errors to Sentry without interview content: exception types and
// stack frames only. Messages, request data, extras, breadcrumbs and any user
// field besides the opaque account id never leave the page. Without
// VITE_SENTRY_DSN (tests, local dev, the prerendered shell) nothing starts.

let initialized = false

/** Strip an event down to what locates the failure. */
export function beforeSend(event: ErrorEvent): ErrorEvent {
  for (const exception of event.exception?.values ?? []) {
    exception.value = ""
  }
  delete event.message
  delete event.logentry
  if (event.request) {
    const { method, url } = event.request
    event.request = { method, url: url?.split(/[?#]/)[0] }
  }
  delete event.extra
  delete event.breadcrumbs
  const id = event.user?.id
  if (id === undefined) {
    delete event.user
  } else {
    event.user = { id }
  }
  return event
}

/** Starts reporting once, in the browser, when a DSN is configured. */
export function initErrorReporting(): void {
  const dsn = import.meta.env.VITE_SENTRY_DSN
  if (initialized || typeof window === "undefined" || !dsn) return
  initialized = true
  Sentry.init({
    dsn,
    environment: import.meta.env.MODE,
    // Every category is set: dataCollection collects whatever it leaves out.
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: { request: false, response: false },
      httpBodies: [],
      urlQueryParams: false,
      graphQL: { document: false, variables: false },
      genAI: { inputs: false, outputs: false },
      databaseQueryData: false,
      queues: false,
      stackFrameVariables: false,
      frameContextLines: 0,
    },
    beforeBreadcrumb: () => null,
    beforeSend,
  })
}

/** Reports an error a route's error boundary caught. */
export function captureRouteError(error: unknown): void {
  if (initialized) Sentry.captureException(error)
}

/** Names the signed-in account by its opaque id; null after sign-out. */
export function setErrorReportingUser(id: string | null): void {
  if (initialized) Sentry.setUser(id === null ? null : { id })
}
