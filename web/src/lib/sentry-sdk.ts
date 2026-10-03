// The part of the Sentry SDK error-reporting uses, imported on demand.
// Named imports keep the chunk to what they need: a dynamic import of
// @sentry/react itself would keep every export of the package.
export { captureException, init, setUser } from "@sentry/react"
