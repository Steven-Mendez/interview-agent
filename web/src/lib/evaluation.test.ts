import { afterEach, describe, expect, it, vi } from "vitest"

import { evaluationAnchor } from "./evaluation"

const UPDATED_AT = "2026-09-14T10:00:00Z"
const UPDATED_MS = Date.parse(UPDATED_AT)
const ENDED_MS = UPDATED_MS - 45_000

describe("evaluationAnchor", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it("follows the heartbeat while the evaluation runs", () => {
    // The disconnect this tab saw is older than the last heartbeat: the
    // heartbeat wins, or a slow-but-alive run would time out.
    expect(evaluationAnchor("evaluating", UPDATED_AT, ENDED_MS)).toBe(
      UPDATED_MS
    )
    expect(evaluationAnchor("evaluating", UPDATED_AT, null)).toBe(UPDATED_MS)
  })

  it("counts from the disconnect this tab saw until the run is claimed", () => {
    expect(evaluationAnchor("completed", UPDATED_AT, ENDED_MS)).toBe(ENDED_MS)
    expect(evaluationAnchor("interviewing", UPDATED_AT, ENDED_MS)).toBe(
      ENDED_MS
    )
  })

  it("falls back to the row's updated_at on a revisit with no disconnect", () => {
    expect(evaluationAnchor("completed", UPDATED_AT, null)).toBe(UPDATED_MS)
  })

  it("falls back to now for an unparseable timestamp", () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-09-14T12:34:56Z"))
    const now = Date.now()
    expect(evaluationAnchor("evaluating", "not a date", ENDED_MS)).toBe(now)
    expect(evaluationAnchor("completed", "", null)).toBe(now)
    // A disconnect in this tab still beats the fallback.
    expect(evaluationAnchor("completed", "", ENDED_MS)).toBe(ENDED_MS)
  })
})
