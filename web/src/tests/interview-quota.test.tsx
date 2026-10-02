/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { UploadPage } from "../components/interview-setup"
import type * as Auth from "@/lib/auth"
import type * as Api from "@/lib/api"
import type { Me } from "@/lib/api"
import type * as Router from "@tanstack/react-router"

const mocks = vi.hoisted(() => ({
  preview: vi.fn(),
  settings: vi.fn(),
  create: vi.fn(),
  me: vi.fn(),
}))
// GET /me goes out as it would without sign-in (mode none).
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof Auth>()),
  useCanCallApi: () => true,
}))
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof Api>()),
  previewResume: mocks.preview,
  getSettings: mocks.settings,
  createInterview: mocks.create,
  getMe: mocks.me,
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useNavigate: () => vi.fn(),
}))

const GUEST: Me = {
  id: "guest",
  email: "guest@example.com",
  name: "Guest",
  is_admin: false,
  interviews_used: 1,
  interview_limit: 3,
  interviews_remaining: 2,
  demo_capacity_available: true,
  auth_provider: "neon",
  created_at: "2026-09-01T10:00:00+00:00",
  last_seen_at: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.settings.mockResolvedValue({
    agent_name: "Laura",
    language: "en",
    voice: "en_female",
    persona: null,
    custom_instructions: null,
    voices: { en: [{ id: "en_female", label: "Laura", gender: "female" }] },
  })
  mocks.preview.mockResolvedValue({
    filename: "cv.pdf",
    text: "Extracted CV",
    characters: 12,
    pdf_sha256: "a".repeat(64),
  })
  mocks.create.mockImplementation(() => new Promise(() => {}))
})
afterEach(cleanup)

/** Through every step to the final submit. */
async function openLastStep(me: Me) {
  mocks.me.mockResolvedValue(me)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <UploadPage />
    </QueryClientProvider>
  )
  fireEvent.change(screen.getByLabelText("Resume (PDF)"), {
    target: {
      files: [new File(["PDF"], "cv.pdf", { type: "application/pdf" })],
    },
  })
  fireEvent.change(screen.getByLabelText("Job offer"), {
    target: { value: "Backend engineer" },
  })
  const next = () =>
    fireEvent.click(screen.getByRole("button", { name: "Continue" }))
  next()
  await screen.findByLabelText("Extracted resume text")
  next()
  await screen.findByLabelText("Main questions")
  next()
  return screen.findByRole<HTMLButtonElement>("button", {
    name: "Prepare interview",
  })
}

describe("interview quota on the setup page", () => {
  it("tells a guest how many interviews are left", async () => {
    const submit = await openLastStep(GUEST)
    await screen.findByText("You have 2 of your 3 interviews left")
    expect(submit.disabled).toBe(false)
  })

  it("closes the submit for a guest who used every interview", async () => {
    const submit = await openLastStep({
      ...GUEST,
      interviews_used: 3,
      interviews_remaining: 0,
    })
    await screen.findByText("You've used your 3 free interviews.")
    expect(submit.disabled).toBe(true)
    fireEvent.click(submit)
    expect(mocks.create).not.toHaveBeenCalled()
  })

  it("closes the submit while the demo is out of interviews this month", async () => {
    const submit = await openLastStep({
      ...GUEST,
      demo_capacity_available: false,
    })
    await screen.findByText(
      /^The demo has reached its interview limit for this month\. Try again on /
    )
    expect(submit.disabled).toBe(true)
  })

  it("never limits an admin", async () => {
    const submit = await openLastStep({
      ...GUEST,
      is_admin: true,
      interviews_used: 12,
      interview_limit: null,
      interviews_remaining: null,
    })
    await screen.findByText("Unlimited interviews")
    expect(submit.disabled).toBe(false)
  })
})
