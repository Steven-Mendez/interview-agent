/** @vitest-environment jsdom */
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import type { Interview } from "@/lib/api"
import { InterviewEnding } from "./interview-ending"
import { ClosingPanel } from "./closing-panel"

vi.mock("@tanstack/react-router", () => ({
  Link: (props: Record<string, unknown>) => <a {...props} />,
}))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const interview = (fields: Partial<Interview>) =>
  ({
    ended_reason: null,
    farewell_status: "played",
    farewell_text: null,
    closing_remaining_seconds: null,
    ...fields,
  }) as Interview

it("explains a lost worker and shows the farewell in writing", () => {
  render(
    <InterviewEnding
      interview={interview({
        ended_reason: "worker_lost",
        farewell_status: "not_possible",
        farewell_text: "Gracias por compartir tu experiencia.",
      })}
    />
  )
  expect(
    screen.getByText(/connection to the interviewer was lost/)
  ).toBeTruthy()
  expect(screen.getByText(/session was no longer connected/)).toBeTruthy()
  expect(screen.getByText("Gracias por compartir tu experiencia.")).toBeTruthy()
})

it("stays silent for a normal ending with a played farewell", () => {
  const view = render(
    <InterviewEnding interview={interview({ ended_reason: "plan_complete" })} />
  )
  expect(view.container.textContent).toBe("")
})

it("anchors reload supervision to the server bound without extending it", () => {
  vi.useFakeTimers()
  let monotonic = 1000
  vi.spyOn(performance, "now").mockImplementation(() => monotonic)
  const view = render(
    <ClosingPanel
      interview={interview({
        status: "closing",
        closing_remaining_seconds: 10,
      })}
    />
  )
  expect(screen.getByText(/Closing the interview/)).toBeTruthy()
  monotonic += 5000
  act(() => vi.advanceTimersByTime(5000))
  // A later sample with more time left must not push the deadline back.
  view.rerender(
    <ClosingPanel
      interview={interview({
        status: "closing",
        closing_remaining_seconds: 40,
      })}
    />
  )
  monotonic += 5000
  act(() => vi.advanceTimersByTime(5000))
  expect(screen.getByText(/Closing recovery is pending/)).toBeTruthy()
})
