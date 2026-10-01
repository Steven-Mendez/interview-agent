import * as React from "react"
import { Link } from "@tanstack/react-router"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { PageContainer, PageShell } from "@/components/ui/page"
import { Spinner } from "@/components/ui/spinner"
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
    <PageShell center>
      <PageContainer
        variant="narrow"
        className="flex flex-col items-center gap-4 text-center"
      >
        {pending ? (
          <Alert className="w-full text-left">
            <AlertDescription>
              Closing recovery is pending. The server keeps saving the interview
              without this page; you can check the saved interview later.
            </AlertDescription>
          </Alert>
        ) : (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner />
            Closing the interview and saving the transcript…
          </p>
        )}
        <p className="text-sm text-muted-foreground">
          The farewell is not replayed after a reload. Results appear only once
          the transcript is saved.
        </p>
        <Button variant="outline" render={<Link to="/interviews" />}>
          View saved interviews
        </Button>
      </PageContainer>
    </PageShell>
  )
}
