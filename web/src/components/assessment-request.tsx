import type { Interview } from "@/lib/api"
import { useEvaluationRequest } from "@/hooks/use-evaluation-request"
import { InfoIcon } from "lucide-react"

import { Button } from "@/components/ui/button"

export function AssessmentRequest({ interview }: { interview: Interview }) {
  const request = useEvaluationRequest(interview.id)
  const running = interview.status === "evaluating"
  const failed = interview.status === "evaluation_failed"
  return (
    <div className="flex flex-col gap-3 text-sm">
      {interview.evaluation_is_previous && (
        <p
          role="status"
          className="flex gap-3 rounded-lg bg-primary-container/60 px-4 py-3 text-on-primary-container"
        >
          <InfoIcon
            aria-hidden
            className="mt-0.5 size-5 shrink-0 text-primary"
          />
          <span>
            This is the earlier saved assessment.
            {running
              ? " A new assessment is in progress; it will appear when ready."
              : failed
                ? " The new assessment could not finish. The earlier feedback remains available."
                : " The saved answers have changed and need a new assessment."}
          </span>
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground">
          A new assessment preserves the previous feedback in the history.
        </p>
        {request.isError && (
          <p role="alert" className="text-destructive">
            {request.error.message}
          </p>
        )}
        <Button
          variant="outline"
          size="sm"
          disabled={request.isPending}
          onClick={() => request.mutate()}
        >
          {request.isPending
            ? "Requesting…"
            : request.isError
              ? "Retry assessment request"
              : failed
                ? "Retry assessment"
                : running
                  ? "Request another assessment"
                  : "Request new assessment"}
        </Button>
      </div>
    </div>
  )
}
