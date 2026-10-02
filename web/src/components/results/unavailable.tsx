import * as React from "react"
import {
  AlertCircleIcon,
  HistoryIcon,
  RotateCcwIcon,
  SlidersHorizontalIcon,
  ClipboardCheckIcon,
} from "lucide-react"

import { Mascot } from "@/components/mascot"
import { RepeatOptionsDialog } from "@/components/repeat-options"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { LinkButton } from "@/components/ui/link-button"
import { PageShell } from "@/components/ui/page"
import { useEvaluationRequest } from "@/hooks/use-evaluation-request"
import { useRepeatInterview } from "@/hooks/use-repeat-interview"
import { ApiError } from "@/lib/api"
import type { Interview } from "@/lib/api"

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

/** A row whose planning failed. Nothing about it can run, so the one thing
 *  on offer is a fresh plan off the same resume and offer. */
export function FailedPanel({ interview }: { interview: Interview }) {
  const repeat = useRepeatInterview(interview.id)
  const [changes, setChanges] = React.useState(false)
  // A closing that could not be persisted also lands here; it ran, but its
  // transcript cannot be evaluated.
  const ran = interview.closing_id !== null
  return (
    <PageShell center>
      <EmptyState
        illustration={<Mascot state="error" className="w-36" />}
        title={
          ran
            ? "This interview could not be saved"
            : "This interview could not be planned"
        }
        description={
          <>
            {ran
              ? "The interview ended, but its transcript could not be saved completely, so it cannot be evaluated."
              : (interview.ended_reason ??
                "Something went wrong while preparing the questions.")}{" "}
            Repeating it plans a new interview from the same resume and offer.
          </>
        }
        actions={
          <>
            <LinkButton variant="ghost" to="/interviews">
              <HistoryIcon />
              History
            </LinkButton>
            <Button
              variant="outline"
              onClick={() => setChanges(true)}
              disabled={repeat.isPending}
            >
              <SlidersHorizontalIcon />
              Repeat with changes…
            </Button>
            <Button
              onClick={() => repeat.mutate(undefined)}
              disabled={repeat.isPending}
            >
              <RotateCcwIcon />
              {repeat.isPending ? "Planning…" : "Repeat this interview"}
            </Button>
          </>
        }
      >
        {repeat.isError && (
          <Alert variant="destructive" className="max-w-md">
            <AlertCircleIcon />
            <AlertDescription>{errorMessage(repeat.error)}</AlertDescription>
          </Alert>
        )}
      </EmptyState>
      <RepeatOptionsDialog
        open={changes}
        onOpenChange={setChanges}
        interview={interview}
        pending={repeat.isPending}
        onRepeat={(body) => repeat.mutate(body)}
      />
    </PageShell>
  )
}

/** An `interviewing` row past its reconnect window: the worker died mid-run
 *  and the server has not sealed it yet, so there is no room left to rejoin.
 *  What remains is the transcript up to the cut — evaluate it as it stands,
 *  or plan the interview again. */
export function InterruptedPanel({ interview }: { interview: Interview }) {
  const repeat = useRepeatInterview(interview.id)
  const evaluate = useEvaluationRequest(interview.id)
  const [changes, setChanges] = React.useState(false)
  const busy = evaluate.isPending || repeat.isPending

  return (
    <PageShell center>
      <EmptyState
        illustration={<Mascot state="warning" className="w-36" />}
        title="This interview was interrupted"
        description="The connection to the interviewer was lost and the interview can no longer be resumed. What was recorded up to that point is saved — you can evaluate it, or repeat the interview from the start with the same resume and offer."
        actions={
          <>
            <LinkButton variant="ghost" to="/interviews">
              <HistoryIcon />
              History
            </LinkButton>
            <Button
              variant="outline"
              onClick={() => repeat.mutate(undefined)}
              disabled={busy}
              title="Plan a fresh interview for the same role and resume"
            >
              <RotateCcwIcon />
              {repeat.isPending ? "Planning…" : "Repeat this interview"}
            </Button>
            <Button onClick={() => evaluate.mutate()} disabled={busy}>
              <ClipboardCheckIcon />
              {evaluate.isPending ? "Starting…" : "Evaluate what was recorded"}
            </Button>
          </>
        }
      >
        {(evaluate.isError || repeat.isError) && (
          <Alert variant="destructive" className="max-w-md">
            <AlertCircleIcon />
            <AlertDescription>
              {errorMessage(evaluate.isError ? evaluate.error : repeat.error)}
            </AlertDescription>
          </Alert>
        )}
      </EmptyState>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setChanges(true)}
        disabled={busy}
      >
        <SlidersHorizontalIcon />
        Repeat with changes…
      </Button>
      <RepeatOptionsDialog
        open={changes}
        onOpenChange={setChanges}
        interview={interview}
        pending={busy}
        onRepeat={(body) => repeat.mutate(body)}
      />
    </PageShell>
  )
}
