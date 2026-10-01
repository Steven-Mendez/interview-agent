import { useRef } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { getQuestion, replayQuestion } from "@/lib/api"
import { Button } from "@/components/ui/button"

export function QuestionRecovery({ interviewId }: { interviewId: string }) {
  const requestId = useRef<{ question: string; id: string } | null>(null)
  const query = useQuery({
    queryKey: ["question", interviewId],
    queryFn: () => getQuestion(interviewId),
    refetchInterval: 2000,
  })
  const replay = useMutation({
    mutationFn: async () => {
      const question = query.data?.question
      if (!question) return
      if (requestId.current?.question !== question.id) {
        requestId.current = { question: question.id, id: crypto.randomUUID() }
      }
      return replayQuestion(interviewId, question.id, requestId.current.id)
    },
    onSuccess: () => {
      requestId.current = null
      void query.refetch()
    },
  })
  const question = query.data?.question
  if (!question) return null
  return (
    <div className="border-b px-4 py-2 text-sm">
      {/* Opened when playback was cut: the candidate may not have heard it. */}
      <details open={question.status === "interrupted"}>
        <summary>Current question</summary>
        <p className="my-2 whitespace-pre-wrap">{question.text}</p>
        <Button
          variant="outline"
          size="sm"
          disabled={replay.isPending || question.status === "requested"}
          onClick={() => replay.mutate()}
        >
          {question.status === "requested"
            ? "Replay requested…"
            : "Listen again"}
        </Button>
        {replay.isError && (
          <p role="alert">
            The replay request could not be confirmed. You can retry.
          </p>
        )}
      </details>
    </div>
  )
}
