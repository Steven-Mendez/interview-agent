import { CheckIcon, SkipForwardIcon } from "lucide-react"

import type { Milestone } from "@/lib/api"
import { cn } from "@/lib/utils"

export function topicState(milestone: Milestone) {
  return milestone.lifecycle ?? (milestone.completed ? "closed" : "pending")
}

export function isSettled(milestone: Milestone) {
  return ["closed", "skipped"].includes(topicState(milestone))
}

const TOPIC_LABELS = {
  pending: "Pending",
  active: "In progress",
  closed: "Closed",
  skipped: "Skipped",
}

export function topicLabel(milestone: Milestone) {
  return TOPIC_LABELS[topicState(milestone)]
}

/** The leading marker of a topic row: its number while pending, a live dot
 *  while being asked, a check once closed, a skip glyph when skipped. The
 *  label next to it always says the same in words. */
export function TopicMarker({
  milestone,
  index,
  className,
}: {
  milestone: Milestone
  index: number
  className?: string
}) {
  const state = topicState(milestone)
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-6 shrink-0 items-center justify-center rounded-full font-heading text-xs font-medium tabular-nums",
        state === "pending" && "border border-border text-muted-foreground",
        state === "active" && "bg-primary text-primary-foreground",
        state === "closed" && "bg-success-container text-on-success-container",
        state === "skipped" && "bg-muted text-muted-foreground",
        className
      )}
    >
      {state === "closed" ? (
        <CheckIcon className="size-3.5" strokeWidth={3} />
      ) : state === "skipped" ? (
        <SkipForwardIcon className="size-3.5" />
      ) : (
        index + 1
      )}
    </span>
  )
}
