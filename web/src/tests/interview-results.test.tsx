/** @vitest-environment jsdom */
import * as React from "react"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, expect, it, vi } from "vitest"
import type * as Router from "@tanstack/react-router"
import type * as Api from "@/lib/api"
import { getInterview, getEvaluationHistory, getSealHistory } from "@/lib/api"
import type { Interview } from "@/lib/api"
import { interviewQueryOptions } from "@/lib/queries"
import { Route } from "@/routes/interviews.$interviewId"

vi.mock("@/hooks/use-interview-session", () => ({
  useInterviewSession: () => ({
    phase: "idle",
    endedAt: null,
    room: null,
    syncClosingState: vi.fn(),
  }),
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useNavigate: () => vi.fn(),
  Link: React.forwardRef<
    HTMLAnchorElement,
    React.ComponentPropsWithoutRef<"a"> & { to: string }
  >(({ to, ...props }, ref) => <a href={to} {...props} ref={ref} />),
}))
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof Api>()),
  getInterview: vi.fn(),
  getEvaluationHistory: vi.fn(),
  getSealHistory: vi.fn(),
  getMe: vi.fn(() =>
    Promise.resolve({
      id: "user",
      email: null,
      name: null,
      is_admin: false,
      interviews_used: 1,
      interview_limit: 3,
      interviews_remaining: 2,
      demo_capacity_available: true,
    })
  ),
}))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.resetAllMocks()
})
const saved: Interview = {
  id: "interview",
  status: "evaluating",
  created_at: "2026-09-30T00:00:00Z",
  updated_at: new Date().toISOString(),
  ended_reason: "plan_complete",
  can_start: false,
  reconnect_until: null,
  title: "Engineer",
  job_offer: "Engineer",
  resume_filename: null,
  repeat_of_id: null,
  plan: {},
  seniority: "junior",
  seniority_source: "explicit",
  seniority_evidence: null,
  interview_length: "short",
  max_minutes: 8,
  closing_id: "closing",
  farewell_status: "played",
  transcript_sealed: true,
  run_config: {},
  question_limit: 2,
  followup_limit: 1,
  transcript_integrity: "complete",
  interviewer: null,
  milestones: [],
  token_usage: null,
  evaluation_is_previous: true,
  evaluation: {
    score: 82,
    hired: true,
    strengths: ["Earlier specific feedback"],
    weaknesses: [],
    rationale: "Earlier conclusion",
    seniority_evaluated: "junior",
    calibration_notes: [],
    ended_by: "plan_complete",
    evaluation_status: "complete",
  },
}
async function mount(interview: Interview) {
  vi.spyOn(Route, "useParams").mockReturnValue({ interviewId: interview.id })
  vi.mocked(getInterview).mockResolvedValue(interview)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  client.setQueryData(interviewQueryOptions(interview.id).queryKey, interview)
  const Page = Route.options.component as React.ComponentType
  await act(async () => {
    render(
      <QueryClientProvider client={client}>
        <React.Suspense fallback={<p>Loading interview…</p>}>
          <Page />
        </React.Suspense>
      </QueryClientProvider>
    )
  })
  return client
}
it.each(["evaluating", "evaluation_failed"] as const)(
  "keeps the previous result available while the replacement is %s",
  async (status) => {
    const client = await mount({ ...saved, status })
    expect(await screen.findByText("Earlier specific feedback")).toBeTruthy()
    expect(screen.getByText("Earlier conclusion")).toBeTruthy()
    expect(screen.getByText("82")).toBeTruthy()
    expect(screen.getByRole("status").textContent).toContain(
      "earlier saved assessment"
    )
    expect(screen.getByText("Evaluation history")).toBeTruthy()
    client.clear()
  }
)
it("offers saved versions and failed-attempt history even if no assessment ever succeeded", async () => {
  vi.mocked(getEvaluationHistory).mockResolvedValue({
    current_request_id: "request",
    requests: [
      {
        id: "request",
        automatic: true,
        status: "failed",
        attempts: 1,
        created_at: "today",
        seal_version: 1,
      },
    ],
    attempts: [
      {
        id: "failed-run",
        request_id: "request",
        ordinal: 1,
        status: "failed",
        result: null,
        error: "private failure details",
        created_at: "today",
      },
    ],
  })
  vi.mocked(getSealHistory).mockResolvedValue({
    current_seal_id: null,
    seals: [],
    incidents: [],
  })
  const client = await mount({
    ...saved,
    status: "evaluation_failed",
    evaluation: null,
  })
  const details = (await screen.findByText("Evaluation history"))
    .parentElement as HTMLDetailsElement
  details.open = true
  fireEvent(details, new Event("toggle"))
  await screen.findByText("This attempt could not produce a valid assessment.")
  expect(screen.queryByText("private failure details")).toBeNull()
  expect(screen.getByText("Review saved answers")).toBeTruthy()
  client.clear()
})
