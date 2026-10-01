import { InterviewTimer } from "@/components/interview-timer"
import { InterviewEnding } from "@/components/interview-ending"
import { RepeatOptions } from "@/components/repeat-options"
import { ClosingPanel } from "@/components/closing-panel"
import { AssessmentRequest } from "@/components/assessment-request"
import { useEvaluationRequest } from "@/hooks/use-evaluation-request"
import { TranscriptReview } from "@/components/transcript-review"
import { EvaluationHistory } from "@/components/evaluation-history"
import { QuestionRecovery } from "@/components/question-recovery"
import * as React from "react"
import { Link, createFileRoute, useNavigate } from "@tanstack/react-router"
import {
  useMutation,
  useQuery,
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
  CircleDotIcon,
  ChevronDownIcon,
  ClockIcon,
  HistoryIcon,
  ListChecksIcon,
  MessagesSquareIcon,
  MicIcon,
  GaugeIcon,
  PhoneOffIcon,
  RotateCcwIcon,
  ScrollTextIcon,
  SparklesIcon,
  UserIcon,
  VideoIcon,
  VideoOffIcon,
  XCircleIcon,
} from "lucide-react"

import {
  CriterionFeedback,
  EvaluationOutcome,
} from "@/components/evaluation-feedback"
import {
  InterviewLimits,
  TopicStatus,
  isSettled,
} from "@/components/interview-progress"

import { interviewQueryOptions, transcriptQueryOptions } from "@/lib/queries"
import {
  ApiError,
  durationLabel,
  SENIORITY_LABELS,
  repeatInterview,
} from "@/lib/api"
import type { Interview, RepeatRequest } from "@/lib/api"
import {
  EVAL_TIMEOUT_MS,
  evaluationAnchor,
  shouldShowInterviewResults,
} from "@/lib/evaluation"
import { cn } from "@/lib/utils"
import { log } from "@/lib/log"
import { useInterviewSession } from "@/hooks/use-interview-session"
import type { InterviewSession } from "@/hooks/use-interview-session"
import { useDevicePreview } from "@/hooks/use-device-preview"
import type { DevicePreview } from "@/hooks/use-device-preview"
import { PageContainer, PageShell } from "@/components/ui/page"
import { Button } from "@/components/ui/button"
import { InterviewAudioRecovery } from "@/components/interview-audio-recovery"
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
  component: InterviewSessionRoute,
})

// `key`: navigating between interviews (repeating one lands on a NEW id under
// this same route) reuses this component instance, and useInterviewSession's
// phase would carry over — a fresh interview would open straight into the
// previous one's "ended" state. Keying it remounts on every id change.
function InterviewSessionRoute() {
  const { interviewId } = Route.useParams()
  return <InterviewSessionPage key={interviewId} interviewId={interviewId} />
}

const AGENT_STATE_LABELS: Partial<Record<string, string>> = {
  initializing: "Connecting…",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function InterviewSessionPage({ interviewId }: { interviewId: string }) {
  const { data: interview } = useSuspenseQuery(
    interviewQueryOptions(interviewId)
  )
  const session = useInterviewSession(interviewId)
  React.useEffect(() => {
    session.syncClosingState(
      interview.status,
      interview.closing_id,
      interview.farewell_status,
      interview.transcript_sealed ?? false
    )
  }, [
    session.syncClosingState,
    interview.status,
    interview.closing_id,
    interview.farewell_status,
    interview.transcript_sealed,
  ])

  // Results replace the live panel once the interview ends: either this tab
  // saw the disconnect (phase 'ended'), or we deep-linked into an already
  // finished interview (phase still 'idle', status terminal).
  const showResults = shouldShowInterviewResults(
    session.phase,
    interview.status
  )

  // Each panel renders its own PageShell so short states (idle / evaluating)
  // can center themselves while the live chat and scorecard fill from the top.
  const content = showResults ? (
    <ResultsPanel
      interview={interview}
      interviewId={interviewId}
      endedAt={session.endedAt}
    />
  ) : interview.status === "error" ? (
    // Preparation or the worker failed: a disconnect cannot turn this into
    // an evaluation that will never run.
    <FailedPanel interview={interview} />
  ) : interview.status === "closing" && session.phase === "idle" ? (
    // Reloaded (or opened) while the server closes: there is no clip to
    // replay here, only the durable outcome to wait for.
    <ClosingPanel interview={interview} />
  ) : interview.status === "interviewing" &&
    !interview.can_start &&
    session.phase === "idle" ? (
    // The worker died mid-interview and the reconnect window has closed:
    // nothing to rejoin (Start would 409 too), only what was recorded.
    <InterruptedPanel interview={interview} />
  ) : (
    <InterviewPanel session={session} interview={interview} />
  )

  // Once the room exists, wrap everything in RoomContext: <RoomAudioRenderer>
  // plays the agent track and the live panel's <BarVisualizer> reads the same
  // room via useVoiceAssistant.
  return session.room ? (
    <RoomContext.Provider value={session.room}>
      <RoomAudioRenderer />
      {(session.phase === "live" || session.phase === "closing") && (
        <div className="mx-auto w-full max-w-6xl px-4 pt-2">
          <InterviewAudioRecovery />
        </div>
      )}
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
  // Owned here, not inside the idle branch: the self-view survives into the
  // live panel, and leaving this component (results, or navigating away) is
  // what releases the camera.
  const preview = useDevicePreview()

  if (phase === "idle") {
    return (
      <PreJoinPanel
        interview={interview}
        preview={preview}
        error={error}
        // An `interviewing` row that can still start is one this candidate
        // left (a closed tab, a dropped line) with the room waiting for them.
        rejoin={interview.status === "interviewing"}
        rejoinUntil={interview.reconnect_until}
        onStart={() =>
          start({
            audioDeviceId: preview.micId || undefined,
            audioOutputDeviceId: preview.speakerId || undefined,
            // The check holds the microphone LiveKit is about to reopen, so
            // it lets go right before the publish — and takes it back should
            // the start fall through, so the meter is live on the way back
            // instead of a bar that never moves.
            beforePublish: preview.releaseMic,
            onPublishFailed: preview.reclaimMic,
          })
        }
      />
    )
  }

  const milestones = interview.milestones
  const completedMilestones = milestones.filter(isSettled).length
  const milestonePct =
    milestones.length > 0
      ? Math.round((completedMilestones / milestones.length) * 100)
      : 0

  return (
    // Height is pinned to the viewport (header + inset margins subtracted) so
    // the page itself never scrolls — only the message list does. Two columns:
    // the chat card fills, an aside carries the topics + voice status.
    <div className="mx-auto flex h-[calc(100svh-var(--header-height))] min-h-0 w-full max-w-6xl gap-4 p-4 md:h-[calc(100svh-var(--header-height)-1rem)] @lg/main:p-6 @3xl/main:gap-6">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-card">
        {/* Panel header: title + live timer on the left, the agent's voice
            status (waveform) and an End button on the right. */}
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <MicIcon className="size-4 text-muted-foreground" />
            Interview
            {phase === "live" && (
              <InterviewTimer elapsedSeconds={interview.elapsed_seconds} />
            )}
          </div>
          <div className="flex items-center gap-2">
            {phase === "closing" ? (
              <StatusPill
                text={
                  session.closingRecoveryPending
                    ? "Recovery pending"
                    : "Closing interview…"
                }
              />
            ) : phase === "connecting" ? (
              <StatusPill text="Connecting…" />
            ) : (
              <AgentStatus />
            )}
            {phase === "live" && (
              <Button variant="outline" size="sm" onClick={session.requestEnd}>
                <PhoneOffIcon className="size-4" />
                End
              </Button>
            )}
            {phase === "closing" && session.farewellBlocked && (
              <Button size="sm" onClick={session.resumeFarewell}>
                Enable farewell audio
              </Button>
            )}
            {phase === "closing" && session.closingRecoveryPending && (
              <Button
                variant="outline"
                size="sm"
                render={<Link to="/interviews" />}
              >
                View saved interviews
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
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
              {completedMilestones}/{milestones.length} topics
            </span>
          </div>
        )}

        {phase === "live" && session.technicalNotice && (
          <Alert className="rounded-none border-x-0 border-t-0">
            <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
              <span>
                The interviewer had a technical difficulty. Answer again by
                speaking, or end the interview.
              </span>
              <Button variant="outline" size="sm" onClick={session.requestEnd}>
                <PhoneOffIcon className="size-4" />
                End interview
              </Button>
            </AlertDescription>
          </Alert>
        )}
        {phase === "live" && <QuestionRecovery interviewId={interview.id} />}
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
                            {m.incomplete && (
                              <span className="block px-3 pb-2 text-xs opacity-70">
                                Incomplete transcription
                              </span>
                            )}
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
        {/* Kept from the pre-join check: the interviewer never receives it,
            but practising to your own face is the point of leaving it on. */}
        {preview.cameraOn && preview.stream && (
          <Card className="overflow-hidden p-0">
            <SelfView stream={preview.stream} className="aspect-video w-full" />
          </Card>
        )}
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
                    {isSettled(m) ? (
                      <CircleDotIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    ) : (
                      <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-medium text-muted-foreground tabular-nums">
                        {i + 1}
                      </span>
                    )}
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <span>{m.title}</span>
                      <TopicStatus milestone={m} />
                    </div>
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

// ---- Pre-join device check --------------------------------------------------

/** The local camera feed, mirrored the way every video app mirrors it. */
function SelfView({
  stream,
  className,
}: {
  stream: MediaStream
  className?: string
}) {
  const ref = React.useRef<HTMLVideoElement>(null)
  React.useEffect(() => {
    // srcObject is a property, not an attribute — it cannot be set in JSX.
    if (ref.current) ref.current.srcObject = stream
  }, [stream])
  return (
    <video
      ref={ref}
      autoPlay
      muted
      playsInline
      className={cn("scale-x-[-1] bg-muted object-cover", className)}
    />
  )
}

/** Horizontal input-level bar, fed by the preview's RMS level. */
function LevelMeter({ level }: { level: number }) {
  return (
    <div
      className="flex h-2 w-full overflow-hidden rounded-full bg-muted"
      role="meter"
      aria-label="Microphone level"
      aria-valuenow={Math.round(level * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-success transition-[width] duration-100"
        style={{ width: `${Math.round(level * 100)}%` }}
      />
    </div>
  )
}

const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "short" })

/** What you see before joining: pick the microphone you will be heard
 *  through, watch it register sound, and optionally frame yourself.
 *
 *  `rejoin` turns Start into Rejoin: the interview is already running and
 *  its room waits for the candidate — until `rejoinUntil` (ISO), when the
 *  backend says so. */
function PreJoinPanel({
  interview,
  preview,
  error,
  rejoin,
  rejoinUntil,
  onStart,
}: {
  interview: Interview
  preview: DevicePreview
  error: string | null
  rejoin: boolean
  rejoinUntil: string | null
  onStart: () => void
}) {
  const ready = preview.status === "ready"
  const rejoinDeadline = rejoinUntil === null ? NaN : Date.parse(rejoinUntil)
  const verb = rejoin ? "Rejoin" : "Start"

  return (
    <PageShell center>
      <PageContainer variant="reading" className="flex flex-col gap-4">
        <div className="flex flex-col items-center gap-2 text-center">
          <span className="flex size-12 items-center justify-center rounded-full bg-primary/10 text-primary">
            <MicIcon className="size-6" />
          </span>
          <h1 className="text-lg font-medium">
            {rejoin ? "Pick up where you left off" : "Ready when you are"}
          </h1>
          <p className="text-sm text-muted-foreground">
            {rejoin ? (
              <>
                This interview is still running — the interviewer is waiting for
                you to come back
                {rejoinUntil !== null && Number.isFinite(rejoinDeadline) && (
                  <>
                    {" "}
                    until{" "}
                    <time dateTime={rejoinUntil}>
                      {timeFormat.format(rejoinDeadline)}
                    </time>
                  </>
                )}
                . Check your devices, then rejoin.
              </>
            ) : (
              <>
                Check your devices first. The interviewer greets you a few
                seconds after you connect — just speak into your mic.
              </>
            )}
          </p>
        </div>

        <InterviewLimits interview={interview} />
        <Card>
          <CardContent className="flex flex-col gap-4">
            {preview.status === "idle" ? (
              <div className="flex flex-col items-center gap-3 py-4 text-center">
                <p className="text-sm text-muted-foreground">
                  Testing your microphone needs permission — the same one the
                  interview itself asks for.
                </p>
                <Button variant="outline" onClick={preview.request}>
                  <MicIcon />
                  Check my devices
                </Button>
              </div>
            ) : preview.status === "requesting" ? (
              <p className="flex items-center justify-center gap-2 py-4 text-sm text-muted-foreground">
                <Spinner />
                Waiting for microphone permission…
              </p>
            ) : (
              <div className="grid gap-4 @xl/main:grid-cols-[1fr_auto]">
                <div className="flex min-w-0 flex-col gap-4">
                  <Field>
                    <FieldLabel htmlFor="mic">Microphone</FieldLabel>
                    <Select
                      value={preview.micId}
                      onValueChange={(next) => next && preview.selectMic(next)}
                    >
                      <SelectTrigger id="mic" className="w-full">
                        <SelectValue>
                          {(value: string) =>
                            preview.mics.find((d) => d.deviceId === value)
                              ?.label || "System default"
                          }
                        </SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {preview.mics.map((device) => (
                          <SelectItem
                            key={device.deviceId}
                            value={device.deviceId}
                          >
                            {device.label || "Microphone"}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <LevelMeter level={preview.level} />
                    <FieldDescription>
                      Say something — the bar should move.
                    </FieldDescription>
                  </Field>

                  <Field>
                    <FieldLabel htmlFor="speaker">Speaker</FieldLabel>
                    {preview.outputSelectable && preview.speakers.length > 0 ? (
                      <Select
                        value={preview.speakerId}
                        onValueChange={(next) =>
                          preview.selectSpeaker(next ?? "")
                        }
                      >
                        <SelectTrigger id="speaker" className="w-full">
                          <SelectValue>
                            {(value: string) =>
                              preview.speakers.find((d) => d.deviceId === value)
                                ?.label || "System default"
                            }
                          </SelectValue>
                        </SelectTrigger>
                        <SelectContent>
                          {preview.speakers.map((device) => (
                            <SelectItem
                              key={device.deviceId}
                              value={device.deviceId}
                            >
                              {device.label || "Speaker"}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <FieldDescription>
                        This browser plays the interview through its default
                        audio output; it cannot be changed here.
                      </FieldDescription>
                    )}
                  </Field>

                  <Field>
                    <div className="flex items-center justify-between gap-2">
                      <FieldLabel htmlFor="cam">Camera</FieldLabel>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={preview.toggleCamera}
                      >
                        {preview.cameraOn ? (
                          <>
                            <VideoOffIcon />
                            Turn off
                          </>
                        ) : (
                          <>
                            <VideoIcon />
                            Turn on
                          </>
                        )}
                      </Button>
                    </div>
                    {preview.cameraOn && preview.cams.length > 0 && (
                      <Select
                        value={preview.camId}
                        onValueChange={(next) =>
                          next && preview.selectCam(next)
                        }
                      >
                        <SelectTrigger id="cam" className="w-full">
                          <SelectValue>
                            {(value: string) =>
                              preview.cams.find((d) => d.deviceId === value)
                                ?.label || "System default"
                            }
                          </SelectValue>
                        </SelectTrigger>
                        <SelectContent>
                          {preview.cams.map((device) => (
                            <SelectItem
                              key={device.deviceId}
                              value={device.deviceId}
                            >
                              {device.label || "Camera"}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                    <FieldDescription>
                      Local only — the interviewer hears you, it never sees you.
                      Leave it on to practise to your own face.
                    </FieldDescription>
                  </Field>
                </div>

                <div className="w-full @xl/main:w-56">
                  {preview.cameraOn && preview.stream ? (
                    <SelfView
                      stream={preview.stream}
                      className="aspect-video w-full rounded-lg"
                    />
                  ) : (
                    <div className="flex aspect-video w-full items-center justify-center rounded-lg border border-dashed text-muted-foreground">
                      <VideoOffIcon className="size-5" />
                    </div>
                  )}
                </div>
              </div>
            )}

            {preview.error && (
              <Alert variant="destructive">
                <AlertDescription>
                  {preview.error}
                  {preview.status === "denied" && (
                    <>
                      {" "}
                      You can also start without the check — the interview will
                      ask for the microphone again.
                    </>
                  )}
                </AlertDescription>
              </Alert>
            )}
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
          </CardContent>
          <CardFooter className="justify-end">
            {/* Never gated on the check: a candidate whose browser hides the
                device list must still be able to start. */}
            <Button onClick={onStart}>
              {ready ? `${verb} interview` : `${verb} interview anyway`}
            </Button>
          </CardFooter>
        </Card>
      </PageContainer>
    </PageShell>
  )
}

// Live status chip shown in the chat panel header (replaces the old bottom
// "Speaking…" marker). The pulsing dot signals an active connection.
function StatusPill({ text }: { text: string }) {
  return (
    <span className="flex items-center gap-1.5 rounded-full border bg-muted/50 px-2.5 py-1 text-xs font-medium text-muted-foreground">
      <span className={cn("size-2 animate-pulse rounded-full bg-primary")} />
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
  const [timedOut, setTimedOut] = React.useState(false)

  const status = interview.status
  // No verdict yet: the run is going (evaluating), has not been claimed
  // (completed), or this tab saw the disconnect before the worker even
  // marked the row (still interviewing, phase 'ended').
  const waiting = status !== "evaluated" && status !== "evaluation_failed"

  // The timeout clock counts from the disconnect (or the row's updated_at on
  // a revisit) until the run is claimed, then from its latest heartbeat —
  // every poll that brings a newer updated_at moves the anchor and, through
  // the effect below, pushes the deadline back. See evaluationAnchor.
  const anchor = evaluationAnchor(status, interview.updated_at, endedAt)

  React.useEffect(() => {
    if (!waiting) return
    const remaining = anchor + EVAL_TIMEOUT_MS - Date.now()
    if (remaining <= 0) {
      setTimedOut(true)
      return
    }
    // An anchor that moved forward (a heartbeat, or the run being claimed
    // after the alert went up) means the evaluation is alive after all.
    setTimedOut(false)
    const timer = setTimeout(() => setTimedOut(true), remaining)
    return () => clearTimeout(timer)
  }, [waiting, status, anchor])

  const retry = useEvaluationRequest(interviewId)

  if (interview.evaluation) {
    return <Evaluation interview={interview} />
  }

  const failed = status === "evaluation_failed"
  const showRetry = failed || timedOut
  // What the button does depends on where the run got stuck: it never
  // started (completed / interviewing), it died on the way (evaluating), or
  // it ended in an error (evaluation_failed).
  const action = failed
    ? "Retry"
    : status === "evaluating"
      ? "Restart"
      : "Start"

  return (
    <PageShell center>
      <PageContainer
        variant="narrow"
        className="flex flex-col items-center gap-4 text-center"
      >
        <InterviewEnding interview={interview} />
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
                  : status === "evaluating"
                    ? "The evaluation is taking longer than expected. You can restart it."
                    : "The evaluation did not start. You can start it now."}
            </AlertDescription>
          </Alert>
        )}
        {showRetry && (
          <Button onClick={() => retry.mutate()} disabled={retry.isPending}>
            {retry.isPending ? "Starting…" : `${action} evaluation`}
          </Button>
        )}
        <div className="flex w-full flex-col gap-4 text-left">
          <EvaluationHistory
            interviewId={interview.id}
            milestones={interview.milestones}
            duration={durationLabel(
              interview.interview_length,
              interview.max_minutes
            )}
          />
          <TranscriptReview interviewId={interview.id} />
        </div>
      </PageContainer>
    </PageShell>
  )
}

/** Same role, same resume, same bar — replanned. Lands on the new interview
 *  exactly like creating one from the upload form does. */
function useRepeatInterview(interviewId: string) {
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
      void navigate({
        to: "/interviews/$interviewId",
        params: { interviewId: next.id },
      })
    },
    onError: (error) => {
      console.error("[app] repeat failed:", error)
    },
  })
}

/** A row whose planning failed. Nothing about it can run, so the one thing
 *  on offer is a fresh plan off the same resume and offer. */
function FailedPanel({ interview }: { interview: Interview }) {
  const repeat = useRepeatInterview(interview.id)
  // A closing that could not be persisted also lands here; it ran, but its
  // transcript cannot be evaluated.
  const ran = interview.closing_id !== null
  return (
    <PageShell center>
      <PageContainer
        variant="narrow"
        className="flex flex-col items-center gap-4 text-center"
      >
        <span className="flex size-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
          <AlertCircleIcon className="size-6" />
        </span>
        <h1 className="text-lg font-medium">
          {ran
            ? "This interview could not be saved"
            : "This interview could not be planned"}
        </h1>
        <p className="text-sm text-muted-foreground">
          {ran
            ? "The interview ended, but its transcript could not be saved completely, so it cannot be evaluated."
            : (interview.ended_reason ??
              "Something went wrong while preparing the questions.")}{" "}
          Repeating it plans a new interview from the same resume and offer.
        </p>
        {repeat.isError && (
          <Alert variant="destructive" className="w-full text-left">
            <AlertDescription>{errorMessage(repeat.error)}</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-col gap-2 @xl/main:flex-row">
          <Button variant="ghost" render={<Link to="/interviews" />}>
            <HistoryIcon />
            History
          </Button>
          <Button
            onClick={() => repeat.mutate(undefined)}
            disabled={repeat.isPending}
          >
            <RotateCcwIcon />
            {repeat.isPending ? "Planning…" : "Repeat this interview"}
          </Button>
        </div>
        <RepeatOptions
          interview={interview}
          pending={repeat.isPending}
          onRepeat={(body) => repeat.mutate(body)}
        />
      </PageContainer>
    </PageShell>
  )
}

/** An `interviewing` row past its reconnect window: the worker died mid-run
 *  and the server has not sealed it yet, so there is no room left to rejoin.
 *  What remains is the transcript up to the cut — evaluate it as it stands,
 *  or plan the interview again. */
function InterruptedPanel({ interview }: { interview: Interview }) {
  const repeat = useRepeatInterview(interview.id)
  const evaluate = useEvaluationRequest(interview.id)
  const busy = evaluate.isPending || repeat.isPending

  return (
    <PageShell center>
      <PageContainer
        variant="narrow"
        className="flex flex-col items-center gap-4 text-center"
      >
        <span className="flex size-12 items-center justify-center rounded-full bg-warning/10 text-warning">
          <PhoneOffIcon className="size-6" />
        </span>
        <h1 className="text-lg font-medium">This interview was interrupted</h1>
        <p className="text-sm text-muted-foreground">
          The connection to the interviewer was lost and the interview can no
          longer be resumed. You can evaluate what was recorded up to that
          point, or repeat it from the start with the same resume and offer.
        </p>
        {(evaluate.isError || repeat.isError) && (
          <Alert variant="destructive" className="w-full text-left">
            <AlertDescription>
              {errorMessage(evaluate.isError ? evaluate.error : repeat.error)}
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-col gap-2 @xl/main:flex-row">
          <Button variant="ghost" render={<Link to="/interviews" />}>
            <HistoryIcon />
            History
          </Button>
          <Button
            variant="outline"
            onClick={() => repeat.mutate(undefined)}
            disabled={busy}
            title="Plan a fresh interview for the same role and resume"
          >
            <RotateCcwIcon />
            {repeat.isPending ? "Planning…" : "Repeat this interview"}
          </Button>
          <Button onClick={() => evaluate.mutate()} disabled={busy}>
            <SparklesIcon />
            {evaluate.isPending ? "Starting…" : "Evaluate what was recorded"}
          </Button>
        </div>
        <RepeatOptions
          interview={interview}
          pending={busy}
          onRepeat={(body) => repeat.mutate(body)}
        />
      </PageContainer>
    </PageShell>
  )
}

function Evaluation({ interview }: { interview: Interview }) {
  const repeat = useRepeatInterview(interview.id)

  const ev = interview.evaluation
  if (!ev) return null

  const milestones = interview.milestones
  const completed = milestones.filter(isSettled).length

  return (
    <PageShell>
      <PageContainer variant="wide">
        <div className="flex flex-col gap-6">
          <AssessmentRequest interview={interview} />
          <InterviewEnding interview={interview} />
          <EvaluationOutcome
            evaluation={ev}
            level={SENIORITY_LABELS[interview.seniority]}
            duration={durationLabel(
              interview.interview_length,
              interview.max_minutes
            )}
            transcriptPartial={interview.transcript_integrity === "partial"}
            captureIntegrityPending={
              interview.capture_integrity_pending ||
              interview.evaluation_invalidated
            }
          />
          <CriterionFeedback evaluation={ev} milestones={milestones} />

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
                      {isSettled(m) ? (
                        <CircleDotIcon className="size-4 shrink-0 text-muted-foreground" />
                      ) : (
                        <ClockIcon className="size-4 shrink-0 text-muted-foreground" />
                      )}
                      <span>{m.title}</span>
                      <TopicStatus milestone={m} />
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

          {/* What the evaluator deliberately did NOT hold against the
              candidate because it sits above the role's level. Empty is the
              normal case — a non-empty list is the audit trail proving the
              calibration filter actually fired. */}
          {ev.calibration_notes.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <GaugeIcon className="size-4 text-muted-foreground" />
                  Not counted against you at this level
                  <CountLabel n={ev.calibration_notes.length} />
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ItemList
                  items={ev.calibration_notes}
                  icon={GaugeIcon}
                  iconClassName="text-muted-foreground"
                  empty=""
                />
              </CardContent>
            </Card>
          )}

          <TranscriptReview interviewId={interview.id} />
          <EvaluationHistory
            interviewId={interview.id}
            milestones={milestones}
            duration={durationLabel(
              interview.interview_length,
              interview.max_minutes
            )}
          />
          <TranscriptCard interviewId={interview.id} />

          {repeat.isError && (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage(repeat.error)}</AlertDescription>
            </Alert>
          )}

          <div className="flex flex-col gap-2 @xl/main:flex-row @xl/main:justify-end">
            <Button variant="ghost" render={<Link to="/interviews" />}>
              <HistoryIcon />
              History
            </Button>
            <Button
              variant="outline"
              onClick={() => repeat.mutate(undefined)}
              disabled={repeat.isPending}
              title="Plan a fresh interview for the same role and resume"
            >
              <RotateCcwIcon />
              {repeat.isPending ? "Planning…" : "Repeat this interview"}
            </Button>
            <Button render={<Link to="/" />}>New interview</Button>
          </div>
          <RepeatOptions
            interview={interview}
            pending={repeat.isPending}
            onRepeat={(body) => repeat.mutate(body)}
          />
        </div>
      </PageContainer>
    </PageShell>
  )
}

/** The stored transcript of a finished interview.
 *
 * Collapsed by default and fetched only once opened: after a live run the
 * candidate just watched every turn go by, and it is the one payload on this
 * screen that grows with the interview's length. */
function TranscriptCard({ interviewId }: { interviewId: string }) {
  const [open, setOpen] = React.useState(false)
  const query = useQuery({
    ...transcriptQueryOptions(interviewId),
    enabled: open,
  })
  const messages = query.data?.messages ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <MessagesSquareIcon className="size-4 text-muted-foreground" />
          Transcript
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto font-normal"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            {open ? "Hide" : "Show"}
            <ChevronDownIcon
              className={cn("transition-transform", open && "rotate-180")}
            />
          </Button>
        </CardTitle>
      </CardHeader>
      {open && (
        <CardContent>
          {query.data?.capture_integrity_pending && (
            <Alert variant="destructive" className="mb-4">
              <AlertDescription>
                This transcript has unresolved capture incidents and requires
                review.
              </AlertDescription>
            </Alert>
          )}
          {query.data?.incidents?.map((incident) => (
            <details key={incident.id} className="mb-3 text-sm">
              <summary>
                Capture incident ·{" "}
                {incident.resolved_at ? "Reviewed" : "Pending review"}
              </summary>
              <p className="whitespace-pre-wrap">{incident.content}</p>
            </details>
          ))}
          {query.isPending ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner />
              Loading the transcript…
            </p>
          ) : query.isError ? (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage(query.error)}</AlertDescription>
            </Alert>
          ) : messages.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nothing was recorded for this interview.
            </p>
          ) : (
            <div className="flex max-h-[28rem] flex-col gap-4 overflow-y-auto pr-1">
              {messages.map((m, i) => {
                const isUser = m.role === "user"
                return (
                  <Message key={m.id ?? i} align={isUser ? "end" : "start"}>
                    <MessageAvatar>
                      <Avatar>
                        <AvatarFallback
                          className={
                            isUser ? undefined : "bg-background text-foreground"
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
                      >
                        <BubbleContent>{m.content}</BubbleContent>
                        {m.version != null && (
                          <p className="text-xs opacity-70">
                            Version {m.version}
                          </p>
                        )}
                        {isUser &&
                          m.metrics &&
                          m.metrics.stt_confirmed !== true && (
                            <p className="text-xs">Unconfirmed transcription</p>
                          )}
                        {isUser &&
                          m.metrics?.stt_segmentation === "unknown" && (
                            <p className="text-xs">
                              Turn boundaries unverified
                            </p>
                          )}
                        {(m.versions?.length ?? 0) > 1 && (
                          <details className="mt-2 text-xs">
                            <summary>Earlier versions</summary>
                            {m.versions
                              ?.filter((v) => v.version !== m.version)
                              .map((v) => (
                                <p
                                  key={v.version}
                                  className="whitespace-pre-wrap"
                                >
                                  Version {v.version}: {v.content}
                                </p>
                              ))}
                          </details>
                        )}
                      </Bubble>
                    </MessageContent>
                  </Message>
                )
              })}
            </div>
          )}
        </CardContent>
      )}
    </Card>
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
