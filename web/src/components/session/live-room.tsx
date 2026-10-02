import * as React from "react"
import {
  BarVisualizer,
  useIsSpeaking,
  useLocalParticipant,
  useTrackToggle,
  useTrackVolume,
  useVoiceAssistant,
} from "@livekit/components-react"
import { Track } from "livekit-client"
import {
  AlertCircleIcon,
  CaptionsIcon,
  CaptionsOffIcon,
  EllipsisVerticalIcon,
  LayoutPanelLeftIcon,
  MessageSquareTextIcon,
  MicIcon,
  MicOffIcon,
  PhoneIcon,
  RotateCcwIcon,
  UserIcon,
  VideoIcon,
  VideoOffIcon,
} from "lucide-react"

import { useImmersive } from "@/components/app-shell"
import { InterviewAudioRecovery } from "@/components/interview-audio-recovery"
import { InterviewTimer } from "@/components/interview-timer"
import { Mascot, MascotHead } from "@/components/mascot"
import { useCurrentQuestion } from "@/components/question-recovery"
import {
  ParticipantTile,
  SelfView,
  TileAvatar,
  TileBadge,
} from "@/components/session/media"
import { TranscriptPanel } from "@/components/session/transcript-panel"
import { agentMascotState, useDarkRoom } from "@/components/session/room"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogClose,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { IconButton } from "@/components/ui/icon-button"
import { LinkButton } from "@/components/ui/link-button"
import { Spinner } from "@/components/ui/spinner"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { DevicePreview } from "@/hooks/use-device-preview"
import type {
  ChatMessage,
  InterviewSession,
} from "@/hooks/use-interview-session"
import type { Interview } from "@/lib/api"
import { cn } from "@/lib/utils"

/** Spotlight keeps the candidate on the stage with the interviewer in a
 *  floating tile; the others are on request. */
type Layout = "spotlight" | "side-by-side" | "interviewer"

const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform)
const MOD = isMac ? "⌘" : "Ctrl"

/** A dark stage while the room is being joined (the interview started but
 *  the room is not handed to the UI yet). */
export function JoiningRoom() {
  useImmersive()
  useDarkRoom()
  return (
    <div className="flex h-svh flex-col items-center justify-center gap-5 bg-stage text-foreground">
      <Mascot state="waiting" className="w-36" />
      <div className="flex items-center gap-3">
        <Spinner className="size-5" />
        <p role="status" className="text-title">
          Joining the interview…
        </p>
      </div>
    </div>
  )
}

/** The live interview: a dark call room centered on the candidate, the
 *  Interviewer Agent as a participant tile, live captions, one floating
 *  control bar and the transcript on demand. The interview plan stays off
 *  screen — a real interviewer does not show their checklist.
 *  Must render inside RoomContext. */
export function LiveRoom({
  session,
  interview,
  preview,
}: {
  session: InterviewSession
  interview: Interview
  preview: DevicePreview
}) {
  useImmersive()
  useDarkRoom()

  const { phase } = session
  const live = phase === "live"
  const agentName = interview.interviewer?.agent_name || "Interviewer"
  const question = useCurrentQuestion(interview.id)
  const [transcriptOpen, setTranscriptOpen] = React.useState(false)
  const [captions, setCaptions] = React.useState(true)
  const [layout, setLayout] = React.useState<Layout>("spotlight")
  const [confirmEnd, setConfirmEnd] = React.useState(false)
  const mic = useTrackToggle({ source: Track.Source.Microphone })

  // Familiar call shortcuts: mic and camera.
  const { toggle: toggleMic } = mic
  const { toggleCamera } = preview
  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return
      const key = event.key.toLowerCase()
      if (key === "d") {
        event.preventDefault()
        void toggleMic()
      } else if (key === "e") {
        event.preventDefault()
        toggleCamera()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [toggleMic, toggleCamera])

  const self = (className: string) => (
    <SelfTile
      key="self"
      preview={preview}
      micEnabled={mic.enabled}
      className={className}
    />
  )
  const agent = (className: string, compact: boolean) => (
    <InterviewerTile key="agent" compact={compact} className={className} />
  )
  const floating =
    "absolute right-3 bottom-3 z-10 aspect-video w-[clamp(9rem,24%,17rem)] shadow-[0_4px_16px_rgb(0_0_0/0.45)] ring-1 ring-white/10"

  return (
    <div className="flex h-svh flex-col overflow-hidden bg-stage text-foreground">
      <div className="relative flex min-h-0 flex-1 gap-4 px-4 pt-4">
        {/* Stage */}
        <div className="relative min-w-0 flex-1">
          {layout === "side-by-side" ? (
            <div className="grid h-full grid-rows-2 gap-3 sm:grid-cols-2 sm:grid-rows-none sm:content-center sm:items-center">
              {self("h-full sm:h-auto sm:aspect-video")}
              {agent("h-full sm:h-auto sm:aspect-video", false)}
            </div>
          ) : layout === "interviewer" ? (
            <>
              {agent("absolute inset-0", false)}
              {self(floating)}
            </>
          ) : (
            <>
              {self("absolute inset-0")}
              {agent(floating, true)}
            </>
          )}

          <div className="pointer-events-none absolute inset-x-3 top-3 z-20 flex flex-col items-center gap-2 [&>*]:pointer-events-auto">
            <InterviewAudioRecovery />
            {live && question.interrupted && (
              <RoomBanner icon={<RotateCcwIcon className="text-primary" />}>
                <span>
                  {question.failed
                    ? "The replay request could not be confirmed. You can retry."
                    : "The question was cut short — you may not have heard all of it."}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!question.canReplay}
                  onClick={question.replay}
                >
                  {question.requested ? "Replay requested…" : "Listen again"}
                </Button>
              </RoomBanner>
            )}
            {live && session.technicalNotice && (
              <RoomBanner icon={<AlertCircleIcon className="text-warning" />}>
                <span>
                  The interviewer had a technical difficulty. Answer again by
                  speaking, or end the interview.
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setConfirmEnd(true)}
                >
                  End interview
                </Button>
              </RoomBanner>
            )}
            {phase === "closing" && (
              <RoomBanner icon={<Spinner className="size-5" />}>
                <span role="status">
                  {session.closingRecoveryPending
                    ? "Closing recovery is pending. The server keeps saving the interview without this page."
                    : `Wrapping up — ${agentName} is saying goodbye while your transcript is saved.`}
                </span>
                {session.farewellBlocked && (
                  <Button size="sm" onClick={session.resumeFarewell}>
                    Enable farewell audio
                  </Button>
                )}
                {session.closingRecoveryPending && (
                  <LinkButton variant="ghost" size="sm" to="/interviews">
                    View saved interviews
                  </LinkButton>
                )}
              </RoomBanner>
            )}
            {session.error && (
              <RoomBanner
                icon={<AlertCircleIcon className="text-destructive" />}
              >
                <span role="alert">{session.error}</span>
              </RoomBanner>
            )}
            {live && !mic.enabled && (
              <RoomBanner icon={<MicOffIcon className="text-destructive" />}>
                <span>
                  Your microphone is off — the interviewer can’t hear you.
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void mic.toggle()}
                >
                  Turn on
                </Button>
              </RoomBanner>
            )}
          </div>

          {captions && (
            <Captions
              messages={session.messages}
              agentState={session.agentState}
              agentName={agentName}
              live={live}
              besideTile={layout !== "side-by-side"}
            />
          )}
        </div>

        {transcriptOpen && (
          <TranscriptPanel
            session={session}
            agentName={agentName}
            onClose={() => setTranscriptOpen(false)}
          />
        )}
      </div>

      {/* Bottom: time and title on the left, the call controls in the
          middle, the transcript on the right — the call layout people
          know. */}
      <footer className="grid h-20 shrink-0 grid-cols-[1fr_auto_1fr] items-center gap-3 px-4">
        <div className="flex min-w-0 items-center gap-3 font-heading text-base">
          {live ? (
            <InterviewTimer elapsedSeconds={interview.elapsed_seconds} />
          ) : (
            <span className="flex items-center gap-2 text-sm">
              <Spinner className="size-4" />
              Wrapping up
            </span>
          )}
          <span aria-hidden className="hidden h-5 w-px bg-border md:block" />
          <h1 className="hidden truncate text-sm font-normal md:block">
            {interview.title}
          </h1>
        </div>

        <div
          role="toolbar"
          aria-label="Call controls"
          className="flex items-center gap-2 sm:gap-3"
        >
          <CallControl
            label={mic.enabled ? "Turn off microphone" : "Turn on microphone"}
            shortcut={`${MOD} D`}
            off={!mic.enabled}
            disabled={mic.pending}
            onClick={() => void mic.toggle()}
          >
            {mic.enabled ? <MicIcon /> : <MicOffIcon />}
          </CallControl>
          <CallControl
            label={preview.cameraOn ? "Turn off camera" : "Turn on camera"}
            shortcut={`${MOD} E`}
            off={!preview.cameraOn}
            onClick={preview.toggleCamera}
          >
            {preview.cameraOn ? <VideoIcon /> : <VideoOffIcon />}
          </CallControl>
          <CallControl
            label={captions ? "Turn off captions" : "Turn on captions"}
            active={captions}
            aria-pressed={captions}
            onClick={() => setCaptions((value) => !value)}
            className="hidden sm:inline-flex"
          >
            {captions ? <CaptionsIcon /> : <CaptionsOffIcon />}
          </CallControl>
          <CallControl
            label={
              question.requested
                ? "Replay requested…"
                : "Listen to the question again"
            }
            disabled={!live || !question.canReplay}
            onClick={question.replay}
            className="hidden sm:inline-flex"
          >
            <RotateCcwIcon />
          </CallControl>
          <MoreMenu
            layout={layout}
            onLayout={setLayout}
            onTranscript={() => setTranscriptOpen(true)}
            captions={captions}
            onCaptions={() => setCaptions((value) => !value)}
            question={question}
            live={live}
          />
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="destructive"
                  aria-label="End interview"
                  disabled={!live}
                  onClick={() => setConfirmEnd(true)}
                  className="h-12 w-[4.5rem] px-0 [&_svg]:size-6"
                >
                  <PhoneIcon className="rotate-[135deg]" />
                </Button>
              }
            />
            <TooltipContent side="top">End interview</TooltipContent>
          </Tooltip>
        </div>

        <div className="hidden items-center justify-end md:flex">
          <PanelToggle
            label="Transcript"
            open={transcriptOpen}
            onClick={() => setTranscriptOpen((open) => !open)}
          >
            <MessageSquareTextIcon />
          </PanelToggle>
        </div>
      </footer>

      <EndInterviewDialog
        open={confirmEnd}
        onOpenChange={setConfirmEnd}
        agentName={agentName}
        onConfirm={() => {
          setConfirmEnd(false)
          session.requestEnd()
        }}
      />
    </div>
  )
}

// ---- Participants ---------------------------------------------------------

const AGENT_STATE_TEXT: Record<string, string> = {
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
}

/** The Interviewer Agent as a participant: the character in its live state,
 *  the active-speaker ring while it talks, and its audio level. */
function InterviewerTile({
  compact,
  className,
}: {
  compact: boolean
  className?: string
}) {
  const { state, audioTrack } = useVoiceAssistant()
  const volume = useTrackVolume(audioTrack)
  const speaking = state === "speaking"
  const stateText = AGENT_STATE_TEXT[state] ?? "Connecting"
  return (
    <ParticipantTile
      name="Interviewer Agent"
      speaking={speaking}
      tint="var(--primary-container)"
      className={className}
      nameAdornment={<MascotHead className="size-4" />}
      badges={
        <TileBadge
          label={speaking ? "The interviewer is speaking" : "Interviewer audio"}
          className={cn(speaking && "bg-primary text-primary-foreground")}
        >
          <BarVisualizer
            state={state}
            track={audioTrack}
            barCount={3}
            className="flex h-3.5 items-center gap-[2px]"
          >
            <span className="h-full w-[3px] origin-center scale-y-[0.3] rounded-full bg-current opacity-80 transition-transform data-[lk-highlighted=true]:scale-y-100" />
          </BarVisualizer>
        </TileBadge>
      }
    >
      <div
        className={cn(
          "flex size-full flex-col items-center justify-center gap-1",
          compact ? "pt-1 pb-6" : "pt-2 pb-8"
        )}
      >
        <Mascot
          state={agentMascotState(state)}
          level={speaking ? volume : undefined}
          className={cn("min-h-0", compact ? "h-[88%]" : "h-[62%] max-h-80")}
        />
        {!compact && <p className="text-sm text-white/80">{stateText}</p>}
      </div>
      <span aria-live="polite" className="sr-only">
        Interviewer: {stateText}
      </span>
    </ParticipantTile>
  )
}

function SelfTile({
  preview,
  micEnabled,
  className,
}: {
  preview: DevicePreview
  micEnabled: boolean
  className?: string
}) {
  const { localParticipant } = useLocalParticipant()
  const speaking = useIsSpeaking(localParticipant) && micEnabled
  const showVideo = preview.cameraOn && preview.stream
  const cameraStarting =
    preview.cameraWanted && !preview.cameraOn && !preview.error
  return (
    <ParticipantTile
      name="You"
      speaking={speaking}
      className={className}
      badges={
        !micEnabled && (
          <TileBadge label="Your microphone is off">
            <MicOffIcon />
          </TileBadge>
        )
      }
    >
      {showVideo && preview.stream ? (
        <SelfView
          stream={preview.stream}
          className="absolute inset-0 size-full"
        />
      ) : cameraStarting ? (
        <Spinner className="size-7" />
      ) : (
        <TileAvatar tone="self" speaking={speaking}>
          <UserIcon />
        </TileAvatar>
      )}
      {preview.error && !preview.cameraOn && (
        <p
          role="alert"
          className="absolute inset-x-3 top-3 rounded-md bg-black/60 px-3 py-2 text-xs"
        >
          {preview.error}
        </p>
      )}
    </ParticipantTile>
  )
}

// ---- Captions -------------------------------------------------------------

function Captions({
  messages,
  agentState,
  agentName,
  live,
  besideTile,
}: {
  messages: ChatMessage[]
  agentState: string | null
  agentName: string
  live: boolean
  /** A floating tile sits bottom-right: keep the captions clear of it. */
  besideTile: boolean
}) {
  const last = messages.at(-1)
  const thinking = live && agentState === "thinking" && last?.who !== "agent"
  if (!last && !live) return null
  return (
    <div
      className={cn(
        "pointer-events-none absolute inset-x-0 bottom-4 z-20 flex justify-center px-4",
        besideTile &&
          "max-sm:bottom-[6.5rem] sm:inset-x-[calc(clamp(9rem,24%,17rem)+1rem)]"
      )}
    >
      <div className="w-[min(46rem,100%)] rounded-lg bg-black/75 px-4 py-3 text-white">
        {!last ? (
          <p className="text-sm text-white/85">
            Connected — the interviewer will greet you in a few seconds. Speak
            into your microphone.
          </p>
        ) : (
          <>
            <p className="mb-0.5 flex items-center gap-1.5 text-xs font-medium text-white/70">
              {last.who === "agent" && <MascotHead className="size-4" />}
              {last.who === "agent" ? agentName : "You"}
            </p>
            {/* Live captions show the words being said, not the start of
                the turn: the box holds the last few lines and the text is
                pinned to its bottom, so new words push older ones out the
                top. The whole turn stays in the transcript panel. */}
            <div className="flex max-h-10 flex-col justify-end overflow-hidden sm:max-h-[4.5rem]">
              <p
                className={cn(
                  "text-sm leading-5 sm:text-base sm:leading-6",
                  last.interim && "text-white/80"
                )}
              >
                {last.text}
              </p>
            </div>
          </>
        )}
        {thinking && (
          <p className="mt-1 shimmer text-xs">{agentName} is thinking…</p>
        )}
      </div>
    </div>
  )
}

// ---- Controls -------------------------------------------------------------

function CallControl({
  off = false,
  active = false,
  className,
  ...props
}: React.ComponentProps<typeof IconButton> & {
  /** A device that is switched off — the call convention's red. */
  off?: boolean
  /** A view toggle that is on. */
  active?: boolean
}) {
  return (
    <IconButton
      size="icon-lg"
      tooltipSide="top"
      className={cn(
        "bg-secondary text-foreground hover:text-foreground disabled:bg-secondary disabled:text-foreground/35",
        off &&
          "bg-on-destructive-container text-destructive-container hover:text-destructive-container",
        active &&
          "bg-primary-container text-on-primary-container hover:text-on-primary-container",
        className
      )}
      {...props}
    />
  )
}

/** The panel toggle: quiet until its panel is open. */
function PanelToggle({
  label,
  open,
  onClick,
  children,
}: {
  label: string
  open: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <IconButton
      label={open ? `Hide ${label.toLowerCase()}` : label}
      tooltipSide="top"
      aria-pressed={open}
      onClick={onClick}
      className={cn(
        "size-12 text-foreground hover:text-foreground [&_svg]:size-6",
        open &&
          "bg-primary-container text-on-primary-container hover:text-on-primary-container"
      )}
    >
      {children}
    </IconButton>
  )
}

function MoreMenu({
  layout,
  onLayout,
  onTranscript,
  captions,
  onCaptions,
  question,
  live,
}: {
  layout: Layout
  onLayout: (layout: Layout) => void
  onTranscript: () => void
  captions: boolean
  onCaptions: () => void
  question: ReturnType<typeof useCurrentQuestion>
  live: boolean
}) {
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger
          render={
            <DropdownMenuTrigger
              render={
                <Button
                  size="icon-lg"
                  variant="quiet"
                  aria-label="More options"
                  className="bg-secondary text-foreground hover:text-foreground"
                >
                  <EllipsisVerticalIcon />
                </Button>
              }
            />
          }
        />
        <TooltipContent side="top">More options</TooltipContent>
      </Tooltip>
      <DropdownMenuContent side="top" align="center" sideOffset={12}>
        <DropdownMenuGroup>
          <DropdownMenuLabel>Layout</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={layout}
            onValueChange={(value) => onLayout(value as Layout)}
          >
            <DropdownMenuRadioItem value="spotlight">
              <LayoutPanelLeftIcon />
              Spotlight on you
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="side-by-side">
              <LayoutPanelLeftIcon className="rotate-90" />
              Side by side
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="interviewer">
              <LayoutPanelLeftIcon className="-scale-x-100" />
              Spotlight on the interviewer
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        {/* What the bar hides on narrow windows. */}
        <DropdownMenuItem onClick={onCaptions} className="sm:hidden">
          {captions ? <CaptionsOffIcon /> : <CaptionsIcon />}
          {captions ? "Turn off captions" : "Turn on captions"}
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!live || !question.canReplay}
          onClick={question.replay}
          className="sm:hidden"
        >
          <RotateCcwIcon />
          Listen to the question again
        </DropdownMenuItem>
        <DropdownMenuItem onClick={onTranscript} className="md:hidden">
          <MessageSquareTextIcon />
          Transcript
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function RoomBanner({
  icon,
  children,
}: {
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="flex w-fit max-w-xl animate-in items-center gap-3 rounded-lg bg-popover py-2 pr-2 pl-4 text-sm text-popover-foreground shadow-e2 duration-200 fade-in slide-in-from-top-2 [&_svg]:size-5 [&_svg]:shrink-0">
      {icon}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {children}
      </div>
    </div>
  )
}

function EndInterviewDialog({
  open,
  onOpenChange,
  onConfirm,
  agentName,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: () => void
  agentName: string
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogTitle>End interview?</AlertDialogTitle>
        <AlertDialogDescription render={<div />}>
          <ul className="flex list-disc flex-col gap-1.5 pl-5">
            <li>{agentName} says goodbye and the live session stops.</li>
            <li>Everything said so far is saved to the transcript.</li>
            <li>
              The evaluation starts automatically — your results open here when
              it is ready.
            </li>
          </ul>
        </AlertDialogDescription>
        <DialogFooter>
          <AlertDialogClose render={<Button variant="ghost" />}>
            Cancel
          </AlertDialogClose>
          <Button variant="destructive" onClick={onConfirm}>
            End interview
          </Button>
        </DialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
