import * as React from "react"
import { ChevronDownIcon } from "lucide-react"

import { cn } from "@/lib/utils"

/** A native <details> row: a quiet surface whose summary opens it. Native so
 *  it works without script and keeps its own keyboard behavior. */
function Disclosure({
  summary,
  description,
  className,
  bodyClassName,
  children,
  ...props
}: Omit<React.ComponentProps<"details">, "summary"> & {
  summary: string
  description?: React.ReactNode
  bodyClassName?: string
}) {
  return (
    <details
      data-slot="disclosure"
      className={cn(
        "group/disclosure overflow-hidden rounded-xl border bg-card",
        className
      )}
      {...props}
    >
      <summary className="text-label flex min-h-14 cursor-pointer list-none items-center gap-3 px-4 py-3 transition-colors hover:bg-foreground/[0.04] [&::-webkit-details-marker]:hidden">
        {summary}
        <ChevronDownIcon
          aria-hidden
          className="ml-auto size-5 shrink-0 text-muted-foreground transition-transform duration-200 group-open/disclosure:rotate-180"
        />
      </summary>
      {description && (
        <p className="-mt-2 px-4 pb-2 text-xs text-muted-foreground">
          {description}
        </p>
      )}
      <div className={cn("px-4 pb-4 text-sm", bodyClassName)}>{children}</div>
    </details>
  )
}

export { Disclosure }
