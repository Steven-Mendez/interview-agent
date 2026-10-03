import { InfoIcon } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import type { Interview } from "@/lib/api"

// Endings that the server or a technical limit decided. A normal close
// (plan covered, question budget, candidate asked) needs no explanation.
const TECHNICAL_REASONS: Partial<Record<string, string>> = {
  worker_lost:
    "The connection to the interviewer was lost and could not be recovered in time. What was recorded up to that point was saved as a partial transcript.",
  abandoned:
    "The session stayed disconnected or silent for too long, so it was closed. What was recorded was saved as a partial transcript.",
  connection_lost:
    "The connection was lost before the interview finished. What was recorded was saved.",
  idle_timeout:
    "Nobody spoke for the configured idle period, so the interview was closed.",
  timeout: "The interview reached its time limit.",
}

const FAREWELL_NOTES: Partial<Record<string, string>> = {
  not_possible:
    "The spoken farewell was not played: the session was no longer connected when the interview ended.",
  failed: "The spoken farewell could not be played in this browser.",
  timeout: "Playback of the spoken farewell could not be confirmed in time.",
}

const AGENT_FAREWELL_NOTES: Partial<Record<string, string>> = {
  played:
    "The interviewer finished the farewell. This confirms agent playout, not complete playback in your browser.",
  not_possible:
    "The interviewer could not start the farewell through the connected audio session.",
  failed: "The interviewer's farewell did not finish successfully.",
  timeout:
    "The interviewer's farewell did not finish within the closing time limit.",
}

/** Why the interview ended and, when the audio was not heard, the farewell
 *  in writing. Never claims a farewell was played. */
export function InterviewEnding({ interview }: { interview: Interview }) {
  const reason = interview.ended_reason
    ? TECHNICAL_REASONS[interview.ended_reason]
    : undefined
  const farewellNote = interview.farewell_status
    ? (interview.farewell_confirmation_source === "agent_playout"
        ? AGENT_FAREWELL_NOTES
        : FAREWELL_NOTES)[interview.farewell_status]
    : undefined
  if (!reason && !farewellNote) return null
  return (
    <Alert className="text-left">
      <InfoIcon />
      <AlertTitle>How this interview ended</AlertTitle>
      <AlertDescription>
        {reason && <p>{reason}</p>}
        {farewellNote && <p>{farewellNote}</p>}
        {interview.farewell_text && (
          <blockquote className="mt-2 border-l-2 border-primary pl-3 italic">
            {interview.farewell_text}
          </blockquote>
        )}
      </AlertDescription>
    </Alert>
  )
}
