import * as React from "react"

import { API_BASE } from "@/lib/api-base"

// The hosted API scales to zero: after a while idle, the first request waits
// for a new instance to boot, which can take several seconds. The page says
// why nothing happens yet, but a slow request alone cannot tell a boot from
// a slow route (the planner takes tens of seconds on a warm API), so it only
// raises the question: a request the API has not answered within
// WAKING_AFTER_MS sends a liveness probe, GET /healthz, and the API counts
// as waking while that probe goes unanswered. Its answer, any status, or its
// failure ends it; the watched request itself never does, so one aborted or
// failed while the API boots leaves the notice up.
// Watched: the API client's request() and the sign-in mode's GET
// /auth/config; a Neon build sends neither before sign-in, so the shell
// wakes the API on load (wakeApi).
// Nothing retries here: TanStack Query already retries reads.

const WAKING_AFTER_MS = 4_000
// A warm API answers the probe well within this, so a slow route on one
// shows nothing; without it the notice would flash, and the status region
// announce it, on every slow request.
const PROBE_GRACE_MS = 1_000

let waking = false
// One probe at a time: requests that go slow together share it.
let probing = false
let woken = false
const listeners = new Set<() => void>()

function setWaking(value: boolean) {
  if (waking === value) return
  waking = value
  for (const listener of listeners) listener()
}

/** Asks the API whether it is up, waking while it waits longer than
 *  `showAfter`. No credentials, and never a cached answer: only the API's
 *  own reply proves it is up. */
async function probe(showAfter: number) {
  if (probing) return
  probing = true
  const shown = setTimeout(() => setWaking(true), showAfter)
  try {
    await fetch(`${API_BASE}/healthz`, {
      credentials: "omit",
      cache: "no-store",
    })
  } catch {
    // Unreachable is not booting: nothing to wait for.
  } finally {
    probing = false
    clearTimeout(shown)
    setWaking(false)
  }
}

/** Watches one fetch to the API: one unanswered too long sends a probe. */
export async function watchApiFetch(fetching: Promise<Response>) {
  const timer = setTimeout(() => void probe(PROBE_GRACE_MS), WAKING_AFTER_MS)
  try {
    return await fetching
  } finally {
    clearTimeout(timer)
  }
}

/** Starts the API booting while the visitor signs in, once per page load
 *  (browser only). The probe itself is the watched request here: waking
 *  after WAKING_AFTER_MS without an answer, like any other. */
export function wakeApi(): void {
  if (woken || typeof window === "undefined") return
  woken = true
  void probe(WAKING_AFTER_MS)
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

/** Whether the API has left a liveness probe unanswered: likely booting. */
export function useApiWaking(): boolean {
  return React.useSyncExternalStore(subscribe, isApiWaking, () => false)
}
