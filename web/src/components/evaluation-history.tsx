import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { getEvaluationHistory } from "@/lib/api"
import type { Milestone } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Disclosure } from "@/components/ui/disclosure"
import { Spinner } from "@/components/ui/spinner"
import { CriterionFeedback, EvaluationOutcome } from "./evaluation-feedback"

type History = Awaited<ReturnType<typeof getEvaluationHistory>>
type Attempt = History["attempts"][number]

const STATUS_LABELS: Record<string, string> = {
  pending: "Waiting to start",
  running: "In progress",
  completed: "Completed",
  succeeded: "Completed",
  failed: "Could not finish",
  superseded: "Replaced by a later request",
  abandoned: "Interrupted",
}

export function EvaluationHistory({
  interviewId,
  milestones,
  duration,
}: {
  interviewId: string
  milestones: Milestone[]
  duration: string
}) {
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: ["evaluation-history", interviewId],
    queryFn: () => getEvaluationHistory(interviewId),
    enabled: open,
    refetchInterval: (observedQuery) =>
      open &&
      observedQuery.state.data?.requests.some((request) =>
        ["pending", "running"].includes(request.status)
      )
        ? 2000
        : false,
  })
  return (
    <Disclosure
      summary="Evaluation history"
      onToggle={(event) => setOpen(event.currentTarget.open)}
      bodyClassName="flex flex-col gap-4"
    >
      {query.isError && (
        <div className="flex flex-wrap items-center gap-3">
          <p role="alert" className="text-destructive">
            Evaluation history could not be loaded.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            Reload history
          </Button>
        </div>
      )}
      {open && query.isPending && (
        <p className="flex items-center gap-2 text-muted-foreground">
          <Spinner />
          Loading evaluation history…
        </p>
      )}
      {query.data?.requests.map((request, index) => (
        <div key={request.id} className="flex flex-col gap-1 text-sm">
          <p className="flex flex-wrap items-center gap-2 font-medium">
            Assessment {index + 1}
            <Badge
              variant={
                request.id === query.data.current_request_id
                  ? "default"
                  : "secondary"
              }
            >
              {request.id === query.data.current_request_id
                ? "Current request"
                : "Earlier request"}
            </Badge>
          </p>
          <p className="text-muted-foreground">
            {request.automatic ? "Automatic" : "Requested manually"} ·{" "}
            {STATUS_LABELS[request.status] ?? "Status unavailable"} ·{" "}
            {request.attempts} attempts
          </p>
          {request.seal_version != null && (
            <p className="text-muted-foreground">
              Saved answers version {request.seal_version}
            </p>
          )}
          {query.data.attempts
            .filter((attempt) => attempt.request_id === request.id)
            .map((attempt) => (
              <HistoricalAttempt
                key={attempt.id}
                attempt={attempt}
                milestones={milestones}
                duration={duration}
              />
            ))}
        </div>
      ))}
      {query.data?.requests.length === 0 &&
        query.data.attempts.length === 0 && (
          <p className="text-muted-foreground">
            No assessment history was recorded for this interview.
          </p>
        )}
    </Disclosure>
  )
}

function HistoricalAttempt({
  attempt,
  milestones,
  duration,
}: {
  attempt: Attempt
  milestones: Milestone[]
  duration: string
}) {
  const result = attempt.result
  return (
    <details className="mt-2 rounded-lg bg-muted px-3 py-2">
      <summary className="text-label cursor-pointer py-1">
        {attempt.ordinal != null
          ? `Attempt ${attempt.ordinal}`
          : "Saved assessment"}
        {" · "}
        {STATUS_LABELS[attempt.status] ?? "Status unavailable"}
      </summary>
      {result && (
        <div className="mt-4 flex flex-col gap-4">
          <EvaluationOutcome
            evaluation={result}
            level={result.seniority_evaluated || "Saved level"}
            duration={duration}
            captureIntegrityPending={attempt.invalidated}
          />
          <p className="whitespace-pre-wrap">{result.rationale}</p>
          <CriterionFeedback evaluation={result} milestones={milestones} />
          <FeedbackList title="Strengths" items={result.strengths} />
          <FeedbackList title="Areas to practise" items={result.weaknesses} />
          <FeedbackList
            title="Not counted against you at this level"
            items={result.calibration_notes}
          />
        </div>
      )}
      {attempt.error && (
        <p className="mt-2 text-muted-foreground">
          This attempt could not produce a valid assessment.
        </p>
      )}
    </details>
  )
}

function FeedbackList({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null
  return (
    <div>
      <p className="mb-1 font-medium">{title}</p>
      <ul className="list-disc space-y-1 pl-5">
        {items.map((item, index) => (
          <li key={index} className="whitespace-pre-wrap">
            {item}
          </li>
        ))}
      </ul>
    </div>
  )
}
