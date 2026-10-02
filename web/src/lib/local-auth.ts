import { API_BASE } from "@/lib/api-base"
import { redirectsSuppressed } from "@/lib/redirect-suppression"

// The dev login of an API running AUTH_MODE=local with LOCAL_ACCOUNTS: a
// username and password traded for the API's own token, kept in
// localStorage. Acceptable only because that mode exists for development —
// the API refuses to start in it against a remote database or in production.
//
// Nothing here runs at import: storage is read on first use, and only in the
// browser (the prerender sees no session).

const STORAGE_KEY = "interview-agent.local-session"

export interface LocalSession {
  token: string
  /** Milliseconds since the epoch. */
  expiresAt: number
  user: { id: string; name: string | null }
}

/** A sign-in the API refused, or one that could not reach it; the message
 *  is fit to show. */
export class LocalSignInError extends Error {
  /** The API has no local accounts (any more): ask it which sign-in it runs. */
  readonly signInOff: boolean
  constructor(message: string, signInOff = false) {
    super(message)
    this.name = "LocalSignInError"
    this.signInOff = signInOff
  }
}

export const INVALID_CREDENTIALS = "Invalid username or password"
export const SIGN_IN_UNAVAILABLE = "Could not sign in. Is the API running?"
export const SIGN_IN_OFF = "The API no longer uses local accounts."
export const STORAGE_BLOCKED =
  "Could not keep the session: browser storage is blocked."

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    // No window (the prerender) or storage blocked: no session.
    return null
  }
}

function parse(raw: string | null): LocalSession | null {
  if (raw === null) return null
  try {
    const value = JSON.parse(raw) as Partial<LocalSession> | null
    const user = value?.user
    if (
      typeof value?.token !== "string" ||
      typeof value.expiresAt !== "number" ||
      typeof user?.id !== "string" ||
      (user.name !== null && typeof user.name !== "string")
    ) {
      return null
    }
    return {
      token: value.token,
      expiresAt: value.expiresAt,
      user: { id: user.id, name: user.name },
    }
  } catch {
    return null
  }
}

// ---- Store (useSyncExternalStore) ----------------------------------------------

const listeners = new Set<() => void>()

// The snapshot is parsed again only when the stored string changes, so
// React sees the same object between changes.
let lastRaw: string | null = null
let lastSession: LocalSession | null = null

/** The stored session, expired or not; the same object until it changes. */
export function getLocalSession(): LocalSession | null {
  const raw = readStored()
  if (raw !== lastRaw) {
    lastRaw = raw
    lastSession = parse(raw)
  }
  return lastSession
}

function notify() {
  for (const listener of listeners) listener()
}

function onStorage(event: StorageEvent) {
  // Another tab signed in or out (null: its storage was cleared).
  if (event.key === STORAGE_KEY || event.key === null) notify()
}

export function subscribeLocalSession(listener: () => void): () => void {
  if (listeners.size === 0) window.addEventListener("storage", onStorage)
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) window.removeEventListener("storage", onStorage)
  }
}

/** Writes the session (null: removes it); false when storage refused. */
function store(session: LocalSession | null): boolean {
  let stored = true
  try {
    if (session) {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
    } else {
      window.localStorage.removeItem(STORAGE_KEY)
    }
  } catch {
    // Storage blocked: there is no session to keep, or none left to clear.
    stored = false
  }
  notify()
  return stored
}

// ---- Sign-in and the token -------------------------------------------------------

function isSignInResponse(body: unknown): body is {
  token: string
  expires_at: string
  user: { id: string; name: string | null }
} {
  if (!body || typeof body !== "object") return false
  const { token, expires_at, user } = body as Record<string, unknown>
  if (typeof token !== "string" || typeof expires_at !== "string") return false
  if (!user || typeof user !== "object") return false
  const { id, name } = user as Record<string, unknown>
  return typeof id === "string" && (name === null || typeof name === "string")
}

/** Signs in with a local account and keeps its token. Throws a
 *  LocalSignInError with the message to show.
 *
 * A plain fetch rather than the API client's: no previous token goes with
 * it, and its 401 is a wrong password, not a session to end. */
export async function signIn(
  username: string,
  password: string
): Promise<LocalSession> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}/auth/local/sign-in`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
  } catch {
    throw new LocalSignInError(SIGN_IN_UNAVAILABLE)
  }
  if (res.status === 401 || res.status === 422) {
    throw new LocalSignInError(INVALID_CREDENTIALS)
  }
  // The dev login is off: the API restarted without LOCAL_ACCOUNTS.
  if (res.status === 404) throw new LocalSignInError(SIGN_IN_OFF, true)
  if (!res.ok) throw new LocalSignInError(SIGN_IN_UNAVAILABLE)
  let body: unknown
  try {
    body = await res.json()
  } catch {
    throw new LocalSignInError(SIGN_IN_UNAVAILABLE)
  }
  const expiresAt = isSignInResponse(body) ? Date.parse(body.expires_at) : NaN
  if (!isSignInResponse(body) || !Number.isFinite(expiresAt)) {
    throw new LocalSignInError(SIGN_IN_UNAVAILABLE)
  }
  const session: LocalSession = {
    token: body.token,
    expiresAt,
    user: { id: body.user.id, name: body.user.name },
  }
  // Every reader goes back to storage: a session it cannot hold is none.
  if (!store(session)) throw new LocalSignInError(STORAGE_BLOCKED)
  return session
}

/** Whether the session's token has run out. */
export function isExpired(session: LocalSession): boolean {
  return session.expiresAt <= Date.now()
}

/** Whether the session still counts as signed in: until its token runs
 *  out — or, during an interview, until the interview lets go: ending the
 *  session would send the room to the sign-in page. Its requests still go
 *  without the token, and fail on their own. */
export function isLive(session: LocalSession): boolean {
  return !isExpired(session) || redirectsSuppressed()
}

/** The stored token while it lasts; an expired one is cleared, once no
 *  interview holds the session. */
export function getLocalAccessToken(): string | null {
  const session = getLocalSession()
  if (!session) return null
  if (isExpired(session)) {
    if (!redirectsSuppressed()) store(null)
    return null
  }
  return session.token
}

/** Clears the session — only if it still holds `token`, when given: a
 *  token refused late must not end a session signed in since. */
export function signOutLocal(token?: string): void {
  const session = getLocalSession()
  if (!session) return
  if (token !== undefined && session.token !== token) return
  store(null)
}
