import * as React from "react"
import { Room, RoomEvent, RpcError } from "livekit-client"
import type { TextStreamHandler } from "livekit-client"

import {
  getInterviewToken,
  ApiError,
  suppressUnauthorizedRedirect,
} from "@/lib/api"
import { log } from "@/lib/log"
import { FarewellPlayback } from "@/lib/farewell"
import { requestInterviewEnd } from "@/lib/closing-request"
import { watchResponseOnset } from "@/lib/response-onset"

// Live interview session over LiveKit. Transcript identity comes from the
// SDK segment id; text similarity alone never proves duplicated speech.

export type SessionPhase = "idle" | "connecting" | "live" | "closing" | "ended"
export type Who = "user" | "agent"

// Async SDK callbacks may update this ref while connect()/publish() is pending.
function readSessionPhase(ref: React.RefObject<SessionPhase>): SessionPhase {
  return ref.current
}

// This is an inactivity timeout, refreshed by every chunk, not a maximum
// utterance length. A stalled reader should not stay pending indefinitely.
const TRANSCRIPTION_IDLE_MS = 30_000

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
  /** Output chosen in the pre-join check; omitted = browser default. */
  audioOutputDeviceId?: string
  /** Runs once the room is connected, right before the microphone is
   *  published — the moment for the pre-join check to let go of the device.
   *  Anything that fails before this leaves the check untouched. */
  beforePublish?: () => void
  /** Join with the microphone published but muted — the pre-join choice. */
  startMuted?: boolean
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
  /** The interviewer spoke its fixed technical notice instead of a question:
   *  the candidate may answer again or end. Cleared by the next question. */
  technicalNotice: boolean
  /** The connected Room, exposed for <RoomContext.Provider> + <RoomAudioRenderer>. */
  room: Room | null
  /** `Date.now()` captured on Disconnected — the evaluation-timeout clock starts here. */
  endedAt: number | null
  requestEnd: () => void
  farewellBlocked: boolean
  closingRecoveryPending: boolean
  resumeFarewell: () => void
  syncClosingState: (
    status: string,
    closingId: string | null,
    farewellStatus: string | null,
    transcriptSealed: boolean
  ) => void
}

// Internal per-segment state, minus
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
  const [technicalNotice, setTechnicalNotice] = React.useState(false)
  const [room, setRoom] = React.useState<Room | null>(null)
  const [endedAt, setEndedAt] = React.useState<number | null>(null)
  const [farewellBlocked, setFarewellBlocked] = React.useState(false)
  const [closingRecoveryPending, setClosingRecoveryPending] =
    React.useState(false)
  const farewellRef = React.useRef<FarewellPlayback | null>(null)
  const stopOnsetRef = React.useRef<(() => void) | null>(null)

  // Refs for the transcription bookkeeping — mutated imperatively inside the
  // stream handlers.
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
      // The worker groups user STT sentences until LiveKit confirms a turn.
      // Keep the official topic for the agent's audio-synchronized text.
      const handler =
        (isUserTurn: boolean): TextStreamHandler =>
        async (reader, participantInfo) => {
          if (disposedRef.current || roomRef.current !== r) return
          if (!isUserTurn && participantInfo.identity === "candidate") return
          const attrs = reader.info.attributes ?? {}
          const segmentId = attrs["lk.segment_id"] ?? reader.info.id
          if (!attrs["lk.segment_id"]) {
            console.warn("[app] transcription stream without lk.segment_id")
          }
          const isUser = isUserTurn
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
          const onIdle = () => {
            if (current()) {
              finalizeBubble(seg, true)
              console.warn(
                "[app] transcription stream timed out waiting for data"
              )
            }
            controller.abort()
          }
          let idleTimer = setTimeout(onIdle, TRANSCRIPTION_IDLE_MS)
          const armIdleTimer = () => {
            clearTimeout(idleTimer)
            idleTimer = setTimeout(onIdle, TRANSCRIPTION_IDLE_MS)
          }
          try {
            // User streams each carry the FULL text, agent streams deltas.
            // Accumulate within this reader, replacing this segment's text:
            // unlike readAll(), this preserves received user text on failure.
            let text = ""
            for await (const chunk of reader.withAbortSignal(
              controller.signal
            )) {
              armIdleTimer()
              text += chunk
              if (current()) setBubbleText(seg, text)
            }
            if (!current()) return
            if (isUser && attrs["interview.incomplete"] === "true") {
              finalizeBubble(seg, true)
            } else if (!isUser || attrs["lk.transcription_final"] === "true") {
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
            clearTimeout(idleTimer)
            readersRef.current.delete(controller)
          }
        }
      r.registerTextStreamHandler("lk.transcription", handler(false))
      r.registerTextStreamHandler("interview.user_transcription", handler(true))
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
      if (!participant.isLocal && "interview.notice" in changed)
        setTechnicalNotice(changed["interview.notice"] === "technical")
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
        phaseRef.current = "connecting"
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
            // Interviewer audio and the farewell clip follow the same output.
            ...(options?.audioOutputDeviceId
              ? { audioOutput: { deviceId: options.audioOutputDeviceId } }
              : {}),
          })
          farewellRef.current?.dispose()
          void roomRef.current?.disconnect()
          roomRef.current = r
          registerTranscriptionHandler(r)
          registerAgentStateHandler(r)
          farewellRef.current = new FarewellPlayback(
            r,
            interviewId,
            {
              onClosing: (text, closingId) => {
                if (disposedRef.current) return
                phaseRef.current = "closing"
                setPhase("closing")
                if (text)
                  setBubbleText(
                    bubbleFor(`farewell-${closingId}`, "agent"),
                    text
                  )
              },
              onCompleted: (status) => {
                if (disposedRef.current) return
                phaseRef.current = "ended"
                setEndedAt(Date.now())
                setPhase("ended")
                for (const seg of segmentsRef.current.values()) {
                  if (seg.segmentId.startsWith("farewell-"))
                    finalizeBubble(seg, status !== "played")
                }
                // Sealed: release the microphone even if no worker remains to
                // close the room (a server-reconciled end).
                stopOnsetRef.current?.()
                stopOnsetRef.current = null
                cancelTranscriptions()
                void roomRef.current?.disconnect()
              },
              onBlocked: setFarewellBlocked,
              onError: setError,
              onRecoveryPending: () => setClosingRecoveryPending(true),
            },
            token
          )
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
            log("disconnected from room (reason:", reason, ")")
            setAgentState(null)
            // Closing is reconciled against saved backend state; a transport
            // loss alone never proves that the farewell finished playing.
            if (
              phaseRef.current !== "closing" &&
              phaseRef.current !== "ended"
            ) {
              phaseRef.current = "idle"
              setPhase("idle")
              setRoom(null)
              setError(
                "The connection was lost. Rejoin to continue your interview."
              )
            }
          })

          await r.connect(server_url, token)
          // Only now, with a room to publish into, does the pre-join check
          // let go of the microphone: a 409/429 on the token or a failed
          // connect returns to a check that is still running.
          handedOver = true
          options?.beforePublish?.()
          if (
            !farewellRef.current.isClosing &&
            !farewellRef.current.isFinished
          ) {
            await r.localParticipant.setMicrophoneEnabled(true)
            // Muted in the pre-join check: published, so unmuting in the
            // room is instant, but silent until the candidate says so.
            if (options?.startMuted)
              await r.localParticipant.setMicrophoneEnabled(false)
          }
          if (link.lost) {
            throw new Error(
              "The connection dropped before the interview started."
            )
          }
          link.live = true
          stopOnsetRef.current?.()
          stopOnsetRef.current = watchResponseOnset(r, interviewId, token)
          log("connected, microphone enabled")
          // Expose the room only now: <RoomAudioRenderer> mounts after the mic
          // gesture and still picks up the agent's (later) audio track.
          setRoom(r)
          if (readSessionPhase(phaseRef) === "connecting") {
            phaseRef.current = "live"
            setPhase("live")
          }
        } catch (err) {
          console.error("[app] could not start the interview:", err)
          setError(errorMessage(err))
          roomRef.current = null
          farewellRef.current?.dispose()
          farewellRef.current = null
          // A room that did connect (the microphone failed after) would
          // otherwise stay joined — agent dispatched, nobody left to hang
          // up — behind a panel that says idle. A no-op on a room that
          // never connected.
          void r?.disconnect()
          phaseRef.current = "idle"
          setPhase("idle")
          if (handedOver && !disposedRef.current) options?.onPublishFailed?.()
        }
      })()
    },
    [
      interviewId,
      registerTranscriptionHandler,
      registerAgentStateHandler,
      bubbleFor,
      setBubbleText,
      finalizeBubble,
      cancelTranscriptions,
    ]
  )

  const requestEnd = React.useCallback(() => {
    const r = roomRef.current
    if (!r || phaseRef.current !== "live") return
    const agent = [...r.remoteParticipants.values()].find(
      (participant) => participant.isAgent
    )
    if (!agent) {
      setError("The interviewer is unavailable. Please wait for reconnection.")
      return
    }
    phaseRef.current = "closing"
    setPhase("closing")
    farewellRef.current?.superviseClosing()
    void requestInterviewEnd(r.localParticipant, agent.identity).catch(
      (rpcError: unknown) => {
        if (disposedRef.current || phaseRef.current === "ended") return
        setError(errorMessage(rpcError))
        if (
          rpcError instanceof RpcError &&
          (rpcError.code === RpcError.ErrorCode.UNSUPPORTED_METHOD ||
            rpcError.code === RpcError.ErrorCode.RECIPIENT_NOT_FOUND) &&
          farewellRef.current?.cancelUnacceptedClose()
        ) {
          phaseRef.current = "live"
          setPhase("live")
        }
        // A transport timeout can lose the reply after the worker acquired
        // closing. Keep the independent API supervision and its fixed deadline.
      }
    )
  }, [])

  const resumeFarewell = React.useCallback(() => {
    void farewellRef.current?.resume()
  }, [])
  const syncClosingState = React.useCallback(
    (
      status: string,
      closingId: string | null,
      farewellStatus: string | null,
      transcriptSealed: boolean
    ) => {
      farewellRef.current?.acceptPersistedState(
        status,
        closingId,
        farewellStatus,
        transcriptSealed
      )
    },
    []
  )

  // A refused session mid-interview must not navigate away from the room:
  // its requests fail on their own, and the interview goes on.
  const inRoom =
    phase === "connecting" || phase === "live" || phase === "closing"
  React.useEffect(() => {
    if (!inRoom) return
    return suppressUnauthorizedRedirect()
  }, [inRoom])

  // Tear the room down on real unmount (navigation away). start() is
  // click-driven, so StrictMode's mount/unmount/mount cycle runs before any
  // room exists and this cleanup is a no-op there.
  React.useEffect(() => {
    disposedRef.current = false
    return () => {
      disposedRef.current = true
      stopOnsetRef.current?.()
      cancelTranscriptions()
      farewellRef.current?.dispose()
      void roomRef.current?.disconnect()
    }
  }, [cancelTranscriptions])

  return {
    phase,
    start,
    error,
    messages,
    agentState,
    technicalNotice,
    room,
    endedAt,
    requestEnd,
    farewellBlocked,
    closingRecoveryPending,
    resumeFarewell,
    syncClosingState,
  }
}
