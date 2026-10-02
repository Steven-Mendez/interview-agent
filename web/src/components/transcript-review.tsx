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
import { Checkbox } from "@/components/ui/checkbox"
import { Disclosure } from "@/components/ui/disclosure"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"

const DECISION_LABELS: Record<IncidentDecision, string> = {
  duplicate: "Already recorded",
  post_cut: "Spoken after the interview ended",
  omission: "Admitted answer missing from the saved recording",
}

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
    <Disclosure
      summary="Review saved answers"
      onToggle={(event) => setOpen(event.currentTarget.open)}
      bodyClassName="flex flex-col gap-2"
    >
      {open && query.isPending && (
        <p className="flex items-center gap-2 text-muted-foreground">
          <Spinner />
          Loading saved versions…
        </p>
      )}
      {query.isError && (
        <p role="alert" className="text-destructive">
          Saved versions could not be loaded.
        </p>
      )}
      {query.data?.seals.map((seal) => (
        <details
          key={seal.id}
          className="rounded-lg bg-muted px-3 py-2 text-sm"
        >
          <summary className="text-label cursor-pointer py-1">
            Version {seal.version} ·{" "}
            {seal.integrity === "complete"
              ? "Complete recording"
              : "Recording completeness unconfirmed"}
            {seal.invalidated ? " · Requires review" : ""}
          </summary>
          {seal.records.map((record) => (
            <p key={record.id} className="mt-2 whitespace-pre-wrap">
              <strong className="font-medium">
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
        <div className="mt-3 flex flex-col gap-3 border-t pt-4">
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
          <label className="flex gap-3 text-sm">
            <Checkbox
              checked={confirmed}
              disabled={revised.isPending || !!pending.current}
              onCheckedChange={(checked) => setConfirmed(checked === true)}
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
            className="self-start"
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
        <p className="text-muted-foreground">
          No version history was recorded for this interview.
        </p>
      )}
    </Disclosure>
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
    <div className="mt-3 flex flex-col gap-3 border-t pt-4 text-sm">
      <p className="text-label">Capture requiring review</p>
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
          <div className="flex flex-col gap-2">
            <label htmlFor={`decision-${incident.id}`} className="text-label">
              Review decision
            </label>
            <Select
              value={decision}
              disabled={mutation.isPending || !!pending.current}
              onValueChange={(next) => next && setDecision(next)}
            >
              <SelectTrigger id={`decision-${incident.id}`} className="w-full">
                <SelectValue>
                  {(value: IncidentDecision) => DECISION_LABELS[value]}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(DECISION_LABELS) as IncidentDecision[]).map(
                  (value) => (
                    <SelectItem key={value} value={value}>
                      {DECISION_LABELS[value]}
                    </SelectItem>
                  )
                )}
              </SelectContent>
            </Select>
          </div>
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
            className="self-start"
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
