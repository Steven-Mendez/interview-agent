// Typed fetch wrappers for the backend contract (see
// interview_agent/server/routes.py), all under API_BASE (lib/api-base).

import { API_BASE } from "@/lib/api-base"
import { watchApiFetch } from "@/lib/api-waking"
import {
  forgetRefusedToken,
  getAccessToken,
  refreshAccessToken,
  resetAuthMode,
} from "@/lib/auth"
import { redirectsSuppressed } from "@/lib/redirect-suppression"

// ---- Shapes -----------------------------------------------------------------

/** Mirrors `interview_agent/interview/db.py` + the transitions in agent.py.
 *
 * created → planned → interviewing → completed → evaluating → evaluated |
 * evaluation_failed; `error` is a plan that failed. `completed` is the gap
 * between the interview ending and the evaluation being claimed — seconds,
 * normally; it sticks only when the worker's trigger never reached the API.
 * `evaluating` is a run in progress in the API process, which bumps the
 * row's `updated_at` every 30 s as a heartbeat while it goes. */
export type InterviewStatus =
  | "created"
  | "planned"
  | "interviewing"
  | "closing"
  | "completed"
  | "evaluating"
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
  /** The bar set for this milestone at the pinned level. */
  expected_evidence: string | null
  completed: boolean
  notes: string | null
  lifecycle?: "pending" | "active" | "closed" | "skipped"
  close_reason?: string | null
  essential?: boolean
  competency?: string | null
  primary_questions?: number
  followups?: number
  clarifications?: number
}

export type Assessment =
  "exceeds" | "meets" | "partial" | "below" | "not_assessable"
export type EvaluationStatus = "complete" | "partial" | "insufficient"
export interface EvidenceRef {
  message_id: string
  message_version?: number | null
  quote: string
}
export interface CriterionEvaluation {
  milestone_id: string
  assessment: Assessment
  evidence: EvidenceRef[]
  rationale: string
  practice: string
}

export interface Evaluation {
  hired: boolean | null
  score: number | null
  strengths: string[]
  weaknesses: string[]
  rationale: string
  /** What kept the score from the top of its band; empty at a band's top. */
  score_gap?: string
  /** The level the evaluator judged against. */
  seniority_evaluated: Seniority | null
  /** Expectations discarded for sitting above that level. */
  calibration_notes: string[]
  ended_by: string
  /** Complete, partial or insufficient evidence for a verdict. */
  evaluation_status: EvaluationStatus
  coverage?: number
  criteria?: CriterionEvaluation[]
  findings?: {
    kind: "strength" | "weakness"
    text: string
    milestone_id: string
    evidence: EvidenceRef[]
  }[]
}

export interface Interview {
  id: string
  /** Live elapsed wall time from the persisted start, measured by PostgreSQL.
   * Unknown before the first start or when talking to an older API. */
  elapsed_seconds?: number | null
  /** ISO-8601, UTC. */
  created_at: string
  updated_at: string
  status: InterviewStatus
  ended_reason: string | null
  /** Whether Start (or a rejoin) can be attempted: true for `planned`, and
   *  for `interviewing` rows still inside their reconnect window. False for
   *  an `interviewing` row past it — its worker died and the server has not
   *  sealed it yet, so it can only be evaluated as recorded, or repeated. */
  can_start: boolean
  /** ISO-8601, UTC: until when the room waits for a rejoin. Non-null only on
   *  `interviewing` rows. */
  reconnect_until: string | null
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
  closing_id: string | null
  farewell_status: string | null
  /** Localized written farewell when the audio was not (provably) heard. */
  farewell_text?: string | null
  /** Server-clock bound for a closing observed after a reload; else null. */
  closing_remaining_seconds?: number | null
  transcript_sealed?: boolean
  run_config: Record<string, unknown> | null
  question_limit: number | null
  followup_limit: number | null
  transcript_integrity?: "complete" | "partial" | "failed" | null
  capture_integrity_pending?: boolean
  evaluation_invalidated?: boolean
  /** Who conducted it, snapshotted at creation. */
  interviewer: {
    agent_name: string | null
    language: string | null
    voice: string | null
  } | null
  milestones: Milestone[]
  evaluation: Evaluation | null
  evaluation_request_id?: string | null
  evaluation_is_previous?: boolean
  token_usage: unknown
}

/** One row of GET /interviews: the detail minus the plan, resume and prose. */
export interface InterviewSummary {
  id: string
  created_at: string
  updated_at: string
  status: InterviewStatus
  ended_reason: string | null
  /** Same meaning as on `Interview`: false on an `interviewing` row means
   *  it was interrupted for good. */
  can_start: boolean
  /** Same as on `Interview`: non-null only while `interviewing`. */
  reconnect_until: string | null
  title: string
  resume_filename: string | null
  seniority: Seniority
  seniority_source: SenioritySource
  interview_length: InterviewLength
  max_minutes: number | null
  repeat_of_id: string | null
  milestones_total: number
  milestones_completed: number
  evaluation: {
    hired: boolean | null
    score: number | null
    evaluation_status?: EvaluationStatus
  } | null
}

export interface InterviewPage {
  items: InterviewSummary[]
  total: number
  limit: number
  offset: number
}

export interface TranscriptMessage {
  id?: string
  version?: number | null
  versions?: { version: number; content: string }[]
  metrics?: { stt_confirmed?: unknown; stt_segmentation?: string } | null
  role: "user" | "assistant"
  content: string
  created_at: string
}

export interface TranscriptResponse {
  messages: TranscriptMessage[]
  capture_integrity_pending?: boolean
  incidents?: {
    id: string
    turn_id: string
    kind: string
    content: string | null
    created_at: string
    resolved_at: string | null
  }[]
}

/** Level and length inherit. Limits inherit for the same length, or are
 *  recalculated for a changed profile before applying explicit overrides. */
export interface RepeatRequest {
  seniority?: string
  interview_length?: string
  question_limit?: number
  followup_limit?: number
  max_minutes?: number
}

/** The stored cap is authoritative, including overridden durations. */
export function durationLabel(
  length: InterviewLength,
  maxMinutes: number | null
): string {
  return maxMinutes === null
    ? LENGTH_LABELS[length]
    : `${length[0].toUpperCase()}${length.slice(1)} — up to ${maxMinutes} min`
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

/** What a 401 on a signed-in request does — the router sends the user to
 *  sign in. Until one is registered, nothing: the error still throws. */
let unauthorizedHandler: () => void = () => {}

export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler
}

export { suppressUnauthorizedRedirect } from "@/lib/redirect-suppression"
export { useApiWaking, wakeApi } from "@/lib/api-waking"

/** The only fetch. Adds the user's JWT unless the caller brought its own
 *  credential (the closing routes carry the LiveKit participant token). */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const ownCredential = new Headers(init?.headers).has("Authorization")
  const send = (token: string | null) => {
    const headers = new Headers(init?.headers)
    if (token) headers.set("Authorization", `Bearer ${token}`)
    return watchApiFetch(fetch(`${API_BASE}${path}`, { ...init, headers }))
  }
  let token = ownCredential ? null : await getAccessToken()
  let res = await send(token)
  if (res.status === 401 && token) {
    // The JWT may only be stale (renewed elsewhere, minted by a clock ahead
    // of the API's): once more, with one fresh from the auth server. A
    // local account has none to offer.
    const fresh = await refreshAccessToken()
    if (fresh) {
      token = fresh
      res = await send(fresh)
    }
  }
  if (!res.ok) {
    // Our session was refused (expired, revoked, never there): sign in
    // again. A refused participant token is the room's business, not the
    // account's; a 503 is the API's, and the session stays. The API may
    // also have changed how it signs in (restarted with other settings):
    // the sign-in page asks it again.
    if (res.status === 401 && !ownCredential && !redirectsSuppressed()) {
      if (token) forgetRefusedToken(token)
      resetAuthMode()
      unauthorizedHandler()
    }
    throw await toApiError(res)
  }
  return (await res.json()) as T
}

/** Why the browser could not play the farewell: a DOMException name, a
 *  media element error code, a broken byte stream, or "other". Never a
 *  message. */
export type PlaybackErrorKind =
  | "NotAllowedError"
  | "NotSupportedError"
  | "AbortError"
  | "NotFoundError"
  | "EncodingError"
  | "InvalidStateError"
  | "SecurityError"
  | "MEDIA_ERR_ABORTED"
  | "MEDIA_ERR_NETWORK"
  | "MEDIA_ERR_DECODE"
  | "MEDIA_ERR_SRC_NOT_SUPPORTED"
  | "stream"
  | "other"

export interface PlaybackAcknowledgement {
  closing_id: string
  stream_id: string
  attempt_id: string
  status: "played" | "failed" | "timeout"
  duration_seconds: number | null
  /** The pre-join choice, or the browser default; never a guessed device. */
  audio_output?: "selected" | "default"
  /** Only with `failed`. */
  error_kind?: PlaybackErrorKind
}

export interface ClosingState {
  status: InterviewStatus
  closing_id: string | null
  farewell_status: string | null
  transcript_sealed: boolean
  transcript_integrity: string | null
  remaining_seconds: number | null
  playback_exceeded_budget: boolean | null
}

export function acknowledgeFarewell(
  interviewId: string,
  participantToken: string,
  body: PlaybackAcknowledgement,
  signal?: AbortSignal
): Promise<{
  accepted: boolean
  status?: string | null
  provisional?: boolean
  reason?: string
}> {
  return request(`/interviews/${interviewId}/closing/ack`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${participantToken}`,
    },
    body: JSON.stringify(body),
    signal,
  })
}

export function recordResponseOnset(
  interviewId: string,
  participantToken: string,
  body: { sample_id: string; seconds: number }
): Promise<{ accepted: boolean }> {
  return request(`/interviews/${interviewId}/metrics/response-onset`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${participantToken}`,
    },
    body: JSON.stringify(body),
  })
}

export function getClosingState(
  interviewId: string,
  participantToken: string,
  signal?: AbortSignal
): Promise<ClosingState> {
  return request(`/interviews/${interviewId}/closing`, {
    headers: { Authorization: `Bearer ${participantToken}` },
    signal,
  })
}

// ---- Account --------------------------------------------------------------------

/** GET /me: who is signed in and what is left of their interviews. Admins
 *  have no limit (null) and the monthly demo capacity never holds them back. */
export interface Me {
  id: string
  email: string | null
  name: string | null
  is_admin: boolean
  interviews_used: number
  interview_limit: number | null
  interviews_remaining: number | null
  demo_capacity_available: boolean
  /** How the account signs in: Neon Auth, or a local (development) one. */
  auth_provider: SignInMethod
  /** ISO-8601, UTC: when the API first saw the account. */
  created_at: string
  /** ISO-8601, UTC: this visit, which /me records before answering (null
   *  only for an account the admin list shows but that was never seen). */
  last_seen_at: string | null
}

export type SignInMethod = "neon" | "local"

export function getMe(): Promise<Me> {
  return request<Me>("/me")
}

/** One account of GET /admin/users: its profile, its quota, and how many
 *  of its interviews are still in the history. */
export interface AdminUser {
  id: string
  name: string | null
  email: string | null
  auth_provider: SignInMethod
  is_admin: boolean
  created_at: string
  last_seen_at: string | null
  interviews_used: number
  /** Null for admins: no limit. */
  interview_limit: number | null
  interviews_stored: number
  last_interview_at: string | null
}

export interface AdminUserPage {
  total: number
  items: AdminUser[]
}

/** Everyone who has signed in, most recently seen first. Admins only: a
 *  403 for anyone else. */
export function getAdminUsers(params?: {
  limit?: number
  offset?: number
}): Promise<AdminUserPage> {
  const query = new URLSearchParams()
  if (params?.limit !== undefined) query.set("limit", String(params.limit))
  if (params?.offset !== undefined) query.set("offset", String(params.offset))
  const qs = query.toString()
  return request<AdminUserPage>(`/admin/users${qs ? `?${qs}` : ""}`)
}

/** The two 429 details of POST /interviews and POST .../repeat. */
export const LIFETIME_LIMIT_REACHED = "lifetime_interview_limit_reached"
export const MONTHLY_CAPACITY_REACHED = "monthly_demo_capacity_reached"

const DEFAULT_INTERVIEW_LIMIT = 3

// The capacity resets at 00:00 UTC on the 1st: that is the day to name.
const retryDateFormat = new Intl.DateTimeFormat("en", {
  month: "long",
  day: "numeric",
  timeZone: "UTC",
})

export function lifetimeLimitMessage(limit?: number | null): string {
  return `You've used your ${limit ?? DEFAULT_INTERVIEW_LIMIT} free interviews.`
}

const HALF_DAY_MS = 12 * 60 * 60 * 1000

/** `retryAfter`: seconds until the capacity resets (the 429's Retry-After);
 *  without it, the 1st of next month (UTC), when the server resets it.
 *
 * Retry-After counts to exactly 00:00 on the 1st by the database's clock,
 * so added to this one it can land a few seconds either side of midnight:
 * the reset is the next 1st after half a day before that instant. */
export function monthlyCapacityMessage(retryAfter?: number | null): string {
  const now = Date.now()
  const around =
    retryAfter === null || retryAfter === undefined
      ? new Date(now)
      : new Date(now + retryAfter * 1000 - HALF_DAY_MS)
  const reset = new Date(
    Date.UTC(around.getUTCFullYear(), around.getUTCMonth() + 1, 1)
  )
  return `The demo has reached its interview limit for this month. Try again on ${retryDateFormat.format(reset)}.`
}

/** A quota refusal in words, or null for any other error. */
export function quotaErrorMessage(
  error: unknown,
  limit?: number | null
): string | null {
  if (!(error instanceof ApiError) || error.status !== 429) return null
  if (error.message === LIFETIME_LIMIT_REACHED) {
    return lifetimeLimitMessage(limit)
  }
  if (error.message === MONTHLY_CAPACITY_REACHED) {
    return monthlyCapacityMessage(error.retryAfter)
  }
  return null
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
  question_limit?: number
  followup_limit?: number
  max_minutes?: number
}

export interface ResumePreview {
  filename: string | null
  text: string
  characters: number
  pdf_sha256: string
}

/** No planner call or stored interview; bind subsequent edits to this PDF. */
export function previewResume(
  file: File,
  signal?: AbortSignal
): Promise<ResumePreview> {
  const body = new FormData()
  body.append("resume", file)
  return request<ResumePreview>("/resumes/preview", {
    method: "POST",
    body,
    signal,
  })
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
): Promise<TranscriptResponse> {
  return request<TranscriptResponse>(`/interviews/${interviewId}/transcript`)
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

/** Starts (or restarts) the evaluation in the background: 202 with the row
 *  in `evaluating` — or the current row untouched when a run is already
 *  going, so calling it twice never starts two. The verdict is NOT in the
 *  response: keep polling GET /interviews/{id} until the status is
 *  `evaluated` or `evaluation_failed`.
 *
 *  Re-invocable on any ended row with a transcript, which includes a run
 *  whose process died (`evaluating` with an `updated_at` older than 2 min)
 *  and an `interviewing` row past its reconnect window — that one is marked
 *  `completed` with ended_reason "connection_lost" and evaluated as recorded.
 *  409 for a live interview still inside the window and for a row with no
 *  transcript; 404 for an unknown id. */
export function evaluateInterview(
  interviewId: string,
  requestId?: string
): Promise<Interview> {
  const suffix = requestId ? `?request_id=${encodeURIComponent(requestId)}` : ""
  return request<Interview>(`/interviews/${interviewId}/evaluate${suffix}`, {
    method: "POST",
  })
}

export interface RecoverableQuestion {
  id: string
  text: string
  status: "pending" | "requested" | "started" | "sdk_completed" | "interrupted"
}

export function getQuestion(interviewId: string) {
  return request<{ question: RecoverableQuestion | null }>(
    `/interviews/${interviewId}/question`
  )
}

export function replayQuestion(
  interviewId: string,
  questionId: string,
  requestId: string
) {
  return request<{ request_id: string }>(
    `/interviews/${interviewId}/question/replay`,
    {
      method: "POST",
      body: JSON.stringify({ question_id: questionId, request_id: requestId }),
    }
  )
}

export function getEvaluationHistory(interviewId: string) {
  return request<{
    current_request_id: string | null
    requests: {
      id: string
      automatic: boolean
      seal_id?: string | null
      seal_version?: number | null
      status: string
      attempts: number
      created_at: string
    }[]
    attempts: {
      id: string
      request_id: string | null
      ordinal: number | null
      status: string
      invalidated?: boolean
      result: Evaluation | null
      error: string | null
      created_at: string
    }[]
  }>(`/interviews/${interviewId}/evaluations`)
}

export type IncidentDecision = "duplicate" | "post_cut" | "omission"
export interface SealHistory {
  current_seal_id: string | null
  seals: {
    id: string
    version: number
    integrity: string
    invalidated: boolean
    records: { id: string; role: string; content: string; version?: number }[]
    provenance: { incorporated_incident_ids?: string[] }
    created_at: string
  }[]
  incidents: {
    id: string
    content: string | null
    review: {
      decision: IncidentDecision
      reviewer: string
      rationale: string
    } | null
  }[]
}
export function getSealHistory(interviewId: string) {
  return request<SealHistory>(`/interviews/${interviewId}/seals`)
}
export function reviewCaptureIncident(
  interviewId: string,
  incidentId: string,
  body: {
    review_id: string
    decision: IncidentDecision
    rationale: string
    reviewer: string
  }
) {
  return request(`/interviews/${interviewId}/incidents/${incidentId}/review`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}
export function createReviewedSnapshot(
  interviewId: string,
  body: {
    seal_id: string
    parent_id: string
    incident_ids: string[]
    rationale: string
    reviewer: string
    confirm_complete: boolean
  }
) {
  return request<{
    seal_id: string
    version: number
    evaluation_required: boolean
  }>(`/interviews/${interviewId}/seals`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}
