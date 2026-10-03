import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"
import { useSuspenseQuery } from "@tanstack/react-query"
import { RoomAudioRenderer, RoomContext } from "@livekit/components-react"

import { ClosingPanel } from "@/components/closing-panel"
import { ResultsPage } from "@/components/results/results-page"
import { FailedPanel, InterruptedPanel } from "@/components/results/unavailable"
import { FinalizingRoom } from "@/components/session/finalizing"
import { JoiningRoom, LiveRoom } from "@/components/session/live-room"
import { PreJoinRoom } from "@/components/session/prejoin"
import { useDevicePreview } from "@/hooks/use-device-preview"
import { useInterviewSession } from "@/hooks/use-interview-session"
import type { InterviewSession } from "@/hooks/use-interview-session"
import type { Interview } from "@/lib/api"
import { shouldShowInterviewResults } from "@/lib/evaluation"
import { interviewQueryOptions } from "@/lib/queries"
import { pageHead } from "@/lib/head"
import { requireSession } from "@/lib/route-guards"

// The interviewId lives in the URL so deep links / refresh work: the loader
// warms the query cache (which also drives the polling in
// interviewQueryOptions) before the component renders.
export const Route = createFileRoute("/interviews/$interviewId")({
  beforeLoad: ({ location }) => requireSession(location),
  loader: ({ context, params }) =>
    context.queryClient.ensureQueryData(
      interviewQueryOptions(params.interviewId)
    ),
  head: ({ loaderData }) => pageHead(loaderData?.title),
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

/** One route, four places: the preparation room and the live room (both
 *  standalone), the finalizing page right after the call, and the results
 *  inside the application. Which one shows follows the interview's state. */
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

  // Results replace the room once the interview ends: either this tab saw
  // the disconnect (phase 'ended'), or we deep-linked into an already
  // finished interview (phase still 'idle', status terminal).
  const showResults = shouldShowInterviewResults(
    session.phase,
    interview.status
  )

  const content = showResults ? (
    // Straight out of the call, the verdict is still being produced: a calm
    // finalizing page bridges the room and the results.
    session.phase === "ended" && !interview.evaluation ? (
      <FinalizingRoom interview={interview} endedAt={session.endedAt} />
    ) : (
      <ResultsPage interview={interview} endedAt={session.endedAt} />
    )
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
    <SessionRoom session={session} interview={interview} />
  )

  // The provider is always there (its value is null until the room is
  // handed over), so the tree under it never changes shape when the room
  // arrives — the preparation room flows into the live room without a
  // remount. <RoomAudioRenderer> plays the interviewer's track.
  return (
    <RoomContext.Provider value={session.room ?? undefined}>
      {session.room && <RoomAudioRenderer />}
      {content}
    </RoomContext.Provider>
  )
}

/** Preparation through the live call. Owns the device preview: the self-view
 *  carries from the preparation room into the call, and leaving this
 *  component (results, or navigating away) is what releases the camera. */
function SessionRoom({
  session,
  interview,
}: {
  session: InterviewSession
  interview: Interview
}) {
  const preview = useDevicePreview()
  // The pre-join mic button: join muted, the way a call lets you.
  const [micMuted, setMicMuted] = React.useState(false)
  const { phase, start } = session

  // Back in the preparation room after the microphone was handed to LiveKit
  // (the connection dropped mid-interview): the room is gone and its track
  // with it, so the check takes the microphone back. Without this the check
  // stays in its hand-over mode, where opening it captures the camera alone
  // and never reaches "ready", so the camera runs with no self-view showing
  // it. A no-op when nothing was handed over.
  const { reclaimMic } = preview
  React.useEffect(() => {
    if (phase === "idle") reclaimMic()
  }, [phase, reclaimMic])

  if (phase === "live" || phase === "closing") {
    return session.room ? (
      <LiveRoom session={session} interview={interview} preview={preview} />
    ) : (
      <JoiningRoom />
    )
  }

  return (
    <PreJoinRoom
      interview={interview}
      preview={preview}
      error={session.error}
      // An `interviewing` row that can still start is one this candidate
      // left (a closed tab, a dropped line) with the room waiting for them.
      rejoin={interview.status === "interviewing"}
      rejoinUntil={interview.reconnect_until}
      connecting={phase === "connecting"}
      micMuted={micMuted}
      onToggleMic={() => setMicMuted((muted) => !muted)}
      onStart={() =>
        start({
          audioDeviceId: preview.micId || undefined,
          audioOutputDeviceId: preview.speakerId || undefined,
          startMuted: micMuted,
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
