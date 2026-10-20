import { Link } from "@tanstack/react-router"

import { MascotHead } from "@/components/mascot"
import { cn } from "@/lib/utils"

/** The product mark: the Interviewer Agent's head — the same character that
 *  conducts the interview, simplified to hold up at favicon sizes. */
export function BrandMark({
  className,
  outlined,
}: {
  className?: string
  outlined?: boolean
}) {
  return <MascotHead className={cn("size-8", className)} outlined={outlined} />
}

/** Mark + product name: the horizontal lockup. Light or dark follows the
 *  surface's tokens; `compact` drops the name where space is short. */
export function ProductLogo({
  compact = false,
  className,
}: {
  compact?: boolean | "mobile"
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-2 text-foreground",
        className
      )}
    >
      <BrandMark className="size-9" />
      {compact !== true && (
        <span
          className={cn(
            "font-heading text-[1.375rem] leading-7 font-normal tracking-[-0.01em]",
            compact === "mobile" && "hidden sm:inline"
          )}
        >
          Interviewer Agent
        </span>
      )}
    </span>
  )
}

/** The lockup as the link home. */
export function BrandLink({
  className,
  compact = false,
}: {
  className?: string
  compact?: boolean | "mobile"
}) {
  return (
    <Link
      to="/"
      className={cn("shrink-0 rounded-full pr-2 outline-offset-4", className)}
      aria-label="Interviewer Agent — Home"
    >
      <ProductLogo compact={compact} />
    </Link>
  )
}
