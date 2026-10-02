import * as React from "react"
import { BetterAuthReactAdapter } from "@neondatabase/neon-js/auth/react/adapters"

import { API_BASE } from "@/lib/api-base"
import {
  getLocalAccessToken,
  getLocalSession,
  isLive,
  signOutLocal,
  subscribeLocalSession,
} from "@/lib/local-auth"

// How users sign in. A build with VITE_NEON_AUTH_URL signs in through Neon
// Auth, and nothing else. Without it the API decides (GET /auth/config),
// asked once in the browser:
//   none:  AUTH_MODE=local without accounts — no sign-in, every call is the
//          local developer's;
//   local: the dev login of LOCAL_ACCOUNTS (lib/local-auth);
//   neon:  the API wants Neon Auth this build cannot do — the sign-in page
//          says so.
//
// The Neon client does no I/O when it is created — the session is fetched
// on the first getSession() or useSession() subscriber, and never on the
// server — so creating it here is safe while the shell is prerendered.

export type AuthMode = "none" | "local" | "neon"

const AUTH_URL = import.meta.env.VITE_NEON_AUTH_URL

/** Whether this build signs in through Neon Auth (VITE_NEON_AUTH_URL). */
export const neonAuthConfigured = Boolean(AUTH_URL)

// What createAuthClient(url, { adapter: BetterAuthReactAdapter() }) does,
// minus the step that throws getJWTToken away: build the adapter for the URL
// and keep it, so the token and the UI read one session cache. (The package
// root would also bundle the Supabase-compatible adapter, unused here.)
const adapter = AUTH_URL ? BetterAuthReactAdapter()(AUTH_URL) : null

/** The Better Auth client (React hooks included); null without Neon Auth. */
export const authClient = adapter?.getBetterAuthInstance() ?? null

// ---- Mode ---------------------------------------------------------------------

const AUTH_MODES: readonly AuthMode[] = ["none", "local", "neon"]

// Known for good in a Neon build. Otherwise only an answer of the API's is
// kept: a failure is asked again (Vite started before the API), and so is
// a mode marked stale by resetAuthMode() — the last answer still shows
// until the new one arrives.
let currentMode: AuthMode | undefined = AUTH_URL ? "neon" : undefined
let modeStale = false
let pendingMode: Promise<AuthMode> | null = null
// Why the last ask failed, until one succeeds: a page with no mode yet
// shows it rather than loading forever.
let modeError: Error | null = null
const modeListeners = new Set<() => void>()

function setModeError(error: Error | null) {
  if (modeError === error) return
  modeError = error
  for (const listener of modeListeners) listener()
}

function subscribeMode(listener: () => void) {
  modeListeners.add(listener)
  return () => {
    modeListeners.delete(listener)
  }
}

async function fetchAuthMode(): Promise<AuthMode> {
  let mode: unknown
  try {
    const res = await fetch(`${API_BASE}/auth/config`)
    if (!res.ok) throw new Error()
    mode = ((await res.json()) as { mode?: unknown } | null)?.mode
  } catch {
    throw new Error("The API could not be reached.")
  }
  if (!AUTH_MODES.includes(mode as AuthMode)) {
    throw new Error("The API did not say how to sign in.")
  }
  return mode as AuthMode
}

/** How users sign in. Asks the API the first time (browser only: on the
 *  server it never settles, so nothing should await it there); rejects
 *  when the API cannot say, and the next call asks again. */
export function resolveAuthMode(): Promise<AuthMode> {
  if (currentMode !== undefined && !modeStale) {
    return Promise.resolve(currentMode)
  }
  if (typeof window === "undefined") return new Promise(() => {})
  pendingMode ??= fetchAuthMode()
    .then(
      (mode) => {
        modeStale = false
        if (currentMode !== mode) {
          currentMode = mode
          for (const listener of modeListeners) listener()
        }
        setModeError(null)
        return mode
      },
      (error: unknown) => {
        setModeError(error as Error)
        throw error
      }
    )
    .finally(() => {
      pendingMode = null
    })
  return pendingMode
}

/** Asks the API again on the next resolveAuthMode(): it may have restarted
 *  in another mode since. Nothing to ask in a Neon build. */
export function resetAuthMode(): void {
  if (!AUTH_URL) modeStale = true
}

// How long a page without a mode waits before asking a silent API again.
const MODE_RETRY_MS = 5_000

/** The sign-in mode, or undefined until it is known — the whole prerender,
 *  outside a Neon build. While unknown it keeps asking. */
export function useAuthMode(): AuthMode | undefined {
  const mode = React.useSyncExternalStore(
    subscribeMode,
    () => currentMode,
    () => (AUTH_URL ? "neon" : undefined)
  )
  React.useEffect(() => {
    if (mode !== undefined) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const attempt = () => {
      resolveAuthMode().catch(() => {
        if (!cancelled) timer = setTimeout(attempt, MODE_RETRY_MS)
      })
    }
    attempt()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [mode])
  return mode
}

/** Why the API could not say how to sign in, while it cannot; null once
 *  it has (and always on the server and in a Neon build). */
export function useAuthModeError(): Error | null {
  return React.useSyncExternalStore(
    subscribeMode,
    () => modeError,
    () => null
  )
}

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

/** The token the API verifies, or null when signed out or without sign-in.
 *
 * Outside a Neon build, the local account's token while it lasts (the API
 * ignores it when it has no sign-in). With Neon Auth, the JWT from
 * getJWTToken, never the session's own opaque token. The SDK keeps the
 * session (and so the JWT) in memory and drops it on sign-out: calling this
 * before every request costs no round trip, except once a minute before the
 * JWT expires, when concurrent callers share one fetch of a fresh session. */
export async function getAccessToken(): Promise<string | null> {
  if (!adapter) return getLocalAccessToken()
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
 *  renewed elsewhere, a clock ahead of the API's). Null when signed out,
 *  for a local account (nothing to renew), or when the auth server cannot
 *  be reached. */
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
 *  auth server refuses, the session comes back. A local account just drops
 *  its token. */
export async function signOut(userId: string): Promise<void> {
  if (!authClient) {
    signOutLocal()
    return
  }
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
  /** False once the session is known. */
  isPending: boolean
  /** Null when signed out — and always without sign-in (mode none). */
  user: AuthUser | null
}

/** A local account's token after the API refused it: no longer a session
 *  (a password changed, the account removed). Neon's are its own SDK's. */
export function forgetRefusedToken(token: string): void {
  if (!authClient) signOutLocal(token)
}

function useNoSession() {
  return { data: null, isPending: false }
}

const useClientSession = authClient ? authClient.useSession : useNoSession

function useNeonAuth(): AuthState {
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

function useLocalAuth(): AuthState {
  const mode = useAuthMode()
  const session = React.useSyncExternalStore(
    subscribeLocalSession,
    getLocalSession,
    () => null
  )
  const live = mode === "local" && session !== null && isLive(session)
  const id = live ? session.user.id : null
  const name = live ? session.user.name : null
  const user = React.useMemo<AuthUser | null>(
    () => (id === null ? null : { id, name, email: null, image: null }),
    [id, name]
  )
  return { isPending: mode === undefined, user }
}

/** The current session, kept in sync across tabs. During the prerender it
 *  is pending: nothing session-dependent should render before mount. */
export const useAuth: () => AuthState = authClient ? useNeonAuth : useLocalAuth

/** Whether calls to the API can go out: signed in, or no sign-in at all. */
export function useCanCallApi(): boolean {
  const mode = useAuthMode()
  const { user } = useAuth()
  return mode === "none" || user !== null
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
