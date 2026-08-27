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

/** Depth axis: what gets asked and what counts as a sufficient answer. */
export type Seniority = "trainee" | "junior" | "mid" | "senior" | "lead"
/** Volume axis: milestone count and minutes. Independent of the level. */
export type InterviewLength = "short" | "standard" | "deep"
/** How the pinned level was arrived at. */
export type SenioritySource = "explicit" | "detected" | "fallback"

export const SENIORITY_LABELS: Record<Seniority, string> = {
  trainee: "Trainee / intern",
  junior: "Junior (0-2 yrs)",
  mid: "Mid-level (2-5 yrs)",
  senior: "Senior (5+ yrs)",
  lead: "Lead / staff",
}

export const LENGTH_LABELS: Record<InterviewLength, string> = {
  short: "Short — ~8 min",
  standard: "Standard — ~15 min",
  deep: "Deep — ~25 min",
}

export const LANGUAGE_LABELS: Partial<Record<string, string>> = {
  en: "English",
  es: "Español",
}

/** en/es labels for the two curated genders. */
export const GENDER_LABELS: Partial<
  Record<string, Partial<Record<string, string>>>
> = {
  en: { female: "female", male: "male" },
  es: { female: "femenina", male: "masculina" },
}

/** Milestone count per length, shown next to the label where there is room. */
export const LENGTH_TOPICS: Record<InterviewLength, string> = {
  short: "3-4 topics",
  standard: "4-6 topics",
  deep: "6-8 topics",
}

export interface Milestone {
  id: string
  position: number
  title: string
  description: string
  /** The bar set for this milestone at the pinned level. Null on legacy rows. */
  expected_evidence: string | null
  completed: boolean
  notes: string | null
}

export interface Evaluation {
  hired: boolean
  score: number
  strengths: string[]
  weaknesses: string[]
  rationale: string
  /** The level the evaluator judged against. */
  seniority_evaluated: Seniority | null
  /** Expectations discarded for sitting above that level. */
  calibration_notes: string[]
  ended_by: string
}

export interface Interview {
  id: string
  /** ISO-8601, UTC. */
  created_at: string
  updated_at: string
  status: InterviewStatus
  ended_reason: string | null
  /** First line of the job offer — the closest thing to a role title. */
  title: string
  job_offer: string
  resume_filename: string | null
  /** Root of the re-run chain; null on a first attempt. */
  repeat_of_id: string | null
  plan: Record<string, unknown> | null
  seniority: Seniority
  seniority_source: SenioritySource
  /** Why the planner classified it this way; null when the user picked it. */
  seniority_evidence: string | null
  interview_length: InterviewLength
  max_minutes: number | null
  /** Who conducted it, snapshotted at creation. Null on legacy rows. */
  interviewer: {
    agent_name: string | null
    language: string | null
    voice: string | null
  } | null
  milestones: Milestone[]
  evaluation: Evaluation | null
  token_usage: unknown
}

/** One row of GET /interviews: the detail minus the plan, resume and prose. */
export interface InterviewSummary {
  id: string
  created_at: string
  updated_at: string
  status: InterviewStatus
  ended_reason: string | null
  title: string
  resume_filename: string | null
  seniority: Seniority
  seniority_source: SenioritySource
  interview_length: InterviewLength
  max_minutes: number | null
  repeat_of_id: string | null
  milestones_total: number
  milestones_completed: number
  evaluation: { hired: boolean; score: number } | null
}

export interface InterviewPage {
  items: InterviewSummary[]
  total: number
  limit: number
  offset: number
}

export interface TranscriptMessage {
  role: "user" | "assistant"
  content: string
  created_at: string
}

/** Both fields inherit from the source when omitted; `seniority: "auto"` asks
 *  the planner to classify the role again instead of inheriting. */
export interface RepeatRequest {
  seniority?: string
  interview_length?: string
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

export function voiceLabel(language: string, voice: VoiceOption): string {
  const gender = GENDER_LABELS[language]?.[voice.gender] ?? voice.gender
  return `${voice.label} (${gender})`
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

/** The `interviewer` field of POST /interviews, JSON-encoded.
 *
 * A key left out inherits the global Settings value; a key set to "" runs
 * this interview without one. Multipart cannot carry that difference in
 * sibling fields — an empty one is indistinguishable from an absent one —
 * which is why the whole object travels as JSON. */
export interface InterviewerInput {
  agent_name?: string
  language?: string
  voice?: string
  persona?: string
  custom_instructions?: string
}

/** `formData` must contain a `resume` (PDF) file field and a `job_offer` text field. */
export function createInterview(formData: FormData): Promise<Interview> {
  // No Content-Type header: the browser sets the multipart boundary itself.
  return request<Interview>("/interviews", { method: "POST", body: formData })
}

export function listInterviews(params?: {
  limit?: number
  offset?: number
  status?: InterviewStatus
}): Promise<InterviewPage> {
  const query = new URLSearchParams()
  if (params?.limit !== undefined) query.set("limit", String(params.limit))
  if (params?.offset !== undefined) query.set("offset", String(params.offset))
  if (params?.status) query.set("status", params.status)
  const qs = query.toString()
  return request<InterviewPage>(`/interviews${qs ? `?${qs}` : ""}`)
}

export function getTranscript(
  interviewId: string
): Promise<{ messages: TranscriptMessage[] }> {
  return request<{ messages: TranscriptMessage[] }>(
    `/interviews/${interviewId}/transcript`
  )
}

/** Plans a NEW interview off the stored resume and offer — the source row is
 *  never touched. The response is the new (planned) interview. */
export function repeatInterview(
  interviewId: string,
  body: RepeatRequest = {}
): Promise<Interview> {
  return request<Interview>(`/interviews/${interviewId}/repeat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
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
