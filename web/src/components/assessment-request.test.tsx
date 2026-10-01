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
import { evaluateInterview } from "@/lib/api"
import type { Interview } from "@/lib/api"
import { interviewQueryOptions } from "@/lib/queries"
import { AssessmentRequest } from "./assessment-request"

vi.mock("@/lib/api", async (original) => ({
  ...(await original<object>()),
  evaluateInterview: vi.fn(),
}))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})
function mount(interview: Interview) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <AssessmentRequest interview={interview} />
    </QueryClientProvider>
  )
  return client
}
it("keeps previous feedback identifiable during a new request and after failure", () => {
  const interview = {
    id: "interview",
    status: "evaluating",
    evaluation_is_previous: true,
  } as Interview
  const client = mount(interview)
  expect(screen.getByRole("status").textContent).toContain(
    "earlier saved assessment"
  )
  expect(screen.getByRole("status").textContent).toContain("in progress")
  expect(evaluateInterview).not.toHaveBeenCalled()
  client.clear()
})
it("preserves a request ID across an uncertain reply and starts a fresh intent only after acceptance", async () => {
  const interview = {
    id: "interview",
    status: "evaluation_failed",
    evaluation_is_previous: true,
  } as Interview
  vi.mocked(evaluateInterview)
    .mockRejectedValueOnce(new Error("Reply lost"))
    .mockResolvedValue({ ...interview, status: "evaluating" })
  const client = mount(interview)
  expect(screen.getByRole("status").textContent).toContain(
    "earlier feedback remains available"
  )
  fireEvent.click(screen.getByText("Retry assessment"))
  await screen.findByRole("alert")
  fireEvent.click(screen.getByText("Retry assessment request"))
  await waitFor(() => expect(evaluateInterview).toHaveBeenCalledTimes(2))
  expect(vi.mocked(evaluateInterview).mock.calls[1]).toEqual(
    vi.mocked(evaluateInterview).mock.calls[0]
  )
  await waitFor(() =>
    expect(
      client.getQueryData<Interview>(
        interviewQueryOptions("interview").queryKey
      )?.status
    ).toBe("evaluating")
  )
  await screen.findByText("Retry assessment")
  fireEvent.click(screen.getByText("Retry assessment"))
  await waitFor(() => expect(evaluateInterview).toHaveBeenCalledTimes(3))
  expect(vi.mocked(evaluateInterview).mock.calls[2][1]).not.toBe(
    vi.mocked(evaluateInterview).mock.calls[0][1]
  )
  client.clear()
})
