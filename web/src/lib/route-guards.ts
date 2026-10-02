import { redirect } from "@tanstack/react-router"

import { authClient, safeRedirectPath } from "@/lib/auth"

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
 * The session comes from the auth client's in-memory cache after the first
 * load, so moving between private pages costs no round trip. Local mode has
 * nothing to check, and neither has the prerender, which has no session. */
export async function requireSession(location: { href: string }) {
  if (!authClient || typeof window === "undefined") return
  const { data } = await authClient.getSession()
  if (!data) throw signInRedirect(location.href)
}
