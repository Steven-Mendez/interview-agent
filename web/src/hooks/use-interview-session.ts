import * as React from "react"
import { Room, RoomEvent } from "livekit-client"

import { getInterviewToken, ApiError } from "@/lib/api"
import { log } from "@/lib/log"

// Live interview session over LiveKit — a faithful port of the transcription
// and connection logic in `frontend/app.js` (lines 226-366), folded into one
// hook. The dedupe rules are load-bearing and tuned against livekit-client
// 2.20.0; keep them literal.

export type SessionPhase = "idle" | "connecting" | "live" | "ended"
export type Who = "user" | "agent"

/** One rendered chat bubble. `interim` = STT not finalized yet (opacity-70). */
export interface ChatMessage {
  segmentId: string
  who: Who
  text: string
  interim: boolean
}

export interface InterviewSession {
  phase: SessionPhase
  /** Call DIRECTLY from an onClick — mic permission + audio autoplay need the gesture. */
  start: () => void
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
  const lastSegmentRef = React.useRef<Partial<Record<Who, Segment>>>({})
  const orderRef = React.useRef<string[]>([]) // render order of segment ids
  const phaseRef = React.useRef<SessionPhase>("idle")
  const roomRef = React.useRef<Room | null>(null)

  phaseRef.current = phase

  // Rebuild the render list from the ordered, rendered segments and publish it.
  const commit = React.useCallback(() => {
    const next: ChatMessage[] = []
    for (const id of orderRef.current) {
      const seg = segmentsRef.current.get(id)
      if (!seg || !seg.rendered) continue
      next.push({
        segmentId: seg.segmentId,
        who: seg.who,
        text: seg.text,
        interim: seg.interim,
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
        lastSegmentRef.current[seg.who] = seg
      }
      seg.text = text
      commit()
    },
    [commit]
  )

  const finalizeBubble = React.useCallback(
    (seg: Segment) => {
      if (seg.timer) {
        clearTimeout(seg.timer)
        seg.timer = null
      }
      if (!seg.rendered) return
      // Supersede dedupe: if the previous bubble from the same speaker never
      // finalized and one text is a prefix of the other, it was a discarded
      // partial of this same utterance — drop it.
      const prev = lastSegmentRef.current[seg.who]
      if (prev && prev !== seg && prev.rendered && prev.interim) {
        if (seg.text.startsWith(prev.text) || prev.text.startsWith(seg.text)) {
          orderRef.current = orderRef.current.filter(
            (id) => id !== prev.segmentId
          )
          segmentsRef.current.delete(prev.segmentId)
        }
      }
      seg.interim = false
      lastSegmentRef.current[seg.who] = seg
      commit()
    },
    [commit]
  )

  const registerTranscriptionHandler = React.useCallback(
    (r: Room) => {
      // One handler per topic per Room (a second registration throws) — this
      // is why we register imperatively inside start(), never in a useEffect
      // (StrictMode would double-mount and throw).
      r.registerTextStreamHandler(
        "lk.transcription",
        async (reader, participantInfo) => {
          const attrs = reader.info.attributes ?? {}
          const segmentId = attrs["lk.segment_id"] ?? reader.info.id
          if (!attrs["lk.segment_id"]) {
            console.warn("[app] transcription stream without lk.segment_id")
          }
          const isUser = participantInfo.identity === "candidate"
          const seg = bubbleFor(segmentId, isUser ? "user" : "agent")

          if (isUser) {
            // User STT: each interim is a separate stream carrying the FULL text
            // so far under the same segment id — replace the bubble text in place.
            const text = await reader.readAll()
            setBubbleText(seg, text)
            if (attrs["lk.transcription_final"] === "true") {
              finalizeBubble(seg)
            } else {
              // Safety net: if the final version never arrives (it occasionally
              // doesn't), solidify the interim after 3s without updates.
              if (seg.timer) clearTimeout(seg.timer)
              seg.timer = setTimeout(() => finalizeBubble(seg), 3000)
            }
          } else {
            // Agent speech: one delta stream per segment, word-synced with the
            // audio playback — accumulate chunks; stream close = segment done
            // (works for interruptions too; the trailer "final" attribute isn't
            // reliably surfaced by the JS SDK).
            for await (const chunk of reader) {
              setBubbleText(seg, seg.text + chunk)
            }
            if (seg.rendered) finalizeBubble(seg)
            else segmentsRef.current.delete(segmentId) // empty stream: never render it
          }
        }
      )
    },
    [bubbleFor, setBubbleText, finalizeBubble]
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

  const start = React.useCallback(() => {
    // Runs inside the click handler: mic permission + audio autoplay need the
    // user gesture. No state update between here and setMicrophoneEnabled that
    // could re-render before the permission prompt (phase→connecting is the
    // only one, and it just swaps the button for a status line).
    if (phaseRef.current !== "idle") return

    void (async () => {
      setPhase("connecting")
      setError(null)
      try {
        const {
          server_url,
          token,
          room: roomName,
        } = await getInterviewToken(interviewId)
        log("token received, connecting to room:", roomName)

        const r = new Room({
          // Cleaner mic input = better STT = better interview.
          audioCaptureDefaults: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          publishDefaults: { dtx: true },
        })
        roomRef.current = r
        registerTranscriptionHandler(r)
        registerAgentStateHandler(r)
        r.on(RoomEvent.Disconnected, (reason) => {
          // Interview over (agent deleted the room, or connection lost).
          log("disconnected from room (reason:", reason, ") — showing results")
          setAgentState(null)
          setEndedAt(Date.now())
          setPhase("ended")
        })

        await r.connect(server_url, token)
        await r.localParticipant.setMicrophoneEnabled(true)
        log("connected, microphone enabled")
        // Expose the room only now: <RoomAudioRenderer> mounts after the mic
        // gesture and still picks up the agent's (later) audio track.
        setRoom(r)
        setPhase("live")
      } catch (err) {
        console.error("[app] could not start the interview:", err)
        setError(errorMessage(err))
        roomRef.current = null
        setPhase("idle")
      }
    })()
  }, [interviewId, registerTranscriptionHandler, registerAgentStateHandler])

  // Tear the room down on real unmount (navigation away). start() is
  // click-driven, so StrictMode's mount/unmount/mount cycle runs before any
  // room exists and this cleanup is a no-op there.
  React.useEffect(() => {
    return () => {
      void roomRef.current?.disconnect()
    }
  }, [])

  return { phase, start, error, messages, agentState, room, endedAt }
}
