import * as React from "react"
import { XIcon } from "lucide-react"

import { MascotHead } from "@/components/mascot"
import { IconButton } from "@/components/ui/icon-button"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import type { InterviewSession } from "@/hooks/use-interview-session"
import { cn } from "@/lib/utils"

/** The room's side panel: the live transcript, nothing else — the interview
 *  plan stays off screen, as a real interviewer keeps their notes to
 *  themselves. Beside the stage on wide windows; over it on narrow ones,
 *  where Escape closes it. */
export function TranscriptPanel({
  session,
  agentName,
  onClose,
}: {
  session: InterviewSession
  agentName: string
  onClose: () => void
}) {
  const { messages, agentState, phase } = session

  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  return (
    <aside
      aria-labelledby="transcript-title"
      className="flex w-[min(22.5rem,calc(100vw-2rem))] shrink-0 animate-in flex-col overflow-hidden rounded-2xl bg-card duration-200 fade-in slide-in-from-right-4 max-lg:absolute max-lg:inset-y-4 max-lg:right-4 max-lg:z-30 max-lg:shadow-e3"
    >
      <div className="flex h-16 shrink-0 items-center justify-between pr-2 pl-6">
        <h2 id="transcript-title" className="text-title-lg">
          Transcript
        </h2>
        <IconButton label="Close transcript" onClick={onClose}>
          <XIcon />
        </IconButton>
      </div>
      <MessageScrollerProvider>
        <MessageScroller className="min-h-0 flex-1">
          <MessageScrollerViewport>
            <MessageScrollerContent className="gap-5 px-6 pt-1 pb-6">
              {messages.length === 0 && (
                <p className="py-10 text-center text-sm text-muted-foreground">
                  The conversation appears here as you talk.
                </p>
              )}
              {messages.map((m) => {
                const isUser = m.who === "user"
                return (
                  <MessageScrollerItem key={m.segmentId} scrollAnchor={isUser}>
                    <div className="flex flex-col gap-1">
                      <p className="text-label flex items-center gap-1.5">
                        {!isUser && <MascotHead className="size-4" />}
                        {isUser ? "You" : agentName}
                      </p>
                      <p
                        className={cn(
                          "text-sm leading-relaxed whitespace-pre-wrap",
                          m.interim && "text-muted-foreground"
                        )}
                      >
                        {m.text}
                      </p>
                      {m.incomplete && (
                        <p className="text-xs text-muted-foreground">
                          Incomplete transcription
                        </p>
                      )}
                    </div>
                  </MessageScrollerItem>
                )
              })}
              {phase === "live" && agentState === "thinking" && (
                <MessageScrollerItem scrollAnchor>
                  <p role="status" className="shimmer text-xs">
                    {agentName} is thinking…
                  </p>
                </MessageScrollerItem>
              )}
            </MessageScrollerContent>
          </MessageScrollerViewport>
          <MessageScrollerButton />
        </MessageScroller>
      </MessageScrollerProvider>
    </aside>
  )
}
