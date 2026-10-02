/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { ErrorEvent } from "@sentry/react"

const mocks = vi.hoisted(() => ({
  init: vi.fn(),
  captureException: vi.fn(),
  setUser: vi.fn(),
}))
vi.mock("@sentry/react", () => mocks)

const CANARY = "CANARY_CV_TRANSCRIPT_SECRET_c41f9"

async function load() {
  vi.resetModules()
  return import("./error-reporting")
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe("beforeSend", () => {
  it("keeps the exception type and the account id, nothing else", async () => {
    const { beforeSend } = await load()
    const event: ErrorEvent = {
      type: undefined,
      message: CANARY,
      logentry: { message: CANARY, params: [CANARY] },
      exception: {
        values: [
          {
            type: "TypeError",
            value: CANARY,
            stacktrace: { frames: [{ function: "parse", lineno: 3 }] },
          },
        ],
      },
      request: {
        method: "GET",
        url: `https://app.example/interviews/1?q=${CANARY}#${CANARY}`,
        query_string: `q=${CANARY}`,
        headers: { Authorization: `Bearer ${CANARY}` },
        cookies: { session: CANARY },
        data: { resume: CANARY },
      },
      extra: { resume: CANARY },
      breadcrumbs: [{ message: CANARY }],
      user: { id: "user-synthetic", email: CANARY, ip_address: "10.0.0.1" },
    }

    const result = beforeSend(event)

    expect(JSON.stringify(result)).not.toContain(CANARY)
    expect(result.exception?.values?.[0]).toEqual({
      type: "TypeError",
      value: "",
      stacktrace: { frames: [{ function: "parse", lineno: 3 }] },
    })
    expect(result.request).toEqual({
      method: "GET",
      url: "https://app.example/interviews/1",
    })
    expect(result.user).toEqual({ id: "user-synthetic" })
  })

  it("drops a user without an id", async () => {
    const { beforeSend } = await load()
    const result = beforeSend({ type: undefined, user: { email: CANARY } })
    expect(result.user).toBeUndefined()
  })
})

describe("initErrorReporting", () => {
  it("does nothing without VITE_SENTRY_DSN", async () => {
    vi.stubEnv("VITE_SENTRY_DSN", "")
    const reporting = await load()

    reporting.initErrorReporting()
    reporting.captureRouteError(new Error(CANARY))
    reporting.setErrorReportingUser("user-synthetic")

    expect(mocks.init).not.toHaveBeenCalled()
    expect(mocks.captureException).not.toHaveBeenCalled()
    expect(mocks.setUser).not.toHaveBeenCalled()
  })

  it("starts once with restrictive data collection and no tracing", async () => {
    vi.stubEnv("VITE_SENTRY_DSN", "https://synthetic@o0.ingest.sentry.io/0")
    const reporting = await load()

    reporting.initErrorReporting()
    reporting.initErrorReporting()

    expect(mocks.init).toHaveBeenCalledTimes(1)
    const options = mocks.init.mock.calls[0][0]
    expect(options.dataCollection).toMatchObject({
      userInfo: false,
      cookies: false,
      httpBodies: [],
      urlQueryParams: false,
      genAI: { inputs: false, outputs: false },
    })
    expect(options.tracesSampleRate).toBeUndefined()
    expect(options.integrations).toBeUndefined()
    expect(options.beforeBreadcrumb({ message: CANARY })).toBeNull()

    reporting.setErrorReportingUser("user-synthetic")
    expect(mocks.setUser).toHaveBeenCalledWith({ id: "user-synthetic" })
  })
})
