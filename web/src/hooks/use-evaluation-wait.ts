import * as React from "react"

import { useEvaluationRequest } from "@/hooks/use-evaluation-request"
import { EVAL_TIMEOUT_MS, evaluationAnchor } from "@/lib/evaluation"
import type { Interview } from "@/lib/api"

/** Waiting for a verdict after the interview ended: whether it is still
 *  coming, whether it is stuck or failed, and the (re)start action that
 *  fits where it got stuck. */
export function useEvaluationWait(
  interview: Interview,
  endedAt: number | null
) {
  const [timedOut, setTimedOut] = React.useState(false)

  const status = interview.status
  // No verdict yet: the run is going (evaluating), has not been claimed
  // (completed), or this tab saw the disconnect before the worker even
  // marked the row (still interviewing, phase 'ended').
  const waiting = status !== "evaluated" && status !== "evaluation_failed"

  // The timeout clock counts from the disconnect (or the row's updated_at on
  // a revisit) until the run is claimed, then from its latest heartbeat —
  // every poll that brings a newer updated_at moves the anchor and, through
  // the effect below, pushes the deadline back. See evaluationAnchor.
  const anchor = evaluationAnchor(status, interview.updated_at, endedAt)

  React.useEffect(() => {
    if (!waiting) return
    const remaining = anchor + EVAL_TIMEOUT_MS - Date.now()
    if (remaining <= 0) {
      setTimedOut(true)
      return
    }
    // An anchor that moved forward (a heartbeat, or the run being claimed
    // after the alert went up) means the evaluation is alive after all.
    setTimedOut(false)
    const timer = setTimeout(() => setTimedOut(true), remaining)
    return () => clearTimeout(timer)
  }, [waiting, status, anchor])

  const retry = useEvaluationRequest(interview.id)
  const failed = status === "evaluation_failed"
  const showRetry = failed || timedOut
  // What the button does depends on where the run got stuck: it never
  // started (completed / interviewing), it died on the way (evaluating), or
  // it ended in an error (evaluation_failed).
  const action = failed
    ? "Retry"
    : status === "evaluating"
      ? "Restart"
      : "Start"
  const problem = failed
    ? "The evaluation failed. You can retry it."
    : status === "evaluating"
      ? "The evaluation is taking longer than expected. You can restart it."
      : "The evaluation did not start. You can start it now."

  return { failed, showRetry, action, problem, retry }
}
