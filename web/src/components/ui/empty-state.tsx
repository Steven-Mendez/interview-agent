import * as React from "react"

import { cn } from "@/lib/utils"

/** Calm empty, error and completion states: the character or an icon in a
 *  soft circle, a
 *  short title, one or two lines of copy, and at most one or two actions. */
function EmptyState({
  icon,
  illustration,
  title,
  description,
  actions,
  tone = "neutral",
  className,
  children,
}: {
  icon?: React.ReactNode
  /** A larger picture in place of the icon — the Interviewer Agent in the
   *  state that fits the moment. */
  illustration?: React.ReactNode
  title: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  tone?: "neutral" | "primary" | "success" | "warning" | "error"
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center gap-4 px-6 py-12 text-center",
        className
      )}
    >
      {illustration}
      {!illustration && icon && (
        <span
          aria-hidden
          className={cn(
            "flex size-20 items-center justify-center rounded-full [&_svg]:size-9",
            tone === "neutral" && "bg-muted text-muted-foreground",
            tone === "primary" && "bg-primary-container text-primary",
            tone === "success" && "bg-success-container text-success",
            tone === "warning" && "bg-warning-container text-warning",
            tone === "error" && "bg-destructive-container text-destructive"
          )}
        >
          {icon}
        </span>
      )}
      <div className="flex max-w-md flex-col gap-2">
        <h2 className="text-title-lg text-foreground">{title}</h2>
        {description && (
          <div className="text-sm leading-relaxed text-muted-foreground">
            {description}
          </div>
        )}
      </div>
      {children}
      {actions && (
        <div className="flex flex-wrap items-center justify-center gap-2 pt-2">
          {actions}
        </div>
      )}
    </div>
  )
}

export { EmptyState }
