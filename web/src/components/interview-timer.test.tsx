/** @vitest-environment jsdom */
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { InterviewTimer } from "./interview-timer"

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})
it("shows the saved elapsed time after remounting instead of resetting on reconnect", () => {
  const first = render(<InterviewTimer elapsedSeconds={123} />)
  expect(screen.getByLabelText("Elapsed interview time").textContent).toContain(
    "02:03"
  )
  first.unmount()
  render(<InterviewTimer elapsedSeconds={126} />)
  expect(screen.getByLabelText("Elapsed interview time").textContent).toContain(
    "02:06"
  )
})
it("uses a monotonic interval between server samples even if the browser clock jumps", () => {
  vi.useFakeTimers()
  let monotonic = 10_000
  vi.spyOn(performance, "now").mockImplementation(() => monotonic)
  const view = render(<InterviewTimer elapsedSeconds={123} />)
  vi.setSystemTime(new Date("2040-01-01T00:00:00Z"))
  monotonic += 2000
  act(() => vi.advanceTimersByTime(2000))
  expect(screen.getByLabelText("Elapsed interview time").textContent).toContain(
    "02:05"
  )
  view.rerender(<InterviewTimer elapsedSeconds={130} />)
  expect(screen.getByLabelText("Elapsed interview time").textContent).toContain(
    "02:10"
  )
  monotonic += 1000
  vi.setSystemTime(new Date("2000-01-01T00:00:00Z"))
  act(() => vi.advanceTimersByTime(1000))
  expect(screen.getByLabelText("Elapsed interview time").textContent).toContain(
    "02:11"
  )
  view.unmount()
  expect(vi.getTimerCount()).toBe(0)
})
it.each([undefined, null, NaN, Infinity, -1])(
  "does not invent a new start when server elapsed time is unavailable (%s)",
  (value) => {
    render(<InterviewTimer elapsedSeconds={value} />)
    expect(
      screen.getByLabelText("Elapsed interview time unavailable").textContent
    ).toContain("--:--")
  }
)
