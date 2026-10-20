import { expect, it } from "vitest"
import { formatDate, formatDateTime } from "./accounts"

it.each([formatDate, formatDateTime])(
  "renders malformed account dates without crashing the page",
  (format) => {
    for (const value of ["", "not-a-date", "2026-13-40T00:00:00Z"]) {
      expect(format(value)).toBe("—")
    }
    expect(format("2026-10-20T12:00:00Z")).not.toBe("—")
  }
)
