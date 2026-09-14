import * as React from "react"

// Local media preview for the pre-join check: pick the microphone you will be
// heard through, see that it actually registers sound, and (optionally) frame
// yourself on camera before the interviewer joins.
//
// Everything here stays in the browser. The interview room only ever carries
// audio — the camera is a self-view for practice, never published — so this
// hook owns its own getUserMedia stream rather than going through LiveKit.

export type PreviewStatus = "idle" | "requesting" | "ready" | "denied" | "error"

export interface DevicePreview {
  status: PreviewStatus
  error: string | null
  mics: MediaDeviceInfo[]
  cams: MediaDeviceInfo[]
  micId: string
  camId: string
  /** True only while the live stream carries a video track. */
  cameraOn: boolean
  /** Smoothed 0..1 input level of the selected microphone. */
  level: number
  /** The live preview stream, for the <video> self-view. */
  stream: MediaStream | null
  /** Ask for permission and open the preview. Safe to call again. */
  request: () => void
  selectMic: (id: string) => void
  selectCam: (id: string) => void
  toggleCamera: () => void
  /** Hand the microphone over: LiveKit opens its own track on the same
   *  device, so the preview must stop capturing audio first. A permission
   *  prompt still pending is cancelled too — its stream would otherwise
   *  land next to LiveKit's and run for the whole interview. */
  releaseMic: () => void
  /** Undo releaseMic when the hand-over fell through (the interview did not
   *  start): reopens the check as it was, so the meter is live again. */
  reclaimMic: () => void
  stop: () => void
}

// How often the level meter samples. Per animation frame would re-render the
// panel 60 times a second for a bar that reads the same either way.
const LEVEL_INTERVAL_MS = 100

type Device = "microphone" | "camera"

const DEVICE_LABEL: Record<Device, string> = {
  microphone: "Microphone",
  camera: "Camera",
}

function messageFor(error: unknown, device: Device): string {
  const fallback = `Could not open your ${device}.`
  if (!(error instanceof Error)) return fallback
  switch (error.name) {
    case "NotAllowedError":
    case "SecurityError":
      return `${DEVICE_LABEL[device]} access was blocked. Allow it in the browser's address bar and try again.`
    case "NotFoundError":
      return `No ${device} found. Connect one and try again.`
    case "NotReadableError":
      return `Your ${device} is already in use by another app.`
    case "OverconstrainedError":
      return `That ${device} is no longer available. Pick another one.`
    default:
      return error.message || fallback
  }
}

export function useDevicePreview(): DevicePreview {
  const [status, setStatus] = React.useState<PreviewStatus>("idle")
  const [error, setError] = React.useState<string | null>(null)
  const [mics, setMics] = React.useState<MediaDeviceInfo[]>([])
  const [cams, setCams] = React.useState<MediaDeviceInfo[]>([])
  const [micId, setMicId] = React.useState("")
  const [camId, setCamId] = React.useState("")
  const [cameraOn, setCameraOn] = React.useState(false)
  const [level, setLevel] = React.useState(0)
  const [stream, setStream] = React.useState<MediaStream | null>(null)

  const streamRef = React.useRef<MediaStream | null>(null)
  const audioContextRef = React.useRef<AudioContext | null>(null)
  const meterRef = React.useRef<ReturnType<typeof setInterval> | null>(null)
  // What the user asked for, as opposed to `cameraOn`, which is what the
  // stream actually carries: a camera that failed to open reads as off, but
  // the next "Turn on" still knows there is nothing to undo.
  const wantCameraRef = React.useRef(false)
  // Every open() takes a new generation; a request still in flight when the
  // generation moves on (a newer pick, a release, unmount) is stale: its
  // stream is stopped the moment it arrives and it touches no state. Without
  // this, two quick mic picks let whichever resolved LAST win, and a prompt
  // accepted after navigating away kept the mic captured in a dead ref.
  const generationRef = React.useRef(0)
  // Set once the hook is torn down: a late reclaimMic (start() failing after
  // the panel is gone) must not open a stream nobody will ever stop.
  const disposedRef = React.useRef(false)
  // Whether releaseMic actually stopped a live audio track — the only case
  // reclaimMic has anything to restore.
  const releasedRef = React.useRef(false)

  const stopMeter = React.useCallback(() => {
    if (meterRef.current) clearInterval(meterRef.current)
    meterRef.current = null
    void audioContextRef.current?.close().catch(() => {})
    audioContextRef.current = null
    setLevel(0)
  }, [])

  const stop = React.useCallback(() => {
    generationRef.current += 1
    releasedRef.current = false
    stopMeter()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    setStream(null)
    setStatus("idle")
  }, [stopMeter])

  // Reads the preview's own audio track. releaseMic() stops that track for
  // LiveKit to reopen, so it tears the meter down in the same breath rather
  // than leaving a bar frozen on its last sample.
  const startMeter = React.useCallback(
    (source: MediaStream) => {
      stopMeter()
      const audioTracks = source.getAudioTracks()
      if (audioTracks.length === 0) return
      const audioTrack = audioTracks[0]
      try {
        const context = new AudioContext()
        audioContextRef.current = context
        void context.resume().catch(() => {})
        const analyser = context.createAnalyser()
        analyser.fftSize = 1024
        context
          .createMediaStreamSource(new MediaStream([audioTrack]))
          .connect(analyser)
        const buffer = new Float32Array(analyser.fftSize)
        meterRef.current = setInterval(() => {
          analyser.getFloatTimeDomainData(buffer)
          let sum = 0
          for (const sample of buffer) sum += sample * sample
          const rms = Math.sqrt(sum / buffer.length)
          // Speech sits far below full scale; ×4 makes normal talking fill
          // most of the bar without clipping every syllable.
          setLevel((previous) => {
            const next = Math.min(rms * 4, 1)
            // Rise fast, fall slow: a bar that tracks each sample looks
            // jittery, one that only smooths looks laggy.
            return next > previous ? next : previous * 0.8 + next * 0.2
          })
        }, LEVEL_INTERVAL_MS)
      } catch {
        // No level meter is a cosmetic loss; the preview itself still works.
      }
    },
    [stopMeter]
  )

  const open = React.useCallback(
    (nextMicId: string, nextCamId: string, wantCamera: boolean) => {
      if (disposedRef.current) return
      const generation = ++generationRef.current
      const stale = () => generation !== generationRef.current
      void (async () => {
        // Typed as always present, but absent in practice on an insecure
        // origin — which is exactly the case worth explaining to the user.
        const media = navigator.mediaDevices as MediaDevices | undefined
        if (!media?.getUserMedia) {
          setStatus("error")
          setError(
            "This browser will not share a microphone over an insecure connection."
          )
          return
        }
        setStatus((current) => (current === "ready" ? current : "requesting"))
        setError(null)

        const audio: MediaTrackConstraints | boolean = nextMicId
          ? { deviceId: { exact: nextMicId } }
          : true
        const video: MediaTrackConstraints | boolean = nextCamId
          ? { deviceId: { exact: nextCamId } }
          : true

        let next: MediaStream
        // Set when the camera failed but the microphone opened: the check
        // stays up on audio and only the camera is reported.
        let cameraError: string | null = null
        try {
          try {
            next = await media.getUserMedia({
              audio,
              video: wantCamera ? video : false,
            })
          } catch (err) {
            if (!wantCamera || stale()) throw err
            // One request, one rejection: which of the two devices failed is
            // not in the error. Asking for audio alone tells them apart and
            // keeps the half of the check that works.
            console.error("[app] camera preview failed:", err)
            cameraError = messageFor(err, "camera")
            next = await media.getUserMedia({ audio })
          }
        } catch (err) {
          if (stale()) return
          console.error("[app] device preview failed:", err)
          setError(messageFor(err, "microphone"))
          setStatus(
            err instanceof Error && err.name === "NotAllowedError"
              ? "denied"
              : "error"
          )
          return
        }
        if (stale()) {
          // Superseded while the prompt was up (a newer pick, a release, or
          // the panel is gone): nobody owns this stream, so stop it here.
          next.getTracks().forEach((track) => track.stop())
          return
        }

        // Swap only after the new stream exists: a failed switch leaves the
        // working preview running instead of a black panel.
        streamRef.current?.getTracks().forEach((track) => track.stop())
        streamRef.current = next
        releasedRef.current = false
        setStream(next)
        startMeter(next)
        setStatus("ready")
        // On only when a video track actually opened — never the intent
        // alone, or a failed camera would keep the self-view (a black box
        // over an audio-only stream) and put video back into every later
        // microphone switch.
        const hasVideo = next.getVideoTracks().length > 0
        setCameraOn(hasVideo)
        if (cameraError) wantCameraRef.current = false
        setError(cameraError)

        // Labels are only populated once permission is granted, so the
        // device list is worth (re)reading here rather than up front.
        const devices = await media.enumerateDevices()
        if (stale()) return
        setMics(devices.filter((d) => d.kind === "audioinput"))
        setCams(devices.filter((d) => d.kind === "videoinput"))
        setMicId(next.getAudioTracks()[0]?.getSettings().deviceId ?? nextMicId)
        // A camera that failed is not worth retrying by default: the next
        // "Turn on" goes back to the system default instead.
        setCamId(
          next.getVideoTracks()[0]?.getSettings().deviceId ??
            (cameraError ? "" : nextCamId)
        )
      })()
    },
    [startMeter]
  )

  const request = React.useCallback(() => {
    open(micId, camId, wantCameraRef.current)
  }, [open, micId, camId])

  const selectMic = React.useCallback(
    (id: string) => {
      setMicId(id)
      open(id, camId, wantCameraRef.current)
    },
    [open, camId]
  )

  const selectCam = React.useCallback(
    (id: string) => {
      setCamId(id)
      wantCameraRef.current = true
      open(micId, id, true)
    },
    [open, micId]
  )

  const toggleCamera = React.useCallback(() => {
    const next = !wantCameraRef.current
    wantCameraRef.current = next
    open(micId, camId, next)
  }, [open, micId, camId])

  const releaseMic = React.useCallback(() => {
    // A prompt still pending would deliver its stream after the hand-over:
    // cancel it, and let the panel offer the check again should the start
    // fall through.
    generationRef.current += 1
    setStatus((current) => (current === "requesting" ? "idle" : current))
    const audioTracks = streamRef.current?.getAudioTracks() ?? []
    releasedRef.current = audioTracks.some(
      (track) => track.readyState === "live"
    )
    audioTracks.forEach((track) => track.stop())
    stopMeter()
  }, [stopMeter])

  const reclaimMic = React.useCallback(() => {
    if (!releasedRef.current) return
    releasedRef.current = false
    open(micId, camId, wantCameraRef.current)
  }, [open, micId, camId])

  // Release the camera and the microphone on unmount — navigating away must
  // never leave the recording indicator on. Bumping the generation covers
  // the prompt still open at that moment: its stream is stopped on arrival.
  React.useEffect(() => {
    disposedRef.current = false
    return () => {
      disposedRef.current = true
      generationRef.current += 1
      if (meterRef.current) clearInterval(meterRef.current)
      meterRef.current = null
      void audioContextRef.current?.close().catch(() => {})
      audioContextRef.current = null
      streamRef.current?.getTracks().forEach((track) => track.stop())
    }
  }, [])

  return {
    status,
    error,
    mics,
    cams,
    micId,
    camId,
    cameraOn,
    level,
    stream,
    request,
    selectMic,
    selectCam,
    toggleCamera,
    releaseMic,
    reclaimMic,
    stop,
  }
}
