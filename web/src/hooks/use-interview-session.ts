import * as React from "react"
import { Room, RoomEvent } from "livekit-client"

import { getInterviewToken, ApiError } from "@/lib/api"
import { log } from "@/lib/log"

// Live interview session over LiveKit. Transcript identity comes from the
// SDK segment id; text similarity alone never proves duplicated speech.

export type SessionPhase = "idle" | "connecting" | "live" | "ended"
export type Who = "user" | "agent"

/** One rendered chat bubble. `interim` = STT not finalized yet (opacity-70). */
export interface ChatMessage {
  segmentId: string
  who: Who
  text: string
  interim: boolean
  incomplete: boolean
}

/** Options for `start`. The device id comes from the pre-join check, so the
 *  interview is published through the microphone the candidate just tested. */
export interface StartOptions {
  audioDeviceId?: string
  /** Runs once the room is connected, right before the microphone is
   *  published — the moment for the pre-join check to let go of the device.
   *  Anything that fails before this leaves the check untouched. */
  beforePublish?: () => void
  /** Runs when start() fails AFTER beforePublish ran: the microphone was let
   *  go of for nothing, so the check can take it back. */
  onPublishFailed?: () => void
}

export interface InterviewSession {
  phase: SessionPhase
  /** Call DIRECTLY from an onClick — mic permission + audio autoplay need the gesture. */
  start: (options?: StartOptions) => void
  error: string | null
  messages: ChatMessage[]
  /** Raw `lk.agent.state` (initializing/listening/thinking/speaking) or null. */
  agentState: string | null
  /** The connected Room, exposed for <RoomContext.Provider> + <RoomAudioRenderer>. */
  room: Room | null
  /** `Date.now()` captured on Disconnected — the evaluation-timeout clock starts here. */
  endedAt: number | null
}

// Internal per-segment state. Mirrors app.js's `segments` Map values, minus
// the DOM node: `rendered` replaces `seg.el` (a segment only enters the render
// list once it has non-empty text).
interface Segment {
  segmentId: string
  who: Who
  text: string
  interim: boolean
  incomplete: boolean
  confirmed: boolean
  version: number
  rendered: boolean
  timer: ReturnType<typeof setTimeout> | null
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

export function useInterviewSession(interviewId: string): InterviewSession {
  const [phase, setPhase] = React.useState<SessionPhase>("idle")
  const [error, setError] = React.useState<string | null>(null)
  const [messages, setMessages] = React.useState<ChatMessage[]>([])
  const [agentState, setAgentState] = React.useState<string | null>(null)
  const [room, setRoom] = React.useState<Room | null>(null)
  const [endedAt, setEndedAt] = React.useState<number | null>(null)

  // Refs for the transcription bookkeeping — mutated imperatively inside the
  // stream handlers, exactly like the module-level state in app.js.
  const segmentsRef = React.useRef<Map<string, Segment>>(new Map())
  const readersRef = React.useRef(new Map<AbortController, Segment>())
  const orderRef = React.useRef<string[]>([]) // render order of segment ids
  const phaseRef = React.useRef<SessionPhase>("idle")
  const roomRef = React.useRef<Room | null>(null)
  // Set on unmount so a start() still in flight stops short of joining a
  // room (or reclaiming a microphone) for a page that is gone.
  const disposedRef = React.useRef(false)

  phaseRef.current = phase

  // Rebuild the render list from the ordered, rendered segments and publish it.
  const commit = React.useCallback(() => {
    if (disposedRef.current) return
    const next: ChatMessage[] = []
    for (const id of orderRef.current) {
      const seg = segmentsRef.current.get(id)
      if (!seg || !seg.rendered) continue
      next.push({
        segmentId: seg.segmentId,
        who: seg.who,
        text: seg.text,
        interim: seg.interim,
        incomplete: seg.incomplete,
      })
    }
    setMessages(next)
  }, [])

  const bubbleFor = React.useCallback(
    (segmentId: string, who: Who): Segment => {
      let seg = segmentsRef.current.get(segmentId)
      if (!seg) {
        seg = {
          segmentId,
          who,
          text: "",
          interim: true,
          incomplete: false,
          confirmed: false,
          version: 0,
          rendered: false,
          timer: null,
        }
        segmentsRef.current.set(segmentId, seg)
      }
      return seg
    },
    []
  )

  // Bubbles are created lazily on first text: preemptive generations that get
  // discarded (or empty STT streams) would otherwise leave ghost bubbles.
  const setBubbleText = React.useCallback(
    (seg: Segment, text: string) => {
      if (!text) return
      if (!seg.rendered) {
        seg.rendered = true
        orderRef.current.push(seg.segmentId)
      }
      seg.text = text
      seg.interim = true
      seg.incomplete = false
      commit()
    },
    [commit]
  )

  const finalizeBubble = React.useCallback(
    (seg: Segment, incomplete = false) => {
      if (seg.timer) clearTimeout(seg.timer)
      seg.timer = null
      if (!seg.rendered) {
        segmentsRef.current.delete(seg.segmentId)
        return
      }
      seg.interim = false
      seg.incomplete = incomplete
      seg.confirmed = !incomplete
      commit()
    },
    [commit]
  )

  const cancelTranscriptions = React.useCallback(() => {
    for (const [controller, seg] of readersRef.current) {
      // Settle cancelled reads here, since the callback deliberately ignores
      // cancelled reads. A newer/confirmed version must not be downgraded.
      controller.abort()
      if (!seg.confirmed) finalizeBubble(seg, true)
    }
    readersRef.current.clear()
    for (const seg of segmentsRef.current.values()) {
      if (seg.timer || seg.interim) finalizeBubble(seg, true)
    }
  }, [finalizeBubble])

  const registerTranscriptionHandler = React.useCallback(
    (r: Room) => {
      // A brief Offline/Reconnecting event can resume the existing stream.
      // A full restart removes remote participants; loss of the interviewer
      // invalidates both user and agent transcription streams it produced.
      r.on(RoomEvent.ParticipantDisconnected, cancelTranscriptions)
      r.on(RoomEvent.Disconnected, cancelTranscriptions)
      // One handler per topic per Room, registered before connect.
      r.registerTextStreamHandler(
        "lk.transcription",
        async (reader, participantInfo) => {
          if (disposedRef.current || roomRef.current !== r) return
          const attrs = reader.info.attributes ?? {}
          const segmentId = attrs["lk.segment_id"] ?? reader.info.id
          if (!attrs["lk.segment_id"]) {
            console.warn("[app] transcription stream without lk.segment_id")
          }
          const isUser = participantInfo.identity === "candidate"
          const seg = bubbleFor(segmentId, isUser ? "user" : "agent")
          const controller = new AbortController()
          readersRef.current.set(controller, seg)
          // A confirmed final is immutable. Still drain a late stream so its
          // reader can finish normally without creating another bubble.
          const version = seg.confirmed ? seg.version : ++seg.version
          if (seg.timer) clearTimeout(seg.timer)
          seg.timer = null
          const current = () =>
            !disposedRef.current &&
            !controller.signal.aborted &&
            roomRef.current === r &&
            seg.version === version &&
            !seg.confirmed &&
            segmentsRef.current.get(segmentId) === seg
          try {
            // User streams each carry the FULL text, agent streams deltas.
            // Accumulate within this reader, replacing this segment's text:
            // unlike readAll(), this preserves received user text on failure.
            let text = ""
            for await (const chunk of reader.withAbortSignal(
              controller.signal
            )) {
              text += chunk
              if (current()) setBubbleText(seg, text)
            }
            if (!current()) return
            if (!isUser || attrs["lk.transcription_final"] === "true") {
              finalizeBubble(seg)
            } else if (seg.rendered) {
              seg.timer = setTimeout(() => {
                if (current()) finalizeBubble(seg, true)
              }, 3000)
            } else {
              segmentsRef.current.delete(segmentId)
            }
          } catch {
            if (current()) {
              finalizeBubble(seg, true)
              console.warn("[app] transcription stream ended before completion")
            }
          } finally {
            readersRef.current.delete(controller)
          }
        }
      )
    },
    [bubbleFor, setBubbleText, finalizeBubble, cancelTranscriptions]
  )

  const registerAgentStateHandler = React.useCallback((r: Room) => {
    // The agent publishes its lifecycle in the `lk.agent.state` participant
    // attribute; mirroring it fills the dead air while the LLM thinks.
    r.on(RoomEvent.ParticipantAttributesChanged, (changed, participant) => {
      if (!participant.isLocal && "lk.agent.state" in changed) {
        log("agent state:", changed["lk.agent.state"])
        setAgentState(changed["lk.agent.state"])
      }
    })
    r.on(RoomEvent.ParticipantConnected, (participant) => {
      const state = participant.attributes["lk.agent.state"]
      if (state) setAgentState(state)
    })
  }, [])

  const start = React.useCallback(
    (options?: StartOptions) => {
      // Start from a user action. Microphone acquisition also lets LiveKit
      // attempt audio playback; the UI offers recovery if it stays blocked.
      if (phaseRef.current !== "idle") return

      void (async () => {
        setPhase("connecting")
        setError(null)
        let r: Room | null = null
        let handedOver = false
        try {
          const {
            server_url,
            token,
            room: roomName,
          } = await getInterviewToken(interviewId)
          // Navigated away while the token was in flight: the unmount
          // cleanup ran before there was a room to disconnect, so creating
          // one now would join it for nobody.
          if (disposedRef.current) return
          log("token received, connecting to room:", roomName)

          r = new Room({
            // Cleaner mic input = better STT = better interview.
            audioCaptureDefaults: {
              // The device chosen in the pre-join check; omitted falls back to
              // the browser's default input.
              deviceId: options?.audioDeviceId || undefined,
              echoCancellation: true,
              noiseSuppression: true,
              autoGainControl: true,
            },
            publishDefaults: { dtx: true },
          })
          roomRef.current = r
          registerTranscriptionHandler(r)
          registerAgentStateHandler(r)
          // Until start() hands the room to the UI, a disconnect is a start
          // failure and the catch below owns the phase — including the
          // disconnect the catch itself issues. Flipping to "ended" here
          // would show "Evaluating…" for an interview that never began.
          const link = { live: false, lost: false }
          r.on(RoomEvent.Disconnected, (reason) => {
            if (disposedRef.current) return
            if (!link.live) {
              link.lost = true
              return
            }
            // Interview over (agent deleted the room, or connection lost).
            log(
              "disconnected from room (reason:",
              reason,
              ") — showing results"
            )
            setAgentState(null)
            setEndedAt(Date.now())
            setPhase("ended")
          })

          await r.connect(server_url, token)
          // Only now, with a room to publish into, does the pre-join check
          // let go of the microphone: a 409/429 on the token or a failed
          // connect returns to a check that is still running.
          handedOver = true
          options?.beforePublish?.()
          await r.localParticipant.setMicrophoneEnabled(true)
          if (link.lost) {
            throw new Error(
              "The connection dropped before the interview started."
            )
          }
          link.live = true
          log("connected, microphone enabled")
          // Expose the room only now: <RoomAudioRenderer> mounts after the mic
          // gesture and still picks up the agent's (later) audio track.
          setRoom(r)
          setPhase("live")
        } catch (err) {
          console.error("[app] could not start the interview:", err)
          setError(errorMessage(err))
          roomRef.current = null
          // A room that did connect (the microphone failed after) would
          // otherwise stay joined — agent dispatched, nobody left to hang
          // up — behind a panel that says idle. A no-op on a room that
          // never connected.
          void r?.disconnect()
          setPhase("idle")
          if (handedOver && !disposedRef.current) options?.onPublishFailed?.()
        }
      })()
    },
    [interviewId, registerTranscriptionHandler, registerAgentStateHandler]
  )

  // Tear the room down on real unmount (navigation away). start() is
  // click-driven, so StrictMode's mount/unmount/mount cycle runs before any
  // room exists and this cleanup is a no-op there.
  React.useEffect(() => {
    disposedRef.current = false
    return () => {
      disposedRef.current = true
      cancelTranscriptions()
      void roomRef.current?.disconnect()
    }
  }, [cancelTranscriptions])

  return { phase, start, error, messages, agentState, room, endedAt }
}
