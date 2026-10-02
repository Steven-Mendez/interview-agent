import * as React from "react"
import {
  AlertCircleIcon,
  CheckIcon,
  CircleDashedIcon,
  HistoryIcon,
  HomeIcon,
} from "lucide-react"

import { useImmersive } from "@/components/app-shell"
import { BrandLink } from "@/components/brand"
import { InterviewEnding } from "@/components/interview-ending"
import { Mascot } from "@/components/mascot"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { LinkButton } from "@/components/ui/link-button"
import { LinearProgress, Spinner } from "@/components/ui/spinner"
import { useEvaluationWait } from "@/hooks/use-evaluation-wait"
import { ApiError } from "@/lib/api"
import type { Interview } from "@/lib/api"
import { cn } from "@/lib/utils"

/** The calm page between leaving the room and the results: light, centered,
 *  with nothing around it but the product mark. */
export function PostCallFrame({
  title,
  description,
  illustration,
  children,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  illustration?: React.ReactNode
  children: React.ReactNode
}) {
  useImmersive()
  return (
    <div className="flex min-h-svh flex-col bg-background">
      <header className="flex h-16 shrink-0 items-center px-4 md:px-6">
        <BrandLink />
      </header>
      <div className="flex flex-1 animate-in items-center justify-center px-4 pb-16 duration-300 fade-in">
        <div className="flex w-full max-w-md flex-col gap-8">
          <div className="flex flex-col items-center gap-2 text-center">
            {illustration}
            <h1 className="text-display">{title}</h1>
            {description && (
              <p className="text-sm leading-relaxed text-muted-foreground">
                {description}
              </p>
            )}
          </div>
          {children}
        </div>
      </div>
    </div>
  )
}

type StepState = "done" | "active" | "error" | "pending"

function Step({
  state,
  title,
  detail,
}: {
  state: StepState
  title: string
  detail?: string
}) {
  return (
    <li className="flex items-start gap-4">
      <span
        aria-hidden
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-full",
          state === "done" && "bg-success-container text-success",
          state === "active" && "bg-primary-container",
          state === "error" && "bg-destructive-container text-destructive",
          state === "pending" && "text-muted-foreground"
        )}
      >
        {state === "done" ? (
          <CheckIcon className="size-4" strokeWidth={3} />
        ) : state === "active" ? (
          <Spinner className="size-4" />
        ) : state === "error" ? (
          <AlertCircleIcon className="size-4" />
        ) : (
          <CircleDashedIcon className="size-5" />
        )}
      </span>
      <div className="flex flex-col gap-0.5 pt-0.5">
        <p
          className={cn(
            "text-sm",
            state === "pending" ? "text-muted-foreground" : "font-medium"
          )}
        >
          {title}
          <span className="sr-only">
            {" "}
            —{" "}
            {state === "done"
              ? "done"
              : state === "active"
                ? "in progress"
                : state === "error"
                  ? "needs attention"
                  : "waiting"}
          </span>
        </p>
        {detail && <p className="text-xs text-muted-foreground">{detail}</p>}
      </div>
    </li>
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

/** Straight after the call: the interview is over and saved, and the
 *  evaluation is on its way. Results replace this the moment it lands. */
export function FinalizingRoom({
  interview,
  endedAt,
}: {
  interview: Interview
  endedAt: number | null
}) {
  const { showRetry, action, problem, retry } = useEvaluationWait(
    interview,
    endedAt
  )
  const evaluating = interview.status === "evaluating"

  return (
    <PostCallFrame
      title="Interview complete"
      illustration={
        <Mascot
          state={showRetry ? "warning" : "evaluating"}
          label={
            showRetry
              ? "The Interviewer Agent needs your attention"
              : "The Interviewer Agent is evaluating your interview"
          }
          className="mb-2 w-40"
        />
      }
      description="Your answers are saved. The evaluation usually takes a couple of minutes — your results open here when it is ready."
    >
      <div className="flex flex-col gap-6">
        {!showRetry && <LinearProgress label="Finalizing interview" />}
        <ol aria-label="Finalizing interview" className="flex flex-col gap-4">
          <Step state="done" title="Interview ended" />
          <Step state="done" title="Transcript saved" />
          <Step
            state={showRetry ? "error" : "active"}
            title={
              evaluating ? "Generating evaluation" : "Processing responses"
            }
            detail={showRetry ? undefined : "Can take a couple of minutes."}
          />
          <Step state="pending" title="Results ready" />
        </ol>

        {showRetry && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>
              {retry.isError ? errorMessage(retry.error) : problem} Your
              transcript is safe either way.
            </AlertDescription>
          </Alert>
        )}

        <InterviewEnding interview={interview} />

        <div className="flex flex-wrap items-center justify-center gap-2">
          {showRetry && (
            <Button onClick={() => retry.mutate()} disabled={retry.isPending}>
              {retry.isPending ? "Starting…" : `${action} evaluation`}
            </Button>
          )}
          <LinkButton variant="ghost" to="/">
            <HomeIcon />
            Back to Home
          </LinkButton>
          <LinkButton variant="ghost" to="/interviews">
            <HistoryIcon />
            History
          </LinkButton>
        </div>
      </div>
    </PostCallFrame>
  )
}
