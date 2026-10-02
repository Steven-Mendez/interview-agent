import * as React from "react"
import {
  AlertCircleIcon,
  MicIcon,
  MicOffIcon,
  VideoIcon,
  VideoOffIcon,
  Volume2Icon,
} from "lucide-react"

import { useImmersive } from "@/components/app-shell"
import { BrandLink } from "@/components/brand"
import {
  DeviceMenu,
  DevicePill,
  SelfView,
  playTestChime,
} from "@/components/session/media"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { Spinner } from "@/components/ui/spinner"
import type { DevicePreview } from "@/hooks/use-device-preview"
import type { Interview } from "@/lib/api"
import { cn } from "@/lib/utils"

const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "short" })

/** The preparation room, the way a call's pre-join screen works: the
 *  browser asks for the camera and microphone as the page opens; the
 *  preview carries its own mic and camera buttons, the device pickers sit
 *  under it, and the right side holds one clear Start.
 *
 *  `rejoin` turns Start into Rejoin: the interview is already running and
 *  its room waits for the candidate — until `rejoinUntil` (ISO), when the
 *  backend says so. */
export function PreJoinRoom({
  interview,
  preview,
  error,
  rejoin,
  rejoinUntil,
  connecting,
  micMuted,
  onToggleMic,
  onStart,
}: {
  interview: Interview
  preview: DevicePreview
  error: string | null
  rejoin: boolean
  rejoinUntil: string | null
  connecting: boolean
  micMuted: boolean
  onToggleMic: () => void
  onStart: () => void
}) {
  useImmersive()
  const rejoinDeadline = rejoinUntil === null ? NaN : Date.parse(rejoinUntil)
  const verb = rejoin ? "Rejoin" : "Start"

  // Ask once, on arrival — camera and microphone in a single prompt. The
  // request goes out on the next task, so StrictMode's development
  // mount/unmount/mount cancels the first schedule instead of a prompt
  // already in flight; `asked` is set only once it really fires.
  const { status, request } = preview
  const asked = React.useRef(false)
  React.useEffect(() => {
    if (asked.current || status !== "idle") return
    const timer = setTimeout(() => {
      asked.current = true
      request(true)
    })
    return () => clearTimeout(timer)
  }, [status, request])

  return (
    <div className="flex min-h-svh flex-col bg-background">
      <header className="flex h-16 shrink-0 items-center px-4 md:px-6">
        <BrandLink />
      </header>

      <div className="mx-auto grid w-full max-w-7xl flex-1 content-center items-center gap-8 px-4 pb-10 md:px-8 lg:grid-cols-[minmax(0,1.6fr)_minmax(18rem,1fr)] lg:gap-14">
        <section aria-label="Your camera and microphone" className="min-w-0">
          <PreviewTile
            preview={preview}
            micMuted={micMuted}
            onToggleMic={onToggleMic}
          />
          <DeviceBar preview={preview} />
        </section>

        <section
          aria-labelledby="prejoin-title"
          className="flex min-w-0 flex-col items-center gap-6 text-center"
        >
          <div className="flex flex-col gap-4">
            <h1 id="prejoin-title" className="text-headline">
              {rejoin ? "Pick up where you left off" : "Ready to interview?"}
            </h1>
            <p className="line-clamp-2 font-heading text-sm font-medium">
              {interview.title}
            </p>
          </div>

          {error && (
            <Alert variant="destructive" className="text-left">
              <AlertCircleIcon />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {/* Never gated on the check: a candidate whose browser hides the
              devices must still be able to start. */}
          <Button
            size="lg"
            onClick={onStart}
            disabled={connecting}
            className="h-14 w-60 max-w-full text-sm"
          >
            {connecting ? (
              <>
                <Spinner className="text-current" />
                Joining…
              </>
            ) : (
              `${verb} interview`
            )}
          </Button>
          {/* The worker may be waking up: say so, or the wait reads as a
              hang. */}
          {connecting && (
            <div role="status" className="flex flex-col gap-1">
              <p className="text-sm font-medium">
                Connecting to the interviewer…
              </p>
              <p className="text-xs text-muted-foreground">
                The first connection can take 10–20 seconds.
              </p>
            </div>
          )}
          {rejoin &&
            rejoinUntil !== null &&
            Number.isFinite(rejoinDeadline) && (
              <p className="text-xs text-muted-foreground">
                The interviewer waits until{" "}
                <time dateTime={rejoinUntil}>
                  {timeFormat.format(rejoinDeadline)}
                </time>
                .
              </p>
            )}
        </section>
      </div>
    </div>
  )
}

/** The big preview: the camera when on, otherwise one line on the dark
 *  tile. Its own mic and camera buttons sit at the bottom, as in a call. */
function PreviewTile({
  preview,
  micMuted,
  onToggleMic,
}: {
  preview: DevicePreview
  micMuted: boolean
  onToggleMic: () => void
}) {
  const { status } = preview
  const ready = status === "ready"
  const blocked = status === "denied" || status === "error"
  const waiting = status === "idle" || status === "requesting"
  const showVideo = ready && preview.cameraOn && preview.stream
  const cameraStarting =
    ready && preview.cameraWanted && !preview.cameraOn && !preview.error
  const cameraFailed = ready && !!preview.error && !preview.cameraOn

  return (
    // The tile is a dark surface in either theme, so it takes the dark tokens.
    <div className="session-dark relative aspect-video w-full overflow-hidden rounded-xl bg-stage text-foreground">
      {showVideo && preview.stream ? (
        <SelfView stream={preview.stream} className="size-full" />
      ) : (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 pb-16 text-center">
          {waiting ? (
            status === "requesting" && (
              <p className="max-w-sm text-sm text-muted-foreground">
                Allow access to your camera and microphone in the browser’s
                prompt.
              </p>
            )
          ) : blocked ? (
            <>
              <p className="text-title-lg">
                {status === "denied"
                  ? "Microphone blocked"
                  : "Microphone unavailable"}
              </p>
              <p className="max-w-md text-sm text-muted-foreground">
                {preview.error}
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => preview.request(true)}
                className="mt-1 text-foreground"
              >
                Try again
              </Button>
            </>
          ) : cameraStarting ? (
            <Spinner className="size-8" />
          ) : (
            <p className="text-title-lg font-normal">
              {cameraFailed ? "Camera unavailable" : "Your camera is off"}
            </p>
          )}
        </div>
      )}

      <span className="absolute top-4 left-4 font-heading text-sm font-medium text-white [text-shadow:0_1px_2px_rgb(0_0_0/0.6)]">
        You
      </span>

      <div className="absolute inset-x-0 bottom-0 flex items-center justify-center gap-4 bg-linear-to-t from-black/40 to-transparent px-4 pt-10 pb-4">
        <TileControl
          label={
            blocked
              ? "Microphone blocked — try again"
              : micMuted
                ? "Turn on microphone"
                : "Turn off microphone"
          }
          off={micMuted || blocked}
          attention={waiting || blocked}
          disabled={status === "requesting"}
          onClick={blocked ? () => preview.request(true) : onToggleMic}
        >
          {micMuted || blocked ? <MicOffIcon /> : <MicIcon />}
        </TileControl>
        <TileControl
          label={preview.cameraOn ? "Turn off camera" : "Turn on camera"}
          off={!preview.cameraOn && !waiting}
          attention={waiting || cameraFailed}
          disabled={!ready}
          onClick={preview.toggleCamera}
        >
          {preview.cameraOn || waiting ? <VideoIcon /> : <VideoOffIcon />}
        </TileControl>
      </div>

      {cameraFailed && preview.error && (
        <p
          role="alert"
          className="absolute top-3 right-3 left-16 rounded-md bg-black/60 px-3 py-2 text-xs text-white"
        >
          {preview.error}
        </p>
      )}
    </div>
  )
}

/** A control on the preview, as a call's pre-join draws it: a light pill
 *  when on, pink when off,
 *  an amber "!" while the browser has not granted the device. */
function TileControl({
  off,
  attention,
  children,
  className,
  ...props
}: React.ComponentProps<typeof IconButton> & {
  off: boolean
  attention: boolean
}) {
  return (
    <span className="relative">
      <IconButton
        size="icon-lg"
        tooltipSide="top"
        className={cn(
          "h-12 w-14 [&_svg]:size-6",
          off
            ? "bg-[#f9dedc] text-[#8c1d18] hover:text-[#8c1d18] disabled:bg-[#f9dedc]/70"
            : "bg-[#dde3ea] text-[#1f1f1f] hover:text-[#1f1f1f] disabled:bg-[#dde3ea]/70 disabled:text-[#1f1f1f]/60",
          className
        )}
        {...props}
      >
        {children}
      </IconButton>
      {attention && (
        <span
          aria-hidden
          className="pointer-events-none absolute -top-0.5 -right-0.5 flex size-4 items-center justify-center rounded-full bg-[#f9ab00] text-[11px] leading-none font-bold text-[#3d2a00]"
        >
          !
        </span>
      )}
    </span>
  )
}

/** The device pickers under the preview, as in a call. */
function DeviceBar({ preview }: { preview: DevicePreview }) {
  const ready = preview.status === "ready"
  return (
    <div className="mt-5 grid grid-cols-1 gap-2 sm:grid-cols-3">
      <DeviceMenu
        kind="Microphone"
        icon={MicIcon}
        devices={preview.mics}
        value={preview.micId}
        onChange={preview.selectMic}
        fallbackLabel="Microphone"
        disabled={!ready}
        className="w-full"
        footer={<MicLevelRow level={preview.level} />}
      />
      {preview.outputSelectable && preview.speakers.length > 0 ? (
        <DeviceMenu
          kind="Speaker"
          icon={Volume2Icon}
          devices={preview.speakers}
          value={preview.speakerId}
          onChange={preview.selectSpeaker}
          fallbackLabel="Speaker"
          className="w-full"
          action={{
            label: "Test speakers",
            icon: Volume2Icon,
            onSelect: () => void playTestChime(preview.speakerId),
          }}
        />
      ) : (
        <DevicePill
          icon={Volume2Icon}
          label="System speaker"
          description="This browser plays the interview through its default audio output; it cannot be changed here."
        />
      )}
      <DeviceMenu
        kind="Camera"
        icon={VideoIcon}
        devices={preview.cams}
        value={preview.camId}
        onChange={preview.selectCam}
        fallbackLabel="Camera"
        disabled={!ready}
        className="w-full"
      />
    </div>
  )
}

/** The microphone menu's last row: its live level, so a pick can be
 *  checked without leaving the menu. */
function MicLevelRow({ level }: { level: number }) {
  return (
    <div className="flex h-10 items-center gap-4 px-4">
      <MicIcon className="size-5 shrink-0" />
      <span
        role="meter"
        aria-label="Microphone level"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(level * 100)}
        className="relative h-1.5 w-full overflow-hidden rounded-full bg-foreground/10"
      >
        <span
          className="absolute inset-y-0 left-0 rounded-full bg-primary transition-[width] duration-100"
          style={{ width: `${Math.round(level * 100)}%` }}
        />
      </span>
    </div>
  )
}
