import { useRef } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { RotateCcwIcon } from "lucide-react"

import { getQuestion, replayQuestion } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** The saved question the candidate may not have heard, and the explicit
 *  replay of it. One instance per live room: the request identity is kept
 *  across an uncertain failure so a retry never asks twice. */
export function useCurrentQuestion(interviewId: string) {
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
  const question = query.data?.question ?? null
  const requested = question?.status === "requested"
  return {
    question,
    /** Playback was cut: the candidate may not have heard it. */
    interrupted: question?.status === "interrupted",
    requested,
    canReplay: question !== null && !replay.isPending && !requested,
    replay: () => replay.mutate(),
    pending: replay.isPending,
    failed: replay.isError,
  }
}

export type CurrentQuestion = ReturnType<typeof useCurrentQuestion>

/** The current question as a compact card with "Listen again". */
export function QuestionCard({
  state,
  className,
}: {
  state: CurrentQuestion
  className?: string
}) {
  const { question } = state
  if (!question) return null
  return (
    <section
      aria-label="Current question"
      className={cn(
        "flex flex-col gap-3 rounded-xl bg-primary-container/40 p-4",
        state.interrupted && "bg-warning-container",
        className
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-overline text-muted-foreground uppercase">
          Current question
        </h3>
        {state.interrupted && (
          <span className="text-xs text-on-warning-container">
            Playback was cut short
          </span>
        )}
      </div>
      <p className="text-sm leading-relaxed whitespace-pre-wrap">
        {question.text}
      </p>
      <Button
        variant="outline"
        size="sm"
        className="self-start"
        disabled={!state.canReplay}
        onClick={state.replay}
      >
        <RotateCcwIcon />
        {state.requested ? "Replay requested…" : "Listen again"}
      </Button>
      {state.failed && (
        <p role="alert" className="text-xs text-destructive">
          The replay request could not be confirmed. You can retry.
        </p>
      )}
    </section>
  )
}

export function QuestionRecovery({ interviewId }: { interviewId: string }) {
  const state = useCurrentQuestion(interviewId)
  return <QuestionCard state={state} />
}
