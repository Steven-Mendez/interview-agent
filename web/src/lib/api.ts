// Typed fetch wrappers for the backend contract (see
// interview_agent/server/routes.py). Every endpoint lives under the `/api`
// prefix — the dev proxy (vite.config.ts) and the prod SPA fallback both
// key off that single prefix.

const API_BASE = "/api"

// ---- Shapes -----------------------------------------------------------------

/** Mirrors `interview_agent/interview/db.py` + the transitions in agent.py. */
export type InterviewStatus =
  | "created"
  | "planned"
  | "interviewing"
  | "completed"
  | "evaluated"
  | "evaluation_failed"
  | "error"

export interface Milestone {
  id: string
  position: number
  title: string
  description: string
  completed: boolean
  notes: string | null
}

export interface Evaluation {
  hired: boolean
  score: number
  strengths: string[]
  weaknesses: string[]
  rationale: string
  ended_by: string
}

export interface Interview {
  id: string
  status: InterviewStatus
  ended_reason: string | null
  plan: Record<string, unknown> | null
  milestones: Milestone[]
  evaluation: Evaluation | null
  token_usage: unknown
}

export interface TokenResponse {
  server_url: string
  room: string
  token: string
}

/** One curated voice, as served under `settings.voices[language]`. */
export interface VoiceOption {
  id: string
  label: string
  gender: string
}

export interface Settings {
  agent_name: string
  language: string
  voice: string
  persona: string | null
  custom_instructions: string | null
  /** Full catalog, keyed by language — rides along so one fetch renders the whole screen. */
  voices: Partial<Record<string, VoiceOption[]>>
}

export interface SettingsUpdate {
  agent_name: string
  language: string
  voice: string
  persona?: string | null
  custom_instructions?: string | null
}

// ---- Errors -------------------------------------------------------------------

/** Thrown for any non-2xx response; carries the `{detail}` body and, for 429s, `Retry-After`. */
export class ApiError extends Error {
  readonly status: number
  readonly retryAfter: number | null

  constructor(status: number, detail: string, retryAfter: number | null) {
    super(detail)
    this.name = "ApiError"
    this.status = status
    this.retryAfter = retryAfter
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`
  try {
    const body: unknown = await res.json()
    if (
      body &&
      typeof body === "object" &&
      "detail" in body &&
      typeof body.detail === "string"
    ) {
      detail = body.detail
    }
  } catch {
    // Non-JSON or empty body — keep the generic `HTTP {status}` message.
  }
  const retryAfterHeader = res.headers.get("Retry-After")
  const retryAfterNumber =
    retryAfterHeader === null ? NaN : Number(retryAfterHeader)
  const retryAfter = Number.isFinite(retryAfterNumber) ? retryAfterNumber : null
  return new ApiError(res.status, detail, retryAfter)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init)
  if (!res.ok) throw await toApiError(res)
  return (await res.json()) as T
}

// ---- Settings -----------------------------------------------------------------

export function getSettings(): Promise<Settings> {
  return request<Settings>("/settings")
}

export function updateSettings(body: SettingsUpdate): Promise<Settings> {
  return request<Settings>("/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

// ---- Interviews -----------------------------------------------------------------

/** `formData` must contain a `resume` (PDF) file field and a `job_offer` text field. */
export function createInterview(formData: FormData): Promise<Interview> {
  // No Content-Type header: the browser sets the multipart boundary itself.
  return request<Interview>("/interviews", { method: "POST", body: formData })
}

export function getInterview(interviewId: string): Promise<Interview> {
  return request<Interview>(`/interviews/${interviewId}`)
}

export function getInterviewToken(interviewId: string): Promise<TokenResponse> {
  return request<TokenResponse>(`/interviews/${interviewId}/token`)
}

/** Re-invocable: the response IS the evaluated interview (no need to re-poll after calling this). */
export function evaluateInterview(interviewId: string): Promise<Interview> {
  return request<Interview>(`/interviews/${interviewId}/evaluate`, {
    method: "POST",
  })
}
