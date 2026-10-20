import { ChevronDownIcon, LightbulbIcon } from "lucide-react"

import type {
  Assessment,
  AnswerRevision,
  CriterionEvaluation,
  Evaluation,
  Milestone,
} from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { TopicMarker, topicLabel } from "@/components/interview-progress"
import { cn } from "@/lib/utils"

const ASSESSMENT_LABELS: Record<Assessment, string> = {
  exceeds: "Exceeds the criterion",
  meets: "Meets the criterion",
  partial: "Partly demonstrated",
  below: "Needs practice",
  not_assessable: "Not assessed",
}

const ASSESSMENT_TONES: Record<
  Assessment,
  "success" | "warning" | "destructive" | "secondary"
> = {
  exceeds: "success",
  meets: "success",
  partial: "warning",
  below: "destructive",
  not_assessable: "secondary",
}

/** The same traffic light the history uses: green once hired, otherwise
 *  red or amber by how far the score sits from a pass. */
export function scoreTone(score: number, hired: boolean) {
  return hired ? "success" : score < 40 ? "destructive" : "warning"
}

function ScoreRing({ score, hired }: { score: number; hired: boolean }) {
  const tone = scoreTone(score, hired)
  const r = 44
  const c = 2 * Math.PI * r
  return (
    <div className="relative flex size-32 shrink-0 items-center justify-center">
      <svg
        viewBox="0 0 100 100"
        className="absolute inset-0 -rotate-90"
        aria-hidden
      >
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          strokeWidth="6"
          className="stroke-surface-high"
        />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - Math.max(0, Math.min(100, score)) / 100)}
          className={cn(
            "transition-[stroke-dashoffset] duration-500 ease-standard",
            tone === "success" && "stroke-success",
            tone === "warning" && "stroke-warning",
            tone === "destructive" && "stroke-destructive"
          )}
        />
      </svg>
      <div className="flex items-baseline gap-0.5">
        <span className="font-heading text-[2.5rem] leading-none font-normal tabular-nums">
          {score}
        </span>
        <span className="text-sm text-muted-foreground">/100</span>
      </div>
    </div>
  )
}

export function EvaluationOutcome({
  evaluation,
  transcriptPartial = false,
  captureIntegrityPending = false,
}: {
  evaluation: Evaluation
  transcriptPartial?: boolean
  captureIntegrityPending?: boolean
}) {
  const complete =
    (!evaluation.assessment_scope ||
      evaluation.assessment_scope === "interview") &&
    !captureIntegrityPending &&
    evaluation.evaluation_status === "complete" &&
    evaluation.score !== null &&
    evaluation.hired !== null
  const insufficient = evaluation.evaluation_status === "insufficient"
  const practice = evaluation.assessment_scope === "practice"
  const limited = evaluation.assessment_scope === "limited"
  return (
    <section
      aria-label="Overall result"
      className="flex flex-col gap-5 rounded-2xl bg-muted px-6 py-6 @2xl/main:flex-row @2xl/main:items-center @2xl/main:gap-8"
    >
      {complete ? (
        <ScoreRing score={evaluation.score!} hired={evaluation.hired!} />
      ) : (
        <Badge variant="secondary" className="h-8 self-start px-3 text-sm">
          {practice
            ? "Practice feedback"
            : insufficient
              ? "Insufficient evidence"
              : limited
                ? "Limited role coverage"
                : "Partial assessment"}
        </Badge>
      )}
      <div className="flex flex-1 flex-col gap-2">
        {complete ? (
          <>
            <p className="flex flex-wrap items-center gap-2">
              <Badge variant={evaluation.hired ? "success" : "outline"}>
                {evaluation.hired ? "Hired" : "Not hired"}
              </Badge>
              <span className="text-sm text-muted-foreground">
                Interview simulation
              </span>
            </p>
          </>
        ) : (
          <p className="text-sm leading-relaxed">
            {insufficient
              ? "There is not enough candidate evidence to assess this interview."
              : practice
                ? "Feedback covers the selected skills for focused practice."
                : limited
                  ? "Some essential role requirements were outside this session."
                  : transcriptPartial
                    ? "Feedback covers the saved answers. Transcript completeness could not be confirmed."
                    : "Feedback covers the answers observed. The available evidence does not support a complete assessment."}{" "}
            No overall score or hiring verdict is available.
          </p>
        )}
        <p className="text-sm leading-relaxed whitespace-pre-wrap">
          {evaluation.rationale}
        </p>
        {!!evaluation.uncovered_requirements?.length && (
          <div className="text-sm text-muted-foreground">
            <p>Essential role requirements outside this session:</p>
            <ul className="list-disc pl-5">
              {evaluation.uncovered_requirements.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
            <p>Missing coverage is not a demonstrated weakness.</p>
          </div>
        )}
        {evaluation.coverage !== undefined && evaluation.coverage < 1 && (
          <p className="text-xs text-muted-foreground">
            Selected criteria observed: {Math.round(evaluation.coverage * 100)}
            %. Coverage measures what was assessed, not ability.
          </p>
        )}
        {captureIntegrityPending && (
          <p role="alert" className="text-sm text-destructive">
            This result cannot show an overall score because its saved answers
            have pending review or a confirmed omission. Review the saved
            answers in the Transcript tab to see the recording history.
          </p>
        )}
        {transcriptPartial && (
          <p className="text-xs text-muted-foreground">
            The connection ended before all transcript data could be confirmed.
            Missing audio is not treated as an incorrect answer.
          </p>
        )}
      </div>
    </section>
  )
}

function CriterionBody({ criterion }: { criterion: CriterionEvaluation }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm leading-relaxed whitespace-pre-wrap">
        {criterion.rationale}
      </p>
      {criterion.evidence.map((ref, index) => (
        <blockquote
          key={`${ref.message_id}-${index}`}
          className="border-l-[3px] border-primary/60 pl-3 text-sm"
        >
          <p className="whitespace-pre-wrap">“{ref.quote}”</p>
        </blockquote>
      ))}
      {criterion.practice && (
        <div className="flex gap-3 rounded-lg bg-primary-container/50 p-3 text-sm">
          <LightbulbIcon
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-primary"
          />
          <div>
            <p className="mb-1 font-medium">Try this next</p>
            <p className="whitespace-pre-wrap">{criterion.practice}</p>
          </div>
        </div>
      )}
    </div>
  )
}

function AnswerComparison({ revision }: { revision: AnswerRevision }) {
  return (
    <article className="overflow-hidden rounded-xl border bg-card">
      <div className="flex flex-col gap-2 px-4 py-4">
        {revision.question && (
          <p className="text-sm leading-relaxed font-medium">
            {revision.question}
          </p>
        )}
      </div>
      <div className="border-y" aria-label="Answer comparison">
        <div className="flex gap-3 bg-destructive/8 px-4 py-3">
          <span aria-hidden className="font-mono text-destructive">
            −
          </span>
          <div className="min-w-0">
            <p className="mb-1 text-xs font-medium text-destructive">
              You said
            </p>
            <p className="text-sm leading-relaxed whitespace-pre-wrap">
              {revision.original.quote}
            </p>
          </div>
        </div>
        <div className="flex gap-3 bg-success/8 px-4 py-3">
          <span aria-hidden className="font-mono text-success">
            +
          </span>
          <div className="min-w-0">
            <p className="mb-1 text-xs font-medium text-success">
              {revision.basis === "knowledge_example"
                ? "After practising, you could explain"
                : "You could say"}
            </p>
            <p className="text-sm leading-relaxed whitespace-pre-wrap">
              {revision.suggested_answer}
            </p>
          </div>
        </div>
      </div>
      <details className="group/explanation px-4 py-3 text-sm">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-medium [&::-webkit-details-marker]:hidden">
          Why this helps
          <ChevronDownIcon
            aria-hidden
            className="size-4 shrink-0 text-muted-foreground transition-transform group-open/explanation:rotate-180"
          />
        </summary>
        <div className="flex flex-col gap-3 pt-3">
          <p className="leading-relaxed whitespace-pre-wrap">
            {revision.explanation}
          </p>
          {revision.missing_details.length > 0 && (
            <div className="rounded-lg bg-muted p-3">
              <p className="mb-2 font-medium">Add your real details</p>
              <ul className="list-disc space-y-1 pl-4">
                {revision.missing_details.map((detail, index) => (
                  <li key={index}>{detail}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </details>
    </article>
  )
}

export function AnswerComparisons({ evaluation }: { evaluation: Evaluation }) {
  const revisions = evaluation.answer_revisions ?? []
  if (!revisions.length) return null
  return (
    <section aria-label="Improve your answers" className="flex flex-col gap-4">
      <h2 className="text-title">Improve your answers</h2>
      <div className="grid items-start gap-4 @4xl/main:grid-cols-2">
        {revisions.map((revision) => (
          <AnswerComparison
            key={`${revision.milestone_id}-${revision.original.message_id}`}
            revision={revision}
          />
        ))}
      </div>
    </section>
  )
}

/** Per-topic evidence as expandable rows. With `allTopics`, topics the
 *  evaluator did not assess still appear, with their interview status. */
export function CriterionFeedback({
  evaluation,
  milestones,
  allTopics = false,
  title = "Evidence and next practice",
}: {
  evaluation: Evaluation | null
  milestones: Milestone[]
  allTopics?: boolean
  title?: string | null
}) {
  const criteria = evaluation?.criteria ?? []
  const rows: Array<{
    key: string
    milestone: Milestone | undefined
    index: number
    criterion: CriterionEvaluation | undefined
  }> = allTopics
    ? [
        ...milestones.map((m, index) => ({
          key: m.id,
          milestone: m,
          index,
          criterion: criteria.find((c) => c.milestone_id === m.id),
        })),
        // Criteria for topics no longer in the plan still deserve a row.
        ...criteria
          .filter((c) => !milestones.some((m) => m.id === c.milestone_id))
          .map((c, i) => ({
            key: c.milestone_id,
            milestone: undefined,
            index: milestones.length + i,
            criterion: c,
          })),
      ]
    : criteria.map((c, i) => {
        const index = milestones.findIndex((m) => m.id === c.milestone_id)
        return {
          key: c.milestone_id,
          milestone: milestones[index],
          index: index >= 0 ? index : i,
          criterion: c,
        }
      })
  if (rows.length === 0) return null
  return (
    <section aria-label={title ?? "Topics"} className="flex flex-col gap-3">
      {title && <h2 className="text-title">{title}</h2>}
      <ul className="divide-y overflow-hidden rounded-xl border bg-card">
        {rows.map(({ key, milestone, index, criterion }) => {
          const heading = (
            <>
              {milestone && !criterion ? (
                <TopicMarker milestone={milestone} index={index} />
              ) : (
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full border text-xs text-muted-foreground tabular-nums">
                  {index + 1}
                </span>
              )}
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="text-sm font-medium">
                  {milestone?.title ?? "Criterion"}
                </span>
                {milestone && !criterion && (
                  <span className="text-xs text-muted-foreground">
                    {topicLabel(milestone)}
                    {milestone.competency && ` · ${milestone.competency}`}
                  </span>
                )}
              </span>
              {criterion && (
                <Badge
                  variant={ASSESSMENT_TONES[criterion.assessment]}
                  className="h-auto min-h-6 max-w-32 shrink-0 py-0.5 text-center whitespace-normal sm:max-w-none"
                >
                  {ASSESSMENT_LABELS[criterion.assessment]}
                </Badge>
              )}
            </>
          )
          return (
            <li key={key}>
              {criterion ? (
                <details className="group/criterion">
                  <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3.5 transition-colors hover:bg-foreground/[0.04] [&::-webkit-details-marker]:hidden">
                    {heading}
                    <ChevronDownIcon
                      aria-hidden
                      className="size-5 shrink-0 text-muted-foreground transition-transform duration-200 group-open/criterion:rotate-180"
                    />
                  </summary>
                  <div className="px-4 pt-1 pb-5 sm:pl-13">
                    <CriterionBody criterion={criterion} />
                  </div>
                </details>
              ) : (
                <div className="flex items-center gap-3 px-4 py-3.5">
                  {heading}
                  {evaluation && (
                    <span className="text-xs text-muted-foreground">
                      No feedback
                    </span>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
