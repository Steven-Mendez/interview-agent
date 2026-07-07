import * as React from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import {
  BarVisualizer,
  RoomAudioRenderer,
  RoomContext,
  useVoiceAssistant,
} from "@livekit/components-react"
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  ListChecksIcon,
  MicIcon,
  PhoneOffIcon,
  ScrollTextIcon,
  SparklesIcon,
  UserIcon,
  XCircleIcon,
} from "lucide-react"

import { interviewQueryOptions } from "@/lib/queries"
import { ApiError, evaluateInterview } from "@/lib/api"
import type { Interview } from "@/lib/api"
import { cn } from "@/lib/utils"
import { log } from "@/lib/log"
import { useInterviewSession } from "@/hooks/use-interview-session"
import type { InterviewSession } from "@/hooks/use-interview-session"
import { PageContainer, PageShell } from "@/components/ui/page"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Spinner } from "@/components/ui/spinner"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import { Message, MessageAvatar, MessageContent } from "@/components/ui/message"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Marker, MarkerContent } from "@/components/ui/marker"

// The interviewId lives in the URL so deep links / refresh work: the loader
// warms the query cache (which also drives the polling in Fase 3's
// interviewQueryOptions) before the component renders.
export const Route = createFileRoute("/interviews/$interviewId")({
  loader: ({ context, params }) =>
    context.queryClient.ensureQueryData(
      interviewQueryOptions(params.interviewId)
    ),
  component: InterviewSessionPage,
})

// Same labels as frontend/app.js's AGENT_STATE_LABELS.
const AGENT_STATE_LABELS: Partial<Record<string, string>> = {
  initializing: "Connecting…",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
}

// 180s after the disconnect the evaluation is considered stuck (the worker's
// auto-trigger can die silently); the endpoint is re-invocable, so we offer a
// manual retry — same policy as app.js's MAX_EVAL_POLLS (90 * 2s).
const EVAL_TIMEOUT_MS = 180_000

const TERMINAL_STATUSES = new Set([
  "completed",
  "evaluated",
  "evaluation_failed",
])

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function InterviewSessionPage() {
  const { interviewId } = Route.useParams()
  const { data: interview } = useSuspenseQuery(
    interviewQueryOptions(interviewId)
  )
  const session = useInterviewSession(interviewId)

  // Results replace the live panel once the interview ends: either this tab
  // saw the disconnect (phase 'ended'), or we deep-linked into an already
  // finished interview (phase still 'idle', status terminal).
  const endedByStatus = TERMINAL_STATUSES.has(interview.status)
  const showResults =
    session.phase === "ended" || (session.phase === "idle" && endedByStatus)

  // Each panel renders its own PageShell so short states (idle / evaluating)
  // can center themselves while the live chat and scorecard fill from the top.
  const content = showResults ? (
    <ResultsPanel
      interview={interview}
      interviewId={interviewId}
      endedAt={session.endedAt}
    />
  ) : (
    <InterviewPanel session={session} interview={interview} />
  )

  // Once the room exists, wrap everything in RoomContext: <RoomAudioRenderer>
  // plays the agent track (replaces app.js's manual TrackSubscribed→attach) and
  // the live panel's <BarVisualizer> reads the same room via useVoiceAssistant.
  return session.room ? (
    <RoomContext.Provider value={session.room}>
      <RoomAudioRenderer />
      {content}
    </RoomContext.Provider>
  ) : (
    content
  )
}

// ---- Live interview ---------------------------------------------------------

function InterviewPanel({
  session,
  interview,
}: {
  session: InterviewSession
  interview: Interview
}) {
  const { phase, start, error, messages, agentState } = session

  if (phase === "idle") {
    return (
      <PageShell center>
        <PageContainer
          variant="narrow"
          className="flex flex-col items-center gap-4 text-center"
        >
          <span className="flex size-12 items-center justify-center rounded-full bg-primary/10 text-primary">
            <MicIcon className="size-6" />
          </span>
          <h1 className="text-lg font-medium">Ready when you are</h1>
          <p className="text-sm text-muted-foreground">
            Starting the interview asks for microphone access. The interviewer
            will greet you a few seconds after you connect — just speak into
            your mic.
          </p>
          <Button onClick={start}>Start interview</Button>
          {error && (
            <Alert variant="destructive" className="w-full text-left">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </PageContainer>
      </PageShell>
    )
  }

  const milestones = interview.milestones
  const completedMilestones = milestones.filter((m) => m.completed).length
  const milestonePct =
    milestones.length > 0
      ? Math.round((completedMilestones / milestones.length) * 100)
      : 0

  return (
    // Height is pinned to the viewport (header + inset margins subtracted) so
    // the page itself never scrolls — only the message list does. Two columns:
    // the chat card fills, an aside carries the topics + voice status.
    <div className="mx-auto flex h-[calc(100svh-var(--header-height))] w-full max-w-6xl min-h-0 gap-4 p-4 @lg/main:p-6 @3xl/main:gap-6 md:h-[calc(100svh-var(--header-height)-1rem)]">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-card">
        {/* Panel header: title + live timer on the left, the agent's voice
            status (waveform) and an End button on the right. */}
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <MicIcon className="size-4 text-muted-foreground" />
            Interview
            {phase === "live" && <LiveTimer />}
          </div>
          <div className="flex items-center gap-2">
            {phase === "connecting" ? (
              <StatusPill text="Connecting…" />
            ) : (
              <AgentStatus />
            )}
            {phase === "live" && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void session.room?.disconnect()}
              >
                <PhoneOffIcon className="size-4" />
                End
              </Button>
            )}
          </div>
        </div>

        {/* Compact progress — only on narrow layouts where the aside (which
            carries the full topic list) is hidden. */}
        {milestones.length > 0 && (
          <div className="flex items-center gap-3 border-b px-4 py-2 @3xl/main:hidden">
            <ListChecksIcon className="size-4 shrink-0 text-muted-foreground" />
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${milestonePct}%` }}
              />
            </div>
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {completedMilestones}/{milestones.length} topics
            </span>
          </div>
        )}

        <MessageScrollerProvider>
          <MessageScroller className="min-h-0 flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent className="mx-auto w-full max-w-3xl px-4 py-4">
                {phase === "live" && messages.length === 0 && (
                  <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center text-sm text-muted-foreground">
                    <AgentBars
                      className="flex h-12 items-end justify-center gap-1"
                      barClassName="w-1.5 rounded-full bg-primary/40 transition-colors data-[lk-highlighted=true]:bg-primary"
                    />
                    Connected — the interviewer will greet you in a few seconds.
                    Speak into your microphone.
                  </div>
                )}
                {messages.map((m) => {
                    const isUser = m.who === "user"
                    return (
                      <MessageScrollerItem
                        key={m.segmentId}
                        scrollAnchor={isUser}
                      >
                        <Message align={isUser ? "end" : "start"}>
                          <MessageAvatar>
                            <Avatar>
                              <AvatarFallback
                                className={
                                  isUser
                                    ? undefined
                                    : "bg-background text-foreground"
                                }
                              >
                                {isUser ? (
                                  <UserIcon className="size-4" />
                                ) : (
                                  <SparklesIcon className="size-4" />
                                )}
                              </AvatarFallback>
                            </Avatar>
                          </MessageAvatar>
                          <MessageContent>
                            <Bubble
                              align={isUser ? "end" : "start"}
                              variant={isUser ? "default" : "tinted"}
                              className={m.interim ? "opacity-70" : undefined}
                            >
                              <BubbleContent>{m.text}</BubbleContent>
                            </Bubble>
                          </MessageContent>
                        </Message>
                      </MessageScrollerItem>
                    )
                  })}
                  {/* Typing-style indicator that fills the dead air while the
                      LLM composes its next turn (mirrors lk.agent.state). */}
                  {phase === "live" && agentState === "thinking" && (
                    <MessageScrollerItem scrollAnchor>
                      <Marker role="status" className="px-1">
                        <MarkerContent className="shimmer">
                          The interviewer is thinking…
                        </MarkerContent>
                      </Marker>
                    </MessageScrollerItem>
                  )}
                </MessageScrollerContent>
              </MessageScrollerViewport>
              <MessageScrollerButton />
            </MessageScroller>
          </MessageScrollerProvider>
        </div>

        {/* Aside — fills the previously-empty sides with the agent's live voice
            status and the interview plan (topics) with per-topic state. */}
        <aside className="hidden w-72 shrink-0 flex-col gap-4 @3xl/main:flex @5xl/main:w-80">
          {phase === "live" && (
            <Card>
              <CardContent className="flex flex-col items-center gap-3 py-6 text-center">
                <AgentBars
                  className="flex h-14 items-end justify-center gap-1.5"
                  barClassName="w-1.5 rounded-full bg-primary/40 transition-colors data-[lk-highlighted=true]:bg-primary"
                />
                <AgentStateLabel />
              </CardContent>
            </Card>
          )}

          {milestones.length > 0 && (
            <Card className="flex min-h-0 flex-1 flex-col">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <ListChecksIcon className="size-4 text-muted-foreground" />
                  Topics
                  <CountLabel n={`${completedMilestones}/${milestones.length}`} />
                </CardTitle>
              </CardHeader>
              <CardContent className="min-h-0 flex-1 overflow-y-auto">
                <ol className="flex flex-col gap-3">
                  {milestones.map((m, i) => (
                    <li key={m.id} className="flex items-start gap-2.5 text-sm">
                      {m.completed ? (
                        <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-success" />
                      ) : (
                        <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-medium text-muted-foreground tabular-nums">
                          {i + 1}
                        </span>
                      )}
                      <span
                        className={cn(
                          m.completed && "text-muted-foreground line-through"
                        )}
                      >
                        {m.title}
                      </span>
                    </li>
                  ))}
                </ol>
              </CardContent>
            </Card>
          )}
        </aside>
      </div>
  )
}

// Live status chip shown in the chat panel header (replaces the old bottom
// "Speaking…" marker). The pulsing dot signals an active connection.
function StatusPill({ text }: { text: string }) {
  return (
    <span className="flex items-center gap-1.5 rounded-full border bg-muted/50 px-2.5 py-1 text-xs font-medium text-muted-foreground">
      <span className={cn("size-2 rounded-full bg-primary animate-pulse")} />
      {text}
    </span>
  )
}

// Bars driven by the agent's audio track: they dance to the interviewer's
// voice while speaking and to an idle sequencer (via `state`) while it listens
// or thinks. Must render inside RoomContext — useVoiceAssistant reads the room.
function AgentBars({
  className,
  barClassName,
}: {
  className: string
  barClassName: string
}) {
  const { state, audioTrack } = useVoiceAssistant()
  return (
    <BarVisualizer
      state={state}
      track={audioTrack}
      barCount={5}
      className={className}
    >
      <span className={barClassName} />
    </BarVisualizer>
  )
}

// Header voice status: a compact waveform + the agent's current lifecycle
// label (Listening… / Thinking… / Speaking…).
function AgentStatus() {
  const { state } = useVoiceAssistant()
  const label = AGENT_STATE_LABELS[state] ?? "Listening…"
  return (
    <span className="flex items-center gap-2 rounded-full border bg-muted/50 py-1 pr-3 pl-2 text-xs font-medium text-muted-foreground">
      <AgentBars
        className="flex h-4 w-9 items-center justify-center gap-[3px]"
        barClassName="w-[3px] rounded-full bg-primary/30 transition-colors data-[lk-highlighted=true]:bg-primary"
      />
      {label}
    </span>
  )
}

// Just the agent's lifecycle label (Listening… / Thinking… / Speaking…) —
// used under the aside's larger waveform.
function AgentStateLabel() {
  const { state } = useVoiceAssistant()
  return (
    <span className="text-sm font-medium">
      {AGENT_STATE_LABELS[state] ?? "Listening…"}
    </span>
  )
}

// Elapsed-time counter, anchored at first render (mounts when phase→live).
function LiveTimer() {
  const startRef = React.useRef(Date.now())
  const [elapsed, setElapsed] = React.useState(0)
  React.useEffect(() => {
    const id = setInterval(
      () => setElapsed(Math.floor((Date.now() - startRef.current) / 1000)),
      1000
    )
    return () => clearInterval(id)
  }, [])
  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0")
  const ss = String(elapsed % 60).padStart(2, "0")
  return (
    <span className="ml-1 flex items-center gap-1.5 text-xs font-normal tabular-nums text-muted-foreground">
      <span className="size-1.5 rounded-full bg-primary animate-pulse" />
      {mm}:{ss}
    </span>
  )
}

// ---- Results ----------------------------------------------------------------

function ResultsPanel({
  interview,
  interviewId,
  endedAt,
}: {
  interview: Interview
  interviewId: string
  endedAt: number | null
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [timedOut, setTimedOut] = React.useState(false)

  // Anchor the evaluation-timeout clock at the disconnect; on a deep-link
  // revisit (no disconnect in this tab) anchor it at mount instead.
  const anchorRef = React.useRef<number>(endedAt ?? Date.now())
  if (endedAt !== null) anchorRef.current = endedAt

  const status = interview.status
  const waiting = !TERMINAL_STATUSES.has(status) || status === "completed"

  React.useEffect(() => {
    if (!waiting) return
    const remaining = anchorRef.current + EVAL_TIMEOUT_MS - Date.now()
    if (remaining <= 0) {
      setTimedOut(true)
      return
    }
    const timer = setTimeout(() => setTimedOut(true), remaining)
    return () => clearTimeout(timer)
  }, [waiting, status])

  const retry = useMutation({
    // The response IS the evaluated interview — no need to resume polling.
    mutationFn: () => evaluateInterview(interviewId),
    onSuccess: (updated) => {
      queryClient.setQueryData(
        interviewQueryOptions(interviewId).queryKey,
        updated
      )
      setTimedOut(false)
      log(
        "evaluation received:",
        updated.evaluation?.hired ? "HIRED" : "NOT HIRED",
        `score ${updated.evaluation?.score ?? "?"}/100`
      )
    },
    onError: (error) => {
      console.error("[app] evaluation retry failed:", error)
    },
  })

  if (status === "evaluated" && interview.evaluation) {
    return (
      <Evaluation
        interview={interview}
        onNew={() => void navigate({ to: "/" })}
      />
    )
  }

  const failed = status === "evaluation_failed"
  const showRetry = failed || timedOut

  return (
    <PageShell center>
      <PageContainer
        variant="narrow"
        className="flex flex-col items-center gap-4 text-center"
      >
        {!showRetry && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner />
            Evaluating the interview… (can take a couple of minutes)
          </p>
        )}
        {showRetry && (
          <Alert variant="destructive" className="w-full text-left">
            <AlertDescription>
              {retry.isError
                ? errorMessage(retry.error)
                : failed
                  ? "The evaluation failed. You can retry it."
                  : "The evaluation is taking longer than expected. You can retry it."}
            </AlertDescription>
          </Alert>
        )}
        {showRetry && (
          <Button onClick={() => retry.mutate()} disabled={retry.isPending}>
            {retry.isPending ? "Retrying…" : "Retry evaluation"}
          </Button>
        )}
      </PageContainer>
    </PageShell>
  )
}

function Evaluation({
  interview,
  onNew,
}: {
  interview: Interview
  onNew: () => void
}) {
  const ev = interview.evaluation
  if (!ev) return null

  const milestones = interview.milestones
  const completed = milestones.filter((m) => m.completed).length

  // Traffic-light verdict: green once actually hired, otherwise red/yellow by
  // how far the score sits from a pass — replaces the separate HIRED/NOT
  // HIRED badge with a single colored status where the score bar lives.
  const tier = ev.hired ? "success" : ev.score < 40 ? "destructive" : "warning"
  const verdictLabel = ev.hired ? "Hired" : "Not hired"
  const verdictTextClass = {
    success: "text-success",
    warning: "text-warning",
    destructive: "text-destructive",
  }[tier]
  const verdictBarClass = {
    success: "bg-success",
    warning: "bg-warning",
    destructive: "bg-destructive",
  }[tier]

  return (
    <PageShell>
      <PageContainer variant="wide">
        <div className="flex flex-col gap-6">
          {/* Score banner — horizontal: score and a progress bar tinted with
              the traffic-light verdict color (red / yellow / green). */}
          <Card>
            <CardContent className="flex flex-col gap-4 @xl/main:flex-row @xl/main:items-center @xl/main:gap-8">
              <div className="flex items-baseline gap-1">
                <span className="text-5xl leading-none font-bold">
                  {ev.score}
                </span>
                <span className="text-xl text-muted-foreground">/100</span>
              </div>
              <div className="flex flex-1 flex-col gap-1.5">
                <span className={cn("text-xs font-medium", verdictTextClass)}>
                  {verdictLabel}
                </span>
                <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn("h-full rounded-full", verdictBarClass)}
                    style={{ width: `${Math.min(Math.max(ev.score, 0), 100)}%` }}
                  />
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Milestones — full-width chip row, stretched like the panels below. */}
          {milestones.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <ListChecksIcon className="size-4 text-muted-foreground" />
                  Milestones
                  <CountLabel n={`${completed}/${milestones.length}`} />
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="flex flex-wrap gap-2">
                  {milestones.map((m) => (
                    <li
                      key={m.id}
                      className="inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm"
                    >
                      {m.completed ? (
                        <CheckCircle2Icon className="size-4 shrink-0 text-success" />
                      ) : (
                        <XCircleIcon className="size-4 shrink-0 text-destructive" />
                      )}
                      <span
                        className={cn(!m.completed && "text-muted-foreground")}
                      >
                        {m.title}
                      </span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}

          {/* Strengths + areas of concern side by side. */}
          <div className="grid gap-6 @xl/main:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <CheckCircle2Icon className="size-4 text-success" />
                  Strengths
                  <CountLabel n={ev.strengths.length} />
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ItemList
                  items={ev.strengths}
                  icon={CheckCircle2Icon}
                  iconClassName="text-success"
                  empty="No strengths noted."
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <AlertCircleIcon className="size-4 text-destructive" />
                  Areas of concern
                  <CountLabel n={ev.weaknesses.length} />
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ItemList
                  items={ev.weaknesses}
                  icon={XCircleIcon}
                  iconClassName="text-destructive"
                  empty="No weaknesses noted."
                />
              </CardContent>
            </Card>
          </div>

          {/* Rationale — full width. */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <ScrollTextIcon className="size-4 text-muted-foreground" />
                Evaluator rationale
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm leading-relaxed whitespace-pre-wrap">
                {ev.rationale}
              </p>
            </CardContent>
          </Card>

          <Button className="w-full @xl/main:w-fit @xl/main:self-end" onClick={onNew}>
            New interview
          </Button>
        </div>
      </PageContainer>
    </PageShell>
  )
}

function CountLabel({ n }: { n: number | string }) {
  return (
    <span className="ml-auto text-sm font-normal text-muted-foreground tabular-nums">
      {n}
    </span>
  )
}

function ItemList({
  items,
  icon: Icon,
  iconClassName,
  empty,
}: {
  items: string[]
  icon: React.ComponentType<{ className?: string }>
  iconClassName?: string
  empty: string
}) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">{empty}</p>
  }
  return (
    <ul className="flex flex-col gap-3">
      {items.map((item, i) => (
        <li key={i} className="flex items-start gap-2 text-sm">
          <Icon className={cn("mt-0.5 size-4 shrink-0", iconClassName)} />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  )
}
