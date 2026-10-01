/** @vitest-environment jsdom */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, expect, it, vi } from "vitest"
import { getEvaluationHistory } from "@/lib/api"
import type { Evaluation } from "@/lib/api"
import { EvaluationHistory } from "./evaluation-history"

vi.mock("@/lib/api", () => ({ getEvaluationHistory: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})
const result: Evaluation = {
  score: 82,
  hired: true,
  evaluation_status: "complete",
  coverage: 1,
  strengths: ["Explained the tradeoff"],
  weaknesses: ["Practise measuring latency"],
  rationale: "A supported historical conclusion",
  seniority_evaluated: "junior",
  calibration_notes: ["Senior design was not required"],
  ended_by: "plan_complete",
  criteria: [
    {
      milestone_id: "m1",
      assessment: "meets",
      rationale: "Specific evidence",
      evidence: [
        { message_id: "42", message_version: 2, quote: "I measured the query" },
      ],
      practice: "Compare two indexes",
    },
  ],
}
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <EvaluationHistory
        interviewId="interview"
        milestones={[]}
        duration="Up to 8 min"
      />
    </QueryClientProvider>
  )
  const details = screen.getByText("Evaluation history")
    .parentElement as HTMLDetailsElement
  details.open = true
  fireEvent(details, new Event("toggle"))
  return client
}
it("renders the complete feedback of each attempt under its request", async () => {
  vi.mocked(getEvaluationHistory).mockResolvedValue({
    current_request_id: "current",
    requests: [
      {
        id: "current",
        automatic: false,
        status: "running",
        attempts: 1,
        created_at: "today",
        seal_version: 2,
      },
    ],
    attempts: [
      {
        id: "run",
        request_id: "current",
        ordinal: 1,
        status: "completed",
        result,
        error: null,
        created_at: "yesterday",
      },
    ],
  })
  const client = mount()
  await screen.findByText("Saved answers version 2")
  expect(screen.getByText("82")).toBeTruthy()
  expect(screen.getByText("Explained the tradeoff")).toBeTruthy()
  expect(screen.getByText("Practise measuring latency")).toBeTruthy()
  expect(screen.getByText("Senior design was not required")).toBeTruthy()
  expect(screen.getByText(/I measured the query/)).toBeTruthy()
  expect(screen.getByText("Compare two indexes")).toBeTruthy()
  expect(screen.queryByText(/No assessment history/)).toBeNull()
  client.clear()
})
it("hides invalidated historical scores but keeps criterion evidence and its explanation", async () => {
  vi.mocked(getEvaluationHistory).mockResolvedValue({
    current_request_id: "later",
    requests: [
      {
        id: "old",
        automatic: true,
        status: "completed",
        attempts: 1,
        created_at: "yesterday",
        seal_version: 1,
      },
    ],
    attempts: [
      {
        id: "old-run",
        request_id: "old",
        ordinal: 1,
        status: "completed",
        result,
        invalidated: true,
        error: null,
        created_at: "yesterday",
      },
    ],
  })
  const client = mount()
  await screen.findByText(/Earlier request/)
  expect(screen.queryByText("82")).toBeNull()
  expect(screen.queryByText(/Hired ·/)).toBeNull()
  expect(screen.getByRole("alert").textContent).toContain("confirmed omission")
  expect(screen.getByText(/I measured the query/)).toBeTruthy()
  client.clear()
})
it("allows reloading history after a transient fetch failure without creating a request", async () => {
  vi.mocked(getEvaluationHistory)
    .mockRejectedValueOnce(new Error("Offline"))
    .mockResolvedValueOnce({
      current_request_id: null,
      requests: [],
      attempts: [],
    })
  const client = mount()
  await screen.findByRole("alert")
  fireEvent.click(screen.getByText("Reload history"))
  await screen.findByText(
    "No assessment history was recorded for this interview."
  )
  await waitFor(() => expect(getEvaluationHistory).toHaveBeenCalledTimes(2))
  client.clear()
})

it("refreshes an open history while a request runs so completion becomes visible", async () => {
  const pending = {
    current_request_id: "current",
    requests: [
      {
        id: "current",
        automatic: false,
        status: "running",
        attempts: 1,
        created_at: "today",
      },
    ],
    attempts: [],
  }
  vi.mocked(getEvaluationHistory)
    .mockResolvedValueOnce(pending)
    .mockResolvedValue({
      ...pending,
      requests: [{ ...pending.requests[0], status: "completed" }],
      attempts: [
        {
          id: "new-run",
          request_id: "current",
          ordinal: 1,
          status: "completed",
          result,
          error: null,
          created_at: "today",
        },
      ],
    })
  const client = mount()
  await screen.findByText(/In progress/)
  await screen.findByText(
    "A supported historical conclusion",
    {},
    { timeout: 3000 }
  )
  expect(getEvaluationHistory).toHaveBeenCalledTimes(2)
  client.clear()
})
