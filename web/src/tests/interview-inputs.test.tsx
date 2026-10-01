/** @vitest-environment jsdom */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { UploadPage } from "../components/interview-setup"
import type * as Api from "@/lib/api"
import type * as Router from "@tanstack/react-router"

const mocks = vi.hoisted(() => ({
  preview: vi.fn(),
  settings: vi.fn(),
  create: vi.fn(),
  navigate: vi.fn(),
}))
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof Api>()),
  previewResume: mocks.preview,
  getSettings: mocks.settings,
  createInterview: mocks.create,
}))
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof Router>()),
  useNavigate: () => mocks.navigate,
}))

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

function renderWizard() {
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
}
const next = () =>
  fireEvent.click(screen.getByRole("button", { name: "Continue" }))

describe("reviewed interview inputs", () => {
  it("submits edited CV text bound to its preview and sends numeric limits", async () => {
    renderWizard()
    next()
    const text = await screen.findByLabelText("Extracted resume text")
    fireEvent.change(text, { target: { value: "Corrected CV text" } })
    next()
    fireEvent.change(await screen.findByLabelText("Main questions"), {
      target: { value: "2" },
    })
    fireEvent.change(screen.getByLabelText("Follow-ups per topic"), {
      target: { value: "0" },
    })
    fireEvent.change(screen.getByLabelText("Time limit (minutes)"), {
      target: { value: "7" },
    })
    next()
    fireEvent.click(
      await screen.findByRole("button", { name: "Start interview" })
    )
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1))
    const payload = mocks.create.mock.calls[0][0] as FormData
    expect(JSON.parse(payload.get("resume_review") as string)).toEqual({
      pdf_sha256: "a".repeat(64),
      text: "Corrected CV text",
    })
    expect(JSON.parse(payload.get("interviewer") as string)).toMatchObject({
      question_limit: 2,
      followup_limit: 0,
      max_minutes: 7,
    })
    expect(mocks.preview).toHaveBeenCalledTimes(1)
  })

  it("preserves an empty edit on back navigation and blocks continuing", async () => {
    renderWizard()
    next()
    fireEvent.change(await screen.findByLabelText("Extracted resume text"), {
      target: { value: "" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Back" }))
    await screen.findByLabelText("Job offer")
    next()
    const text = await screen.findByLabelText<HTMLTextAreaElement>(
      "Extracted resume text"
    )
    expect(text.value).toBe("")
    next()
    await screen.findByText("Review the extracted resume text.")
    expect(screen.queryByLabelText("Main questions")).toBeNull()
    expect(mocks.create).not.toHaveBeenCalled()
    expect(mocks.preview).toHaveBeenCalledTimes(1)
  })

  it("rejects out-of-range limits before opening the last step", async () => {
    renderWizard()
    next()
    await screen.findByLabelText("Extracted resume text")
    next()
    fireEvent.change(await screen.findByLabelText("Main questions"), {
      target: { value: "13" },
    })
    next()
    await screen.findByText("Use a whole number from 1 to 12, or leave blank.")
    expect(screen.queryByRole("button", { name: "Start interview" })).toBeNull()
    expect(mocks.create).not.toHaveBeenCalled()
  })
})
