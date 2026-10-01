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
import {
  createReviewedSnapshot,
  evaluateInterview,
  getSealHistory,
  reviewCaptureIncident,
} from "@/lib/api"
import { TranscriptReview } from "./transcript-review"

vi.mock("@/lib/api", () => ({
  createReviewedSnapshot: vi.fn(),
  evaluateInterview: vi.fn(),
  getSealHistory: vi.fn(),
  reviewCaptureIncident: vi.fn(),
}))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <TranscriptReview interviewId="interview" />
    </QueryClientProvider>
  )
  const details = screen.getByText("Review saved answers")
    .parentElement as HTMLDetailsElement
  details.open = true
  fireEvent(details, new Event("toggle"))
  return client
}
it("records a review only after an explicit decision and preserves its identity after failure", async () => {
  vi.mocked(getSealHistory).mockResolvedValue({
    current_seal_id: "first",
    seals: [],
    incidents: [{ id: "incident", content: "Late content", review: null }],
  })
  vi.mocked(reviewCaptureIncident)
    .mockRejectedValueOnce(new Error("Network uncertain"))
    .mockResolvedValueOnce({})
  const client = mount()
  await screen.findByText("Late content")
  expect(reviewCaptureIncident).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText("Reviewer"), {
    target: { value: "Reviewer" },
  })
  fireEvent.change(screen.getByLabelText("Reason for review"), {
    target: { value: "Checked audio timestamps" },
  })
  fireEvent.click(screen.getByText("Save review"))
  await screen.findByRole("alert")
  fireEvent.click(screen.getByText("Save review"))
  await waitFor(() => expect(reviewCaptureIncident).toHaveBeenCalledTimes(2))
  expect(vi.mocked(reviewCaptureIncident).mock.calls[1]).toEqual(
    vi.mocked(reviewCaptureIncident).mock.calls[0]
  )
  expect(createReviewedSnapshot).not.toHaveBeenCalled()
  client.clear()
})
it("creates a reviewed version and explicit evaluation with stable identities on retry", async () => {
  vi.mocked(getSealHistory).mockResolvedValue({
    current_seal_id: "first",
    seals: [
      {
        id: "first",
        version: 1,
        integrity: "complete",
        invalidated: true,
        records: [],
        provenance: {},
        created_at: "today",
      },
    ],
    incidents: [
      {
        id: "incident",
        content: "Tail",
        review: {
          decision: "omission",
          rationale: "Confirmed",
          reviewer: "Reviewer",
        },
      },
    ],
  })
  vi.mocked(createReviewedSnapshot).mockResolvedValue({
    seal_id: "second",
    version: 2,
    evaluation_required: true,
  })
  vi.mocked(evaluateInterview)
    .mockRejectedValueOnce(new Error("Reply lost"))
    .mockResolvedValueOnce({} as never)
  const client = mount()
  await screen.findByText("Tail")
  expect(createReviewedSnapshot).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText("Reviewer for revised assessment"), {
    target: { value: "Reviewer" },
  })
  fireEvent.change(screen.getByLabelText("Reason for revised assessment"), {
    target: { value: "Matched the admitted recording" },
  })
  fireEvent.click(screen.getByText("Create revised assessment"))
  await screen.findByRole("alert")
  const body = vi.mocked(createReviewedSnapshot).mock.calls[0][1]
  expect(body.confirm_complete).toBe(false)
  fireEvent.click(screen.getByText("Retry revised assessment"))
  await waitFor(() => expect(evaluateInterview).toHaveBeenCalledTimes(2))
  expect(vi.mocked(createReviewedSnapshot).mock.calls[1][1]).toEqual(body)
  expect(vi.mocked(evaluateInterview).mock.calls[1]).toEqual(
    vi.mocked(evaluateInterview).mock.calls[0]
  )
  client.clear()
})
