import * as React from "react"
import { BetterAuthReactAdapter } from "@neondatabase/neon-js/auth/react/adapters"

// Sign-in through Neon Auth. Without VITE_NEON_AUTH_URL (local development
// against an API running AUTH_MODE=local) there is no client at all: no
// session, no token, and every caller treats the app as signed in.
//
// The client does no I/O when it is created — the session is fetched on
// the first getSession() or useSession() subscriber, and never on the server
// — so creating it here is safe while the shell is prerendered.

const AUTH_URL = import.meta.env.VITE_NEON_AUTH_URL

export const authEnabled = Boolean(AUTH_URL)

// What createAuthClient(url, { adapter: BetterAuthReactAdapter() }) does,
// minus the step that throws getJWTToken away: build the adapter for the URL
// and keep it, so the token and the UI read one session cache. (The package
// root would also bundle the Supabase-compatible adapter, unused here.)
const adapter = AUTH_URL ? BetterAuthReactAdapter()(AUTH_URL) : null

/** The Better Auth client (React hooks included); null in local mode. */
export const authClient = adapter?.getBetterAuthInstance() ?? null

// The SDK hands out its cached JWT until 10 s before `exp`; the API allows
// only 30 s of clock skew, so a slow upload or a slow clock could arrive with
// it expired. A minute before, ask for a fresh one instead.
const FRESH_FOR_MS = 60_000

/** The JWT's `exp` in milliseconds, or null when it cannot be read. */
export function jwtExpiresAt(token: string): number | null {
  try {
    const payload = token.split(".")[1]
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"))
    const exp: unknown = (JSON.parse(json) as { exp?: unknown }).exp
    return typeof exp === "number" ? exp * 1000 : null
  } catch {
    return null
  }
}

let refreshing: Promise<string | null> | null = null

/** A new session from the auth server, past the SDK's cache (its
 *  X-Force-Fetch header); the answer replaces what the cache held. Throws
 *  when the server cannot say; null when it says signed out. */
async function fetchFreshToken(): Promise<string | null> {
  if (!authClient) return null
  const { data, error } = await authClient.getSession({
    fetchOptions: { headers: { "X-Force-Fetch": "true" } },
  })
  if (error) throw new Error("Session refresh failed")
  return data?.session.token ?? null
}

/** The JWT the API verifies, or null when signed out or in local mode.
 *
 * Never the session's own token — Neon Auth swaps it for the JWT, but only
 * getJWTToken says so. The SDK keeps the session (and so the JWT) in memory
 * and drops it on sign-out: calling this before every request costs no
 * round trip, except once a minute before the JWT expires, when concurrent
 * callers share one fetch of a fresh session. */
export async function getAccessToken(): Promise<string | null> {
  if (!adapter) return null
  let token: string | null = null
  try {
    // false: no anonymous token when signed out — the API has no use for it.
    token = await adapter.getJWTToken(false)
    const expiresAt = token === null ? null : jwtExpiresAt(token)
    if (expiresAt === null || expiresAt - Date.now() > FRESH_FOR_MS) {
      return token
    }
    refreshing ??= fetchFreshToken().finally(() => {
      refreshing = null
    })
    return await refreshing
  } catch {
    // Unreachable auth server: the token in hand while it lasts, else the
    // request goes out without one and the API's 401 sends the user to
    // sign in.
    return token
  }
}

/** A JWT fresh from the auth server, for one the API refused (a session
 *  renewed elsewhere, a clock ahead of the API's). Null when signed out, in
 *  local mode, or when the auth server cannot be reached. */
export async function refreshAccessToken(): Promise<string | null> {
  if (!adapter) return null
  refreshing ??= fetchFreshToken().finally(() => {
    refreshing = null
  })
  try {
    return await refreshing
  } catch {
    return null
  }
}

// ---- Sign-out -----------------------------------------------------------------

// The account being signed out. Better Auth keeps reporting its session
// until the refetch that follows the sign-out answers; until then it counts
// as signed out, so nothing asks the API on its behalf without a token.
let signingOutId: string | null = null
const signingOutListeners = new Set<() => void>()

function setSigningOutId(id: string | null) {
  if (signingOutId === id) return
  signingOutId = id
  for (const listener of signingOutListeners) listener()
}

function subscribeSigningOut(listener: () => void) {
  signingOutListeners.add(listener)
  return () => {
    signingOutListeners.delete(listener)
  }
}

/** Signs `userId` out. Every useAuth() reports signed out at once; if the
 *  auth server refuses, the session comes back. */
export async function signOut(userId: string): Promise<void> {
  if (!authClient) return
  setSigningOutId(userId)
  try {
    const { error } = await authClient.signOut()
    if (error) setSigningOutId(null)
  } catch {
    setSigningOutId(null)
  }
}

/** The signed-in user as the UI needs it. */
export interface AuthUser {
  id: string
  name: string | null
  email: string | null
  image: string | null
}

export interface AuthState {
  /** False once the session is known (immediately in local mode). */
  isPending: boolean
  /** Null when signed out — and always in local mode. */
  user: AuthUser | null
}

function useNoSession() {
  return { data: null, isPending: false }
}

const useClientSession = authClient ? authClient.useSession : useNoSession

/** The current session, kept in sync across tabs. During the prerender it
 *  is pending: nothing session-dependent should render before mount. */
export function useAuth(): AuthState {
  const { data, isPending } = useClientSession()
  const signingOut = React.useSyncExternalStore(
    subscribeSigningOut,
    () => signingOutId,
    () => null
  )
  const settledSignedOut = !isPending && !data
  React.useEffect(() => {
    // The sign-out has reached the session: nothing left to hide.
    if (settledSignedOut) setSigningOutId(null)
  }, [settledSignedOut])
  const user = data?.user.id === signingOut ? undefined : data?.user
  return {
    isPending,
    user: user
      ? {
          id: user.id,
          name: user.name || null,
          email: user.email || null,
          image: user.image ?? null,
        }
      : null,
  }
}

/** Whether calls to the API can go out: signed in, or no sign-in at all. */
export function useCanCallApi(): boolean {
  const { user } = useAuth()
  return !authEnabled || user !== null
}

/** A path on this site, or undefined: no scheme, no protocol-relative
 *  `//host`, no backslash or whitespace that a browser would read as one. */
export function sameSitePath(target: unknown): string | undefined {
  if (typeof target !== "string") return undefined
  if (!target.startsWith("/") || target.startsWith("//")) return undefined
  if (/[\\\s]/.test(target)) return undefined
  const base = "https://app.invalid"
  let url: URL
  try {
    url = new URL(target, base)
  } catch {
    return undefined
  }
  if (url.origin !== base || url.pathname.startsWith("//")) return undefined
  return `${url.pathname}${url.search}${url.hash}`
}

/** A post-login destination, or undefined: a path on this site, and not
 *  under /auth/ (signing in must not land on signing out). */
export function safeRedirectPath(target: unknown): string | undefined {
  const path = sameSitePath(target)
  if (path === undefined || /^\/auth(?:[/?#]|$)/.test(path)) return undefined
  return path
}
