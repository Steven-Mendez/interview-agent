import { useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

/** Display only. Interview deadlines remain authoritative in PostgreSQL. */
export function InterviewTimer({
  elapsedSeconds,
  className,
}: {
  elapsedSeconds?: number | null
  className?: string
}) {
  const known =
    typeof elapsedSeconds === "number" &&
    Number.isFinite(elapsedSeconds) &&
    elapsedSeconds >= 0
  const sample = known ? elapsedSeconds : null
  const anchor = useRef({ sample, at: performance.now() })
  const [now, setNow] = useState(() => performance.now())
  if (anchor.current.sample !== sample) {
    anchor.current = { sample, at: performance.now() }
  }
  useEffect(() => {
    if (sample === null) return
    const id = setInterval(() => setNow(performance.now()), 1000)
    return () => clearInterval(id)
  }, [sample])
  const elapsed =
    sample === null
      ? null
      : Math.floor(sample + Math.max(0, now - anchor.current.at) / 1000)
  const display =
    elapsed === null
      ? "--:--"
      : `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`
  return (
    <span
      aria-label={
        elapsed === null
          ? "Elapsed interview time unavailable"
          : "Elapsed interview time"
      }
      className={cn(
        "flex items-center gap-2 font-heading text-sm font-medium tabular-nums",
        className
      )}
    >
      <span
        aria-hidden
        className="size-2 animate-pulse rounded-full bg-end-call"
      />
      {display}
    </span>
  )
}
