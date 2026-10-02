import * as React from "react"
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  CalendarIcon,
  CheckCircle2Icon,
  ClockIcon,
  EllipsisVerticalIcon,
  GaugeIcon,
  RotateCcwIcon,
  SlidersHorizontalIcon,
  XCircleIcon,
} from "lucide-react"

import { MascotHead } from "@/components/mascot"
import { AssessmentRequest } from "@/components/assessment-request"
import {
  CriterionFeedback,
  EvaluationOutcome,
} from "@/components/evaluation-feedback"
import { EvaluationHistory } from "@/components/evaluation-history"
import { InterviewEnding } from "@/components/interview-ending"
import { isSettled } from "@/components/interview-progress"
import { InterviewStatusChip } from "@/components/interview-status"
import { RepeatOptionsDialog } from "@/components/repeat-options"
import { TranscriptView } from "@/components/results/transcript-view"
import { TranscriptReview } from "@/components/transcript-review"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { LinkButton } from "@/components/ui/link-button"
import { PageContainer, PageShell, Section } from "@/components/ui/page"
import { LinearProgress } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useEvaluationWait } from "@/hooks/use-evaluation-wait"
import { interviewErrorMessage, quotaBlock, useMe } from "@/hooks/use-me"
import { useRepeatInterview } from "@/hooks/use-repeat-interview"
import { ApiError, SENIORITY_LABELS, durationLabel } from "@/lib/api"
import type { Interview } from "@/lib/api"
import { cn } from "@/lib/utils"

const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

/** An interview's results, back in the application: what happened, the
 *  verdict when there is one, per-topic evidence and the transcript. */
export function ResultsPage({
  interview,
  endedAt,
}: {
  interview: Interview
  endedAt: number | null
}) {
  const repeat = useRepeatInterview(interview.id)
  const me = useMe()
  // An exhausted guest cannot repeat: the reason shows instead of a 429.
  const quotaBlocked = quotaBlock(me.data)
  const [repeatOpen, setRepeatOpen] = React.useState(false)
  const agentName = interview.interviewer?.agent_name || "Interviewer"
  const milestones = interview.milestones
  const duration = durationLabel(
    interview.interview_length,
    interview.max_minutes
  )

  return (
    <PageShell className="animate-in duration-300 fade-in">
      <PageContainer variant="wide" className="flex flex-col gap-6">
        <LinkButton
          variant="quiet"
          size="sm"
          to="/interviews"
          className="-ml-3 self-start"
        >
          <ArrowLeftIcon />
          History
        </LinkButton>

        <header className="flex flex-col gap-4 @3xl/main:flex-row @3xl/main:items-start @3xl/main:justify-between">
          <div className="flex min-w-0 flex-col gap-3">
            <p className="text-overline text-muted-foreground uppercase">
              Interview results
            </p>
            <h1 className="text-headline line-clamp-2">{interview.title}</h1>
            <ul className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted-foreground">
              <li>
                <InterviewStatusChip row={interview} />
              </li>
              <li className="flex items-center gap-1.5">
                <CalendarIcon aria-hidden className="size-4" />
                <time dateTime={interview.created_at}>
                  {dateFormat.format(new Date(interview.created_at))}
                </time>
              </li>
              <li className="flex items-center gap-1.5">
                <GaugeIcon aria-hidden className="size-4" />
                {SENIORITY_LABELS[interview.seniority]}
              </li>
              <li className="flex items-center gap-1.5">
                <ClockIcon aria-hidden className="size-4" />
                {duration}
              </li>
              {interview.interviewer?.agent_name && (
                <li className="flex items-center gap-1.5">
                  <MascotHead className="size-4" />
                  {interview.interviewer.agent_name}
                </li>
              )}
              {interview.repeat_of_id && (
                <li>
                  <Badge variant="outline">
                    <RotateCcwIcon aria-hidden />
                    Re-run
                  </Badge>
                </li>
              )}
            </ul>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="outline"
              onClick={() => repeat.mutate(undefined)}
              disabled={repeat.isPending || quotaBlocked !== null}
              title={
                quotaBlocked ??
                "Plan a fresh interview for the same role and resume"
              }
            >
              <RotateCcwIcon />
              {repeat.isPending ? "Planning…" : "Repeat"}
            </Button>
            <DropdownMenu>
              <Tooltip>
                <TooltipTrigger
                  render={
                    <DropdownMenuTrigger
                      render={
                        <Button
                          variant="quiet"
                          size="icon"
                          aria-label="More actions"
                        >
                          <EllipsisVerticalIcon />
                        </Button>
                      }
                    />
                  }
                />
                <TooltipContent>More actions</TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  disabled={repeat.isPending || quotaBlocked !== null}
                  onClick={() => setRepeatOpen(true)}
                >
                  <SlidersHorizontalIcon />
                  Repeat with changes…
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>

        {repeat.isError ? (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>
              {interviewErrorMessage(repeat.error, me.data)}
            </AlertDescription>
          </Alert>
        ) : (
          quotaBlocked && (
            <p className="text-sm text-muted-foreground">{quotaBlocked}</p>
          )
        )}

        <Tabs defaultValue="overview">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="topics">
              Topics
              {milestones.length > 0 && (
                <span className="text-xs text-muted-foreground tabular-nums">
                  {milestones.filter(isSettled).length}/{milestones.length}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger value="transcript">Transcript</TabsTrigger>
          </TabsList>
          <TabsContent value="overview" className="pt-2">
            {interview.evaluation ? (
              <EvaluatedOverview interview={interview} duration={duration} />
            ) : (
              <PendingOverview
                interview={interview}
                endedAt={endedAt}
                duration={duration}
              />
            )}
          </TabsContent>
          <TabsContent value="topics" className="pt-2">
            {milestones.length === 0 &&
            !interview.evaluation?.criteria?.length ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                This interview has no planned topics.
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                <p className="text-sm text-muted-foreground">
                  {interview.evaluation?.criteria?.length
                    ? "Open a topic to see the evidence quoted from your answers and what to practise next."
                    : "How far each planned topic got."}
                </p>
                <CriterionFeedback
                  evaluation={interview.evaluation}
                  milestones={milestones}
                  allTopics
                  title={null}
                />
              </div>
            )}
          </TabsContent>
          <TabsContent value="transcript" className="pt-2">
            <PageContainer variant="reading" className="mx-0">
              <TranscriptView
                interviewId={interview.id}
                agentName={agentName}
              />
            </PageContainer>
          </TabsContent>
        </Tabs>

        <RepeatOptionsDialog
          open={repeatOpen}
          onOpenChange={setRepeatOpen}
          interview={interview}
          pending={repeat.isPending}
          onRepeat={(body) => repeat.mutate(body)}
        />
      </PageContainer>
    </PageShell>
  )
}

function EvaluatedOverview({
  interview,
  duration,
}: {
  interview: Interview
  duration: string
}) {
  const ev = interview.evaluation!
  // The earlier-assessment notice belongs at the top; the plain "request a
  // new assessment" action is a record-keeping task and sits with the rest.
  const assessmentOnTop =
    interview.evaluation_is_previous ||
    interview.status === "evaluating" ||
    interview.status === "evaluation_failed"
  return (
    <div className="flex flex-col gap-8">
      {assessmentOnTop && <AssessmentRequest interview={interview} />}
      <InterviewEnding interview={interview} />
      <EvaluationOutcome
        evaluation={ev}
        level={SENIORITY_LABELS[interview.seniority]}
        duration={duration}
        transcriptPartial={interview.transcript_integrity === "partial"}
        captureIntegrityPending={
          interview.capture_integrity_pending ||
          interview.evaluation_invalidated
        }
      />

      <div className="grid gap-8 @3xl/main:grid-cols-2">
        <FeedbackList
          title="Strengths"
          items={ev.strengths}
          icon={CheckCircle2Icon}
          iconClassName="text-success"
          empty="No strengths noted."
        />
        <FeedbackList
          title="Areas of concern"
          items={ev.weaknesses}
          icon={XCircleIcon}
          iconClassName="text-destructive"
          empty="No weaknesses noted."
        />
      </div>

      <Section title="Evaluator rationale">
        <p className="max-w-3xl text-sm leading-relaxed whitespace-pre-wrap">
          {ev.rationale}
        </p>
      </Section>

      {/* What the evaluator deliberately did NOT hold against the candidate
          because it sits above the role's level. Empty is the normal case —
          a non-empty list is the audit trail proving the calibration filter
          actually fired. */}
      {ev.calibration_notes.length > 0 && (
        <FeedbackList
          title="Not counted against you at this level"
          items={ev.calibration_notes}
          icon={GaugeIcon}
          iconClassName="text-muted-foreground"
          empty=""
        />
      )}

      <Records interview={interview} duration={duration}>
        {!assessmentOnTop && <AssessmentRequest interview={interview} />}
      </Records>
    </div>
  )
}

function PendingOverview({
  interview,
  endedAt,
  duration,
}: {
  interview: Interview
  endedAt: number | null
  duration: string
}) {
  const { showRetry, action, problem, retry } = useEvaluationWait(
    interview,
    endedAt
  )
  return (
    <div className="flex flex-col gap-8">
      <section
        aria-label="Evaluation"
        className="flex flex-col gap-4 rounded-2xl bg-muted px-6 py-6"
      >
        {showRetry ? (
          <>
            <div className="flex items-start gap-3">
              <AlertCircleIcon
                aria-hidden
                className="mt-0.5 size-5 shrink-0 text-destructive"
              />
              <div className="flex flex-col gap-1">
                <h2 className="text-title">No results yet</h2>
                <p role="alert" className="text-sm">
                  {retry.isError ? errorMessage(retry.error) : problem}
                </p>
                <p className="text-sm text-muted-foreground">
                  The saved transcript is not affected.
                </p>
              </div>
            </div>
            <Button
              className="self-start"
              onClick={() => retry.mutate()}
              disabled={retry.isPending}
            >
              {retry.isPending ? "Starting…" : `${action} evaluation`}
            </Button>
          </>
        ) : (
          <>
            <h2 className="text-title">Generating the evaluation</h2>
            <LinearProgress label="Evaluating the interview" />
            <p className="text-sm text-muted-foreground">
              Evaluating the interview… (can take a couple of minutes). This
              page updates by itself.
            </p>
          </>
        )}
      </section>
      <InterviewEnding interview={interview} />
      <Records interview={interview} duration={duration} />
    </div>
  )
}

/** Assessment history, saved-answer review and external traces: the
 *  record-keeping side of a result, folded away. */
function Records({
  interview,
  duration,
  children,
}: {
  interview: Interview
  duration: string
  children?: React.ReactNode
}) {
  return (
    <Section
      title="Records"
      description="Earlier assessments and saved answer versions."
    >
      <div className="flex flex-col gap-3">
        {children}
        <EvaluationHistory
          interviewId={interview.id}
          milestones={interview.milestones}
          duration={duration}
        />
        <TranscriptReview interviewId={interview.id} />
      </div>
    </Section>
  )
}

function FeedbackList({
  title,
  items,
  icon: Icon,
  iconClassName,
  empty,
}: {
  title: string
  items: string[]
  icon: React.ComponentType<{ className?: string }>
  iconClassName?: string
  empty: string
}) {
  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          {title}
          <span className="text-sm font-normal text-muted-foreground tabular-nums">
            {items.length}
          </span>
        </span>
      }
    >
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item, i) => (
            <li
              key={i}
              className="flex items-start gap-3 text-sm leading-relaxed"
            >
              <Icon
                aria-hidden
                className={cn("mt-0.5 size-[18px] shrink-0", iconClassName)}
              />
              <span>{item}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}
