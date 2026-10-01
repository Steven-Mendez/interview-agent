import { useEffect, useRef, useState } from "react"

/** Display only. Interview deadlines remain authoritative in PostgreSQL. */
export function InterviewTimer({
  elapsedSeconds,
}: {
  elapsedSeconds?: number | null
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
      className="ml-1 flex items-center gap-1.5 text-xs font-normal text-muted-foreground tabular-nums"
    >
      <span className="size-1.5 animate-pulse rounded-full bg-primary" />
      {display}
    </span>
  )
}
