import { redirect } from "@tanstack/react-router"

import { authClient, resolveAuthMode, safeRedirectPath } from "@/lib/auth"
import { getLocalAccessToken } from "@/lib/local-auth"

/** The sign-in page, coming back to `from` once signed in. */
export function signInRedirect(from: string) {
  return redirect({
    to: "/auth/$pathname",
    params: { pathname: "sign-in" },
    search: { redirectTo: safeRedirectPath(from) },
  })
}

/** `beforeLoad` of a private route: without a session, off to sign in.
 *
 * Neon's session comes from the auth client's in-memory cache after the
 * first load, so moving between private pages costs no round trip; a local
 * account's is in storage. No sign-in at all (mode none) has nothing to
 * check, and neither has the prerender, which has no session. An API that
 * cannot say its mode fails the page, whose Try again asks it again. */
export async function requireSession(location: { href: string }) {
  if (typeof window === "undefined") return
  const mode = await resolveAuthMode()
  if (mode === "none") return
  if (authClient) {
    const { data } = await authClient.getSession()
    if (!data) throw signInRedirect(location.href)
    return
  }
  // Also the API asking for Neon Auth in a build without it: the sign-in
  // page explains.
  if (getLocalAccessToken() === null) throw signInRedirect(location.href)
}
