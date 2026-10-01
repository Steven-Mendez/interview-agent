import { useRef } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { evaluateInterview } from "@/lib/api"
import { interviewQueryOptions } from "@/lib/queries"

/** One explicit intent keeps its identity if the HTTP reply is lost. */
export function useEvaluationRequest(interviewId: string) {
  const pending = useRef<string | null>(null)
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => {
      pending.current ??= crypto.randomUUID()
      return evaluateInterview(interviewId, pending.current)
    },
    onSuccess: (row) => {
      pending.current = null
      client.setQueryData(interviewQueryOptions(interviewId).queryKey, row)
      void client.invalidateQueries({ queryKey: ["interviews"] })
      void client.invalidateQueries({
        queryKey: ["evaluation-history", interviewId],
      })
    },
  })
}
