import type { InterviewStatus } from "@/lib/api"

// 180 s past the anchor the evaluation is considered stuck; the endpoint is
// re-invocable, so the results panel then offers to (re)start it by hand —
// same policy as app.js's MAX_EVAL_POLLS (90 * 2s).
export const EVAL_TIMEOUT_MS = 180_000

/** The instant the evaluation-timeout clock counts from.
 *
 * While the evaluation runs (`evaluating`) the API bumps the row's
 * `updated_at` every 30 s as a heartbeat, so the latest one is the anchor:
 * a live run keeps pushing the deadline back and never times out, one whose
 * process died stops moving and does. Before the run is claimed (`completed`,
 * or `interviewing` when this tab saw the disconnect first) the anchor is
 * the disconnect itself — or, on a deep-link revisit with none in this tab,
 * the row's `updated_at`, which is when the worker marked it. Anchoring at
 * mount instead made an interview stuck for hours demand another three
 * minutes of waiting before offering the retry it needed all along.
 *
 * An unparseable timestamp falls back to now. */
export function evaluationAnchor(
  status: InterviewStatus,
  updatedAt: string,
  endedAt: number | null
): number {
  const updated = Date.parse(updatedAt) || Date.now()
  return status === "evaluating" ? updated : (endedAt ?? updated)
}
