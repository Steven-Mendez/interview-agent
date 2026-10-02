import { useQuery } from "@tanstack/react-query"
import { AlertCircleIcon, MessageSquareTextIcon } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { EmptyState } from "@/components/ui/empty-state"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api"
import { transcriptQueryOptions } from "@/lib/queries"
import { cn } from "@/lib/utils"

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "short" })

function formatTime(iso: string) {
  const at = Date.parse(iso)
  return Number.isFinite(at) ? timeFormat.format(at) : null
}

/** The stored transcript of a finished interview, as a conversation: each
 *  turn with its speaker, time and capture notes. Fetched when shown. */
export function TranscriptView({
  interviewId,
  agentName,
}: {
  interviewId: string
  agentName: string
}) {
  const query = useQuery(transcriptQueryOptions(interviewId))
  const messages = query.data?.messages ?? []

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-6" aria-busy>
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="flex gap-3">
            <Skeleton className="size-8 rounded-full" />
            <div className="flex flex-1 flex-col gap-2">
              <Skeleton className="h-3.5 w-28" />
              <Skeleton className="h-4 w-full max-w-lg" />
              <Skeleton className="h-4 w-2/3 max-w-md" />
            </div>
          </div>
        ))}
        <span className="sr-only">Loading the transcript…</span>
      </div>
    )
  }
  if (query.isError) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertDescription>{errorMessage(query.error)}</AlertDescription>
      </Alert>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      {query.data.capture_integrity_pending && (
        <Alert variant="warning">
          <AlertCircleIcon />
          <AlertDescription>
            This transcript has unresolved capture incidents and requires
            review.
          </AlertDescription>
        </Alert>
      )}
      {query.data.incidents?.map((incident) => (
        <details
          key={incident.id}
          className="rounded-lg bg-muted px-4 py-2 text-sm"
        >
          <summary className="text-label cursor-pointer py-1">
            Capture incident ·{" "}
            {incident.resolved_at ? "Reviewed" : "Pending review"}
          </summary>
          <p className="pb-2 whitespace-pre-wrap">{incident.content}</p>
        </details>
      ))}
      {messages.length === 0 ? (
        <EmptyState
          icon={<MessageSquareTextIcon />}
          title="No conversation recorded"
          description="Nothing was recorded for this interview."
        />
      ) : (
        <ol className="flex flex-col gap-5">
          {messages.map((m, i) => {
            const isUser = m.role === "user"
            const time = formatTime(m.created_at)
            return (
              <li key={m.id ?? i} className="flex gap-3">
                <span
                  aria-hidden
                  className={cn(
                    "flex size-8 shrink-0 items-center justify-center rounded-full font-heading text-sm",
                    isUser
                      ? "bg-avatar-self text-white"
                      : "bg-primary text-primary-foreground"
                  )}
                >
                  {isUser ? "Y" : agentName.slice(0, 1)}
                </span>
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <p className="flex items-baseline gap-2">
                    <span className="text-label">
                      {isUser ? "You" : agentName}
                    </span>
                    {time && (
                      <time
                        dateTime={m.created_at}
                        className="text-xs text-muted-foreground"
                      >
                        {time}
                      </time>
                    )}
                  </p>
                  <p className="text-sm leading-relaxed whitespace-pre-wrap">
                    {m.content}
                  </p>
                  <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground empty:hidden">
                    {m.version != null && <span>Version {m.version}</span>}
                    {isUser &&
                      m.metrics &&
                      m.metrics.stt_confirmed !== true && (
                        <span>Unconfirmed transcription</span>
                      )}
                    {isUser && m.metrics?.stt_segmentation === "unknown" && (
                      <span>Turn boundaries unverified</span>
                    )}
                  </div>
                  {(m.versions?.length ?? 0) > 1 && (
                    <details className="text-xs">
                      <summary className="cursor-pointer text-primary">
                        Earlier versions
                      </summary>
                      {m.versions
                        ?.filter((v) => v.version !== m.version)
                        .map((v) => (
                          <p
                            key={v.version}
                            className="mt-1 whitespace-pre-wrap text-muted-foreground"
                          >
                            Version {v.version}: {v.content}
                          </p>
                        ))}
                    </details>
                  )}
                </div>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
