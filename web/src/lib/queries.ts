import { queryOptions } from "@tanstack/react-query"

import { getInterview } from "@/lib/api"
import type { Interview } from "@/lib/api"

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
