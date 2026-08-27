import { queryOptions } from "@tanstack/react-query"

import { getInterview, getTranscript, listInterviews } from "@/lib/api"
import type { Interview, InterviewStatus } from "@/lib/api"

type InterviewStatusValue = Interview["status"]

// Statuses where the poll should keep going: the room may still be
// connected (created/planned/interviewing) or the auto-triggered evaluation
// hasn't landed yet (completed). Terminal states (evaluated,
// evaluation_failed, error) stop the interval — Fase 4's Retry flow
// re-invokes POST .../evaluate directly and calls `setQueryData` with the
// response instead of resuming polling.
const POLLING_STATUSES: ReadonlySet<InterviewStatusValue> = new Set([
  "created",
  "planned",
  "interviewing",
  "completed",
])

export function interviewQueryOptions(interviewId: string) {
  return queryOptions({
    queryKey: ["interview", interviewId] as const,
    queryFn: () => getInterview(interviewId),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status !== undefined && POLLING_STATUSES.has(status) ? 2000 : false
    },
  })
}

export const HISTORY_PAGE_SIZE = 20

/** One page of the history. The key carries the page + filter so paging is
 *  cached per page instead of thrashing a single entry. */
export function interviewsQueryOptions(params: {
  offset?: number
  status?: InterviewStatus
}) {
  const offset = params.offset ?? 0
  const status = params.status
  return queryOptions({
    queryKey: ["interviews", { offset, status: status ?? null }] as const,
    queryFn: () => listInterviews({ limit: HISTORY_PAGE_SIZE, offset, status }),
    // A row's status/score changes as an interview progresses, and the list is
    // the screen you come back to after finishing one.
    staleTime: 10_000,
  })
}

/** The stored turns of a past interview. Immutable once the interview is over,
 *  which is the only place it is read from — hence no refetching. */
export function transcriptQueryOptions(interviewId: string) {
  return queryOptions({
    queryKey: ["interview", interviewId, "transcript"] as const,
    queryFn: () => getTranscript(interviewId),
    staleTime: Infinity,
  })
}
