import * as React from "react"

// The hosted API scales to zero: after a while idle, the first request waits
// for a new instance to boot, which can take several seconds. A request the
// API has not answered within WAKING_AFTER_MS marks it as waking, so the page
// can say why nothing happens yet; the next answer, or failure, clears it.
// Watched: the API client's request() and the sign-in mode's GET
// /auth/config, a visit's usual first request.
// Nothing retries here: TanStack Query already retries reads.

const WAKING_AFTER_MS = 4_000
// A slow answer from an API that answered moments ago is a slow route (the
// planner takes tens of seconds), not a boot: only a request sent after this
// long without any answer can find the API asleep.
const WARM_FOR_MS = 5 * 60_000

let waking = false
let lastAnswerAt = -Infinity
const listeners = new Set<() => void>()

function setWaking(value: boolean) {
  if (waking === value) return
  waking = value
  for (const listener of listeners) listener()
}

/** Watches one fetch to the API: waking while it goes unanswered too long. */
export async function watchApiFetch(fetching: Promise<Response>) {
  const sentAt = Date.now()
  const timer =
    sentAt - lastAnswerAt < WARM_FOR_MS
      ? undefined
      : setTimeout(() => {
          // Another request may have been answered meanwhile: the API is up.
          if (lastAnswerAt < sentAt) setWaking(true)
        }, WAKING_AFTER_MS)
  try {
    const res = await fetching
    // Any status counts: an error page still means the API is up.
    lastAnswerAt = Date.now()
    return res
  } finally {
    clearTimeout(timer)
    setWaking(false)
  }
}

export function isApiWaking(): boolean {
  return waking
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** Whether a request has waited long enough that the API is likely booting. */
export function useApiWaking(): boolean {
  return React.useSyncExternalStore(subscribe, isApiWaking, () => false)
}
