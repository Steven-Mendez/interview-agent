import {
  AlertCircleIcon,
  CheckCircle2Icon,
  CircleDashedIcon,
  CircleDotIcon,
  ClockIcon,
  LoaderIcon,
  PhoneOffIcon,
  PlayCircleIcon,
} from "lucide-react"

import { scoreTone } from "@/components/evaluation-feedback"
import { Badge } from "@/components/ui/badge"
import type { InterviewStatus, InterviewSummary } from "@/lib/api"
import { cn } from "@/lib/utils"

type Tone = "default" | "secondary" | "success" | "warning" | "destructive"

export interface StatusMeta {
  label: string
  tone: Tone
  icon: React.ComponentType<{ className?: string }>
}

/** How each status reads. Shared by Home, History and Results so a status
 *  looks the same wherever it appears. */
export const STATUS_META: Record<InterviewStatus, StatusMeta> = {
  created: { label: "Planning…", tone: "secondary", icon: LoaderIcon },
  planned: { label: "Ready to start", tone: "default", icon: PlayCircleIcon },
  interviewing: { label: "In progress", tone: "default", icon: CircleDotIcon },
  closing: { label: "Closing", tone: "secondary", icon: ClockIcon },
  completed: { label: "Ended", tone: "secondary", icon: CircleDashedIcon },
  evaluating: { label: "Evaluating…", tone: "secondary", icon: LoaderIcon },
  evaluated: { label: "Evaluated", tone: "success", icon: CheckCircle2Icon },
  evaluation_failed: {
    label: "Evaluation failed",
    tone: "destructive",
    icon: AlertCircleIcon,
  },
  error: { label: "Failed", tone: "destructive", icon: AlertCircleIcon },
}

// The one status the row alone does not settle: `interviewing` past its
// reconnect window is a worker that died mid-run, not an interview going on.
const INTERRUPTED_META: StatusMeta = {
  label: "Interrupted",
  tone: "warning",
  icon: PhoneOffIcon,
}

export function statusMeta(row: {
  status: InterviewStatus
  can_start: boolean
}): StatusMeta {
  return row.status === "interviewing" && !row.can_start
    ? INTERRUPTED_META
    : STATUS_META[row.status]
}

export function InterviewStatusChip({
  row,
  className,
}: {
  row: { status: InterviewStatus; can_start: boolean }
  className?: string
}) {
  const meta = statusMeta(row)
  const Icon = meta.icon
  return (
    <Badge variant={meta.tone} className={className}>
      <Icon aria-hidden />
      {meta.label}
    </Badge>
  )
}

/** A list row's verdict: the score with its traffic light and the verdict in
 *  words, or why there is no score. Nothing when not evaluated. */
export function EvaluationSummary({
  evaluation,
  className,
}: {
  evaluation: InterviewSummary["evaluation"]
  className?: string
}) {
  if (!evaluation) return null
  const hasVerdict = evaluation.score != null && evaluation.hired !== null
  if (!hasVerdict) {
    return (
      <span className={cn("text-xs text-muted-foreground", className)}>
        {evaluation.evaluation_status === "insufficient"
          ? "Insufficient evidence"
          : "Partial assessment"}
      </span>
    )
  }
  const tone = scoreTone(evaluation.score!, evaluation.hired!)
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <span
        className={cn(
          "inline-flex h-7 min-w-10 items-center justify-center rounded-md px-1.5 font-heading text-sm font-medium tabular-nums",
          tone === "success" &&
            "bg-success-container text-on-success-container",
          tone === "warning" &&
            "bg-warning-container text-on-warning-container",
          tone === "destructive" &&
            "bg-destructive-container text-on-destructive-container"
        )}
      >
        {evaluation.score}
      </span>
      <span className="text-xs text-muted-foreground">
        {evaluation.hired ? "Hired" : "Not hired"}
      </span>
    </span>
  )
}
