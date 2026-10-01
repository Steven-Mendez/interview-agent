import { RoomEvent, Track } from "livekit-client"
import type { RemoteTrack, Room } from "livekit-client"

import { recordResponseOnset } from "@/lib/api"

// Decoded-audio heuristic: candidate speech end → first interviewer audio.
// It measures what the browser receives, not the speaker or loopback.
const SAMPLE_MS = 50
const SPEECH_LEVEL = 0.02
const AGENT_LEVEL = 0.01
const MIN_SPEECH_MS = 250
const END_SILENCE_MS = 600
const MIN_ONSET_MS = 100
const MAX_WAIT_MS = 60_000

/** Pure state machine fed with RMS levels; returns a sample in seconds. */
export class ResponseOnsetDetector {
  private speechStart: number | null = null
  private quietSince: number | null = null
  private speechEnd: number | null = null
  private agentSince: number | null = null
  private agentBusy = false

  feed(now: number, mic: number, agent: number): number | null {
    const agentLoud = agent >= AGENT_LEVEL
    if (mic >= SPEECH_LEVEL) {
      this.speechStart ??= now
      this.quietSince = null
      // Speaking again before the interviewer answered: not a response gap.
      this.speechEnd = null
    } else if (this.speechStart !== null) {
      this.quietSince ??= now
      if (
        now - this.quietSince >= END_SILENCE_MS &&
        this.quietSince - this.speechStart >= MIN_SPEECH_MS
      ) {
        this.speechEnd = this.quietSince
        this.speechStart = null
        this.quietSince = null
        // Audio already playing when the candidate stopped is not a reply.
        this.agentBusy = agentLoud
      } else if (now - this.quietSince >= END_SILENCE_MS) {
        this.speechStart = null
        this.quietSince = null
      }
    }
    if (!agentLoud) {
      this.agentSince = null
      this.agentBusy = false
      if (this.speechEnd !== null && now - this.speechEnd > MAX_WAIT_MS)
        this.speechEnd = null
      return null
    }
    if (this.agentBusy || this.speechEnd === null) return null
    this.agentSince ??= now
    if (now - this.agentSince < MIN_ONSET_MS) return null
    const seconds = (this.agentSince - this.speechEnd) / 1000
    this.speechEnd = null
    this.agentSince = null
    this.agentBusy = true
    return seconds >= 0 && seconds <= 60 ? seconds : null
  }
}

function level(
  analyser: AnalyserNode | null,
  buffer: Float32Array<ArrayBuffer>
) {
  if (!analyser) return 0
  analyser.getFloatTimeDomainData(buffer)
  let sum = 0
  for (const value of buffer) sum += value * value
  return Math.sqrt(sum / buffer.length)
}

/** Best effort; never touches playback (separate context, no destination). */
export function watchResponseOnset(
  room: Room,
  interviewId: string,
  participantToken: string
): () => void {
  let context: AudioContext
  try {
    context = new AudioContext()
  } catch {
    return () => {}
  }
  const buffer = new Float32Array(1024)
  const detector = new ResponseOnsetDetector()
  let mic: AnalyserNode | null = null
  let agent: AnalyserNode | null = null
  const analyse = (track: MediaStreamTrack) => {
    const analyser = context.createAnalyser()
    analyser.fftSize = 1024
    context.createMediaStreamSource(new MediaStream([track])).connect(analyser)
    return analyser
  }
  const attachMic = () => {
    const track = room.localParticipant.getTrackPublication(
      Track.Source.Microphone
    )?.track?.mediaStreamTrack
    if (track && !mic) mic = analyse(track)
  }
  const onSubscribed = (
    track: RemoteTrack,
    _publication: unknown,
    participant: { isAgent: boolean }
  ) => {
    if (participant.isAgent && track.kind === Track.Kind.Audio)
      agent = analyse(track.mediaStreamTrack)
  }
  room.on(RoomEvent.LocalTrackPublished, attachMic)
  room.on(RoomEvent.TrackSubscribed, onSubscribed)
  attachMic()
  for (const participant of room.remoteParticipants.values())
    for (const publication of participant.audioTrackPublications.values())
      if (participant.isAgent && publication.track)
        agent = analyse(publication.track.mediaStreamTrack)
  const timer = setInterval(() => {
    if (context.state === "suspended") void context.resume().catch(() => {})
    const seconds = detector.feed(
      performance.now(),
      level(mic, buffer),
      level(agent, buffer)
    )
    if (seconds !== null)
      void recordResponseOnset(interviewId, participantToken, {
        sample_id: crypto.randomUUID(),
        seconds,
      }).catch(() => {})
  }, SAMPLE_MS)
  return () => {
    clearInterval(timer)
    room.off(RoomEvent.LocalTrackPublished, attachMic)
    room.off(RoomEvent.TrackSubscribed, onSubscribed)
    void context.close().catch(() => {})
  }
}
