import type { Interview } from "@/lib/api"
import { useEvaluationRequest } from "@/hooks/use-evaluation-request"
import { Button } from "@/components/ui/button"

export function AssessmentRequest({ interview }: { interview: Interview }) {
  const request = useEvaluationRequest(interview.id)
  const running = interview.status === "evaluating"
  const failed = interview.status === "evaluation_failed"
  return (
    <div className="flex flex-col gap-3 rounded-xl border p-4 text-sm">
      {interview.evaluation_is_previous && (
        <p role="status">
          This is the earlier saved assessment.
          {running
            ? " A new assessment is in progress; it will appear when ready."
            : failed
              ? " The new assessment could not finish. The earlier feedback remains available."
              : " The saved answers have changed and need a new assessment."}
        </p>
      )}
      <p>A new assessment preserves the previous feedback in the history.</p>
      {request.isError && <p role="alert">{request.error.message}</p>}
      <Button
        variant="outline"
        className="self-start"
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
  )
}
