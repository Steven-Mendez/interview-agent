import { cn } from "@/lib/utils"

/** Small circular progress for local actions. Decorative by default — the
 *  text next to it says what is loading. */
function Spinner({ className, ...props }: React.ComponentProps<"svg">) {
  return (
    <svg
      data-slot="spinner"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
      className={cn("size-4 shrink-0 animate-spin text-primary", className)}
      {...props}
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeOpacity="0.2"
        strokeWidth="2.5"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** Indeterminate linear progress for long, persistent processing. */
function LinearProgress({
  className,
  label,
}: {
  className?: string
  label: string
}) {
  return (
    <div
      role="progressbar"
      aria-label={label}
      className={cn(
        "relative h-1 w-full overflow-hidden rounded-full bg-primary/20",
        className
      )}
    >
      <div className="progress-indeterminate absolute inset-y-0 left-0 w-full rounded-full bg-primary" />
    </div>
  )
}

export { Spinner, LinearProgress }
