import type { Assessment, Evaluation, Milestone } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

const ASSESSMENT_LABELS: Record<Assessment, string> = {
  exceeds: "Exceeds the criterion",
  meets: "Meets the criterion",
  partial: "Partly demonstrated",
  below: "Needs practice",
  not_assessable: "Not assessed",
}

export function EvaluationOutcome({
  evaluation,
  level,
  duration,
  transcriptPartial = false,
  captureIntegrityPending = false,
}: {
  evaluation: Evaluation
  level: string
  duration: string
  transcriptPartial?: boolean
  captureIntegrityPending?: boolean
}) {
  const complete =
    !captureIntegrityPending &&
    evaluation.evaluation_status === "complete" &&
    evaluation.score !== null &&
    evaluation.hired !== null
  const insufficient = evaluation.evaluation_status === "insufficient"
  return (
    <Card>
      <CardContent className="flex flex-col gap-4 @xl/main:flex-row @xl/main:items-center @xl/main:gap-8">
        {complete ? (
          <div className="flex shrink-0 items-baseline gap-1">
            <span className="text-5xl leading-none font-bold">
              {evaluation.score}
            </span>
            <span className="text-xl text-muted-foreground">/100</span>
          </div>
        ) : (
          <Badge variant="secondary">
            {insufficient ? "Insufficient evidence" : "Partial assessment"}
          </Badge>
        )}
        <div className="flex flex-1 flex-col gap-2">
          {complete ? (
            <>
              <p className="text-sm font-medium">
                {evaluation.hired ? "Hired" : "Not hired"} · Interview
                simulation
              </p>
              {evaluation.score_gap && (
                <p className="text-sm text-muted-foreground">
                  Why not higher: {evaluation.score_gap}
                </p>
              )}
            </>
          ) : (
            <p className="text-sm">
              {insufficient
                ? "There is not enough candidate evidence to assess this interview."
                : transcriptPartial
                  ? "Feedback covers the saved answers. Transcript completeness could not be confirmed."
                  : "Feedback covers the answers observed. The available evidence does not support a complete assessment."}{" "}
              No overall score or hiring verdict is available.
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Assessed as {level} · {duration}
          </p>
          {evaluation.coverage !== undefined && (
            <p className="text-xs text-muted-foreground">
              Criteria observed: {Math.round(evaluation.coverage * 100)}%.
              Coverage measures what was assessed, not ability.
            </p>
          )}
          {captureIntegrityPending && (
            <p role="alert" className="text-sm text-destructive">
              This result cannot show an overall score because its saved answers
              have pending review or a confirmed omission. Review the saved
              answers to see the recording history.
            </p>
          )}
          {transcriptPartial && (
            <p className="text-xs text-muted-foreground">
              The connection ended before all transcript data could be
              confirmed. Missing audio is not treated as an incorrect answer.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export function CriterionFeedback({
  evaluation,
  milestones,
}: {
  evaluation: Evaluation
  milestones: Milestone[]
}) {
  if (!evaluation.criteria?.length) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Evidence and next practice</CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-6">
          {evaluation.criteria.map((criterion) => {
            const milestone = milestones.find(
              (m) => m.id === criterion.milestone_id
            )
            return (
              <li key={criterion.milestone_id} className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-medium">
                    {milestone?.title ?? "Criterion"}
                  </h3>
                  <Badge variant="outline">
                    {ASSESSMENT_LABELS[criterion.assessment]}
                  </Badge>
                </div>
                <p className="text-sm whitespace-pre-wrap">
                  {criterion.rationale}
                </p>
                {criterion.evidence.map((ref, index) => (
                  <blockquote
                    key={`${ref.message_id}-${index}`}
                    className="border-l-2 pl-3 text-sm"
                  >
                    <p className="whitespace-pre-wrap">“{ref.quote}”</p>
                    <footer className="mt-1 text-xs text-muted-foreground">
                      Candidate answer #{ref.message_id}
                      {ref.message_version != null &&
                        ` · version ${ref.message_version}`}
                    </footer>
                  </blockquote>
                ))}
                {criterion.practice && (
                  <div className="rounded-lg bg-muted p-3 text-sm">
                    <p className="mb-1 font-medium">Try this next</p>
                    <p className="whitespace-pre-wrap">{criterion.practice}</p>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      </CardContent>
    </Card>
  )
}
