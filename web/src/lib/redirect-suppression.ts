// Holds against leaving the page for the sign-in one: an interview in
// progress must not be navigated away from. Its own module so that api.ts
// (a refused session) and local-auth.ts (an expired one) can both honour
// them without importing each other.

// Holders of suppressUnauthorizedRedirect() that have not released it yet.
let redirectSuppressions = 0

/** Keeps a 401 from leaving the page until the returned release runs: an
 *  interview in progress must not be navigated away from. A 401 meanwhile
 *  only throws. Counted, so each holder releases just its own hold. */
export function suppressUnauthorizedRedirect(): () => void {
  redirectSuppressions += 1
  let released = false
  return () => {
    if (released) return
    released = true
    redirectSuppressions -= 1
  }
}

/** Whether something holds the sign-in redirect off right now. */
export function redirectsSuppressed(): boolean {
  return redirectSuppressions > 0
}
