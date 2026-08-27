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
   *  device, so the preview must stop capturing audio first. */
  releaseMic: () => void
  stop: () => void
}

// How often the level meter samples. Per animation frame would re-render the
// panel 60 times a second for a bar that reads the same either way.
const LEVEL_INTERVAL_MS = 100

function messageFor(error: unknown): string {
  if (!(error instanceof Error)) return "Could not open your microphone."
  switch (error.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "Microphone access was blocked. Allow it in the browser's address bar and try again."
    case "NotFoundError":
      return "No microphone found. Connect one and try again."
    case "NotReadableError":
      return "Your microphone is already in use by another app."
    default:
      return error.message || "Could not open your microphone."
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

  const stopMeter = React.useCallback(() => {
    if (meterRef.current) clearInterval(meterRef.current)
    meterRef.current = null
    void audioContextRef.current?.close()
    audioContextRef.current = null
    setLevel(0)
  }, [])

  const stop = React.useCallback(() => {
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
        void context.resume()
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
        try {
          const next = await media.getUserMedia({
            audio: nextMicId ? { deviceId: { exact: nextMicId } } : true,
            video: wantCamera
              ? nextCamId
                ? { deviceId: { exact: nextCamId } }
                : true
              : false,
          })
          // Swap only after the new stream exists: a failed switch leaves the
          // working preview running instead of a black panel.
          streamRef.current?.getTracks().forEach((track) => track.stop())
          streamRef.current = next
          setStream(next)
          startMeter(next)
          setStatus("ready")

          // Labels are only populated once permission is granted, so the
          // device list is worth (re)reading here rather than up front.
          const devices = await media.enumerateDevices()
          setMics(devices.filter((d) => d.kind === "audioinput"))
          setCams(devices.filter((d) => d.kind === "videoinput"))
          setMicId(
            next.getAudioTracks()[0]?.getSettings().deviceId ?? nextMicId
          )
          setCamId(
            next.getVideoTracks()[0]?.getSettings().deviceId ?? nextCamId
          )
        } catch (err) {
          console.error("[app] device preview failed:", err)
          setError(messageFor(err))
          setStatus(
            err instanceof Error && err.name === "NotAllowedError"
              ? "denied"
              : "error"
          )
        }
      })()
    },
    [startMeter]
  )

  const request = React.useCallback(() => {
    open(micId, camId, cameraOn)
  }, [open, micId, camId, cameraOn])

  const selectMic = React.useCallback(
    (id: string) => {
      setMicId(id)
      open(id, camId, cameraOn)
    },
    [open, camId, cameraOn]
  )

  const selectCam = React.useCallback(
    (id: string) => {
      setCamId(id)
      open(micId, id, true)
    },
    [open, micId]
  )

  const toggleCamera = React.useCallback(() => {
    const next = !cameraOn
    setCameraOn(next)
    open(micId, camId, next)
  }, [open, micId, camId, cameraOn])

  const releaseMic = React.useCallback(() => {
    streamRef.current?.getAudioTracks().forEach((track) => track.stop())
    stopMeter()
  }, [stopMeter])

  // Release the camera and the microphone on unmount — navigating away must
  // never leave the recording indicator on.
  React.useEffect(() => {
    return () => {
      if (meterRef.current) clearInterval(meterRef.current)
      void audioContextRef.current?.close()
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
    stop,
  }
}
