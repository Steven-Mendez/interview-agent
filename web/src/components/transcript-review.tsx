import { useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  createReviewedSnapshot,
  evaluateInterview,
  getSealHistory,
  reviewCaptureIncident,
} from "@/lib/api"
import type { IncidentDecision, SealHistory } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"

export function TranscriptReview({ interviewId }: { interviewId: string }) {
  const [open, setOpen] = useState(false)
  const [reviewer, setReviewer] = useState("")
  const [rationale, setRationale] = useState("")
  const [confirmed, setConfirmed] = useState(false)
  const pending = useRef<
    | (Parameters<typeof createReviewedSnapshot>[1] & { request_id: string })
    | null
  >(null)
  const client = useQueryClient()
  const query = useQuery({
    queryKey: ["transcript-review", interviewId],
    queryFn: () => getSealHistory(interviewId),
    enabled: open,
  })
  const refresh = async () => {
    await client.invalidateQueries()
  }
  const current = query.data?.seals.find(
    (seal) => seal.id === query.data.current_seal_id
  )
  const omitted =
    query.data?.incidents.filter(
      (item) =>
        item.review?.decision === "omission" &&
        !current?.provenance.incorporated_incident_ids?.includes(item.id)
    ) ?? []
  const revised = useMutation({
    mutationFn: async () => {
      if (!current) throw new Error("No saved transcript is available")
      pending.current ??= {
        seal_id: crypto.randomUUID(),
        request_id: crypto.randomUUID(),
        parent_id: current.id,
        incident_ids: omitted.map((item) => item.id),
        reviewer,
        rationale,
        confirm_complete: confirmed,
      }
      const { request_id, ...body } = pending.current
      await createReviewedSnapshot(interviewId, body)
      await evaluateInterview(interviewId, request_id)
    },
    onSuccess: async () => {
      pending.current = null
      await refresh()
    },
  })
  return (
    <details
      className="rounded-xl border p-4"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer font-medium">
        Review saved answers
      </summary>
      {open && query.isPending && <p>Loading saved versions…</p>}
      {query.isError && <p role="alert">Saved versions could not be loaded.</p>}
      {query.data?.seals.map((seal) => (
        <details key={seal.id} className="mt-3 text-sm">
          <summary>
            Version {seal.version} ·{" "}
            {seal.integrity === "complete"
              ? "Complete recording"
              : "Recording completeness unconfirmed"}
            {seal.invalidated ? " · Requires review" : ""}
          </summary>
          {seal.records.map((record) => (
            <p key={record.id} className="mt-2 whitespace-pre-wrap">
              <strong>
                {record.role === "user" ? "Candidate" : "Interviewer"}:
              </strong>{" "}
              {record.content}
            </p>
          ))}
        </details>
      ))}
      {query.data?.incidents.map((incident) => (
        <IncidentReview
          key={incident.id}
          interviewId={interviewId}
          incident={incident}
          refresh={refresh}
        />
      ))}
      {(omitted.length > 0 || pending.current) && (
        <div className="mt-5 flex flex-col gap-3 border-t pt-4">
          <p className="text-sm">
            Confirmed missing answers keep the earlier score hidden. A revised
            assessment includes them and preserves the earlier history.
          </p>
          <Input
            aria-label="Reviewer for revised assessment"
            placeholder="Your name"
            value={reviewer}
            disabled={revised.isPending || !!pending.current}
            onChange={(event) => setReviewer(event.target.value)}
          />
          <Textarea
            aria-label="Reason for revised assessment"
            placeholder="What did you check?"
            value={rationale}
            disabled={revised.isPending || !!pending.current}
            onChange={(event) => setRationale(event.target.value)}
          />
          <label className="flex gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={revised.isPending || !!pending.current}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            I checked all audio admitted before the interview ended and confirm
            these saved answers are complete.
          </label>
          <p className="text-xs text-muted-foreground">
            Leave this unchecked if completeness is uncertain. Feedback will
            then have no overall score.
          </p>
          {revised.isError && <p role="alert">{revised.error.message}</p>}
          <Button
            disabled={
              revised.isPending || !reviewer.trim() || !rationale.trim()
            }
            onClick={() => revised.mutate()}
          >
            {pending.current && revised.isError
              ? "Retry revised assessment"
              : "Create revised assessment"}
          </Button>
        </div>
      )}
      {query.data?.seals.length === 0 && (
        <p className="mt-3 text-sm">
          No version history was recorded for this interview.
        </p>
      )}
    </details>
  )
}

function IncidentReview({
  interviewId,
  incident,
  refresh,
}: {
  interviewId: string
  incident: SealHistory["incidents"][number]
  refresh: () => Promise<void>
}) {
  const [decision, setDecision] = useState<IncidentDecision>("duplicate")
  const [reviewer, setReviewer] = useState("")
  const [rationale, setRationale] = useState("")
  const pending = useRef<Parameters<typeof reviewCaptureIncident>[2] | null>(
    null
  )
  const mutation = useMutation({
    mutationFn: () => {
      pending.current ??= {
        review_id: crypto.randomUUID(),
        decision,
        rationale,
        reviewer,
      }
      return reviewCaptureIncident(interviewId, incident.id, pending.current)
    },
    onSuccess: refresh,
  })
  return (
    <div className="mt-4 flex flex-col gap-3 border-t pt-4 text-sm">
      <p className="font-medium">Capture requiring review</p>
      <p className="whitespace-pre-wrap">{incident.content}</p>
      {incident.review ? (
        <p>
          {incident.review.decision === "omission"
            ? "Confirmed missing answer"
            : "Excluded after review"}{" "}
          · {incident.review.reviewer}: {incident.review.rationale}
        </p>
      ) : (
        <>
          <label>
            Review decision
            <select
              className="ml-3 rounded border p-2"
              value={decision}
              disabled={mutation.isPending || !!pending.current}
              onChange={(event) =>
                setDecision(event.target.value as IncidentDecision)
              }
            >
              <option value="duplicate">Already recorded</option>
              <option value="post_cut">Spoken after the interview ended</option>
              <option value="omission">
                Admitted answer missing from the saved recording
              </option>
            </select>
          </label>
          <Input
            aria-label="Reviewer"
            placeholder="Your name"
            value={reviewer}
            disabled={mutation.isPending || !!pending.current}
            onChange={(event) => setReviewer(event.target.value)}
          />
          <Textarea
            aria-label="Reason for review"
            placeholder="Explain what you checked in the audio and timing."
            value={rationale}
            disabled={mutation.isPending || !!pending.current}
            onChange={(event) => setRationale(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            The decision is retained. A confirmed missing answer permanently
            invalidates the earlier overall score.
          </p>
          {mutation.isError && <p role="alert">{mutation.error.message}</p>}
          <Button
            disabled={
              mutation.isPending || !reviewer.trim() || !rationale.trim()
            }
            onClick={() => mutation.mutate()}
          >
            Save review
          </Button>
        </>
      )}
    </div>
  )
}
