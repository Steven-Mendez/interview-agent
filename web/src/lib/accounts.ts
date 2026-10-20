import type { SignInMethod } from "@/lib/api"

/** How an account signs in, in words. */
export const AUTH_PROVIDER_LABELS: Record<SignInMethod, string> = {
  neon: "Neon Auth",
  local: "Local account",
}

const LOCAL_ACCOUNT_PREFIX = "local:"

/** A local account's username (its id is `local:<username>`), or null. */
export function localUsername(id: string): string | null {
  return id.startsWith(LOCAL_ACCOUNT_PREFIX)
    ? id.slice(LOCAL_ACCOUNT_PREFIX.length) || null
    : null
}

/** "2 of 3" for a guest; "Unlimited" for an admin (no limit). */
export function interviewsUsedLabel(
  used: number,
  limit: number | null
): string {
  return limit === null ? "Unlimited" : `${used} of ${limit}`
}

const dateTimeFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

/** An ISO timestamp as the history page shows dates. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  return Number.isFinite(date.getTime()) ? dateTimeFormat.format(date) : "—"
}

/** An ISO timestamp, the day only. */
export function formatDate(iso: string): string {
  const date = new Date(iso)
  return Number.isFinite(date.getTime()) ? dateFormat.format(date) : "—"
}
