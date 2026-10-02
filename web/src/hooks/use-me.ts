import { useQuery } from "@tanstack/react-query"
import type { QueryClient } from "@tanstack/react-query"

import {
  ApiError,
  getMe,
  lifetimeLimitMessage,
  monthlyCapacityMessage,
  quotaErrorMessage,
} from "@/lib/api"
import type { Me } from "@/lib/api"
import { useCanCallApi } from "@/lib/auth"

export const ME_QUERY_KEY = ["me"] as const

/** The signed-in user and their interview quota (GET /me). Local mode asks
 *  too: the API answers for its fixed development user. */
export function useMe() {
  const enabled = useCanCallApi()
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: getMe,
    enabled,
    staleTime: 30_000,
  })
}

/** After a create or repeat — spent, or refused for the quota. */
export function refreshMeAfter(queryClient: QueryClient, error?: unknown) {
  const spentOrRefused =
    error === undefined || (error instanceof ApiError && error.status === 429)
  if (spentOrRefused) {
    void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY })
  }
}

/** Why no new interview can start right now, or null. Unknown (still
 *  loading, or GET /me failed) never blocks: the API has the last word. */
export function quotaBlock(me: Me | undefined): string | null {
  if (!me || me.is_admin) return null
  if (me.interviews_remaining !== null && me.interviews_remaining <= 0) {
    return lifetimeLimitMessage(me.interview_limit)
  }
  if (!me.demo_capacity_available) return monthlyCapacityMessage()
  return null
}

/** How many interviews are left, for a guest with some to go. */
export function quotaNotice(me: Me): string {
  if (me.is_admin || me.interview_limit === null) return "Unlimited interviews"
  const left = me.interviews_remaining ?? me.interview_limit
  return `You have ${left} of your ${me.interview_limit} interviews left`
}

/** A create or repeat error in words: the quota refusal when it is one. */
export function interviewErrorMessage(
  error: unknown,
  me: Me | undefined
): string {
  return (
    quotaErrorMessage(error, me?.interview_limit) ??
    (error instanceof Error ? error.message : "Something went wrong.")
  )
}
