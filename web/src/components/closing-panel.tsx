import * as React from "react"
import { HistoryIcon, InfoIcon } from "lucide-react"

import { PostCallFrame } from "@/components/session/finalizing"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { LinkButton } from "@/components/ui/link-button"
import { LinearProgress } from "@/components/ui/spinner"
import type { Interview } from "@/lib/api"

// Initial closing lease plus two sweep intervals, as in the live supervision.
const MAX_WAIT_MS = 45_000

/** Supervision for a closing observed without a local close request (a
 *  reload or a second tab). Anchored to the server's remaining time on a
 *  monotonic clock; polling never extends the deadline. */
export function ClosingPanel({ interview }: { interview: Interview }) {
  const deadline = React.useRef<number | null>(null)
  const [pending, setPending] = React.useState(false)
  const remaining = interview.closing_remaining_seconds

  React.useEffect(() => {
    const now = performance.now()
    const serverBound =
      typeof remaining === "number" && Number.isFinite(remaining)
        ? now + Math.max(0, remaining) * 1000
        : Infinity
    deadline.current = Math.min(
      deadline.current ?? now + MAX_WAIT_MS,
      serverBound
    )
    const wait = deadline.current - now
    if (wait <= 0) {
      setPending(true)
      return
    }
    const timer = setTimeout(() => setPending(true), wait)
    return () => clearTimeout(timer)
  }, [remaining])

  return (
    <PostCallFrame
      title="Wrapping up"
      description="The farewell is not replayed after a reload. Results appear only once the transcript is saved."
    >
      <div className="flex flex-col items-center gap-6">
        {pending ? (
          <Alert variant="info">
            <InfoIcon />
            <AlertDescription>
              Closing recovery is pending. The server keeps saving the interview
              without this page; you can check the saved interview later.
            </AlertDescription>
          </Alert>
        ) : (
          <div className="flex w-full flex-col gap-3">
            <LinearProgress label="Saving the transcript" />
            <p
              role="status"
              className="text-center text-sm text-muted-foreground"
            >
              Closing the interview and saving the transcript…
            </p>
          </div>
        )}
        <LinkButton variant="outline" to="/interviews">
          <HistoryIcon />
          View saved interviews
        </LinkButton>
      </div>
    </PostCallFrame>
  )
}
