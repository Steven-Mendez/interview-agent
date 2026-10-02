import { useNavigate } from "@tanstack/react-router"
import { useMutation, useQueryClient } from "@tanstack/react-query"

import { refreshMeAfter } from "@/hooks/use-me"
import { repeatInterview } from "@/lib/api"
import type { RepeatRequest } from "@/lib/api"
import { interviewQueryOptions } from "@/lib/queries"
import { log } from "@/lib/log"

/** Same role, same resume, same bar — replanned. Lands on the new interview
 *  exactly like creating one from the setup flow does. */
export function useRepeatInterview(interviewId: string) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body?: RepeatRequest) => repeatInterview(interviewId, body),
    onSuccess: (next) => {
      log("interview repeated:", next.id)
      // The response is the new row: seed its detail so the landing needs no
      // fetch, and drop every cached history page — the one on screen a
      // moment ago stays fresh for 10 s and would come back without it.
      queryClient.setQueryData(interviewQueryOptions(next.id).queryKey, next)
      void queryClient.invalidateQueries({ queryKey: ["interviews"] })
      refreshMeAfter(queryClient)
      void navigate({
        to: "/interviews/$interviewId",
        params: { interviewId: next.id },
      })
    },
    onError: (error) => {
      console.error("[app] repeat failed:", error)
      refreshMeAfter(queryClient, error)
    },
  })
}
