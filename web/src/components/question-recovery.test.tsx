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
import { getQuestion, replayQuestion } from "@/lib/api"
import { QuestionRecovery } from "./question-recovery"

vi.mock("@/lib/api", () => ({ getQuestion: vi.fn(), replayQuestion: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it("requires an explicit click and preserves request identity after an uncertain failure", async () => {
  vi.mocked(getQuestion).mockResolvedValue({
    question: { id: "q1", text: "Saved question?", status: "started" },
  })
  vi.mocked(replayQuestion)
    .mockRejectedValueOnce(new Error("Network failed"))
    .mockResolvedValueOnce({ request_id: "accepted" })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <QuestionRecovery interviewId="interview" />
    </QueryClientProvider>
  )
  const button = await screen.findByText("Listen again")
  expect(replayQuestion).not.toHaveBeenCalled()
  fireEvent.click(button)
  await screen.findByRole("alert")
  const requestId = vi.mocked(replayQuestion).mock.calls[0][2]
  fireEvent.click(button)
  await waitFor(() => expect(replayQuestion).toHaveBeenCalledTimes(2))
  expect(vi.mocked(replayQuestion).mock.calls[1]).toEqual([
    "interview",
    "q1",
    requestId,
  ])
  client.clear()
})

it("does not offer replay when a confirmed answer has retired the question", async () => {
  vi.mocked(getQuestion).mockResolvedValue({ question: null })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <QuestionRecovery interviewId="interview" />
    </QueryClientProvider>
  )
  await waitFor(() => expect(getQuestion).toHaveBeenCalled())
  expect(screen.queryByText("Listen again")).toBeNull()
  client.clear()
})
