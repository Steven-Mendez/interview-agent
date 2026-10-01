import * as React from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { LENGTH_LABELS } from "@/lib/api"
import type { Interview, InterviewLength, RepeatRequest } from "@/lib/api"

/** Same body the backend contract defines: a changed duration recalculates
 *  the profile limits first, then the typed overrides given here apply. */
export function repeatBody(
  interview: Pick<Interview, "interview_length">,
  length: InterviewLength,
  questions: string,
  followups: string
): RepeatRequest {
  const body: RepeatRequest = {}
  if (length !== interview.interview_length) body.interview_length = length
  if (questions.trim()) body.question_limit = Number(questions)
  if (followups.trim()) body.followup_limit = Number(followups)
  return body
}

/** Optional changes for a repeat; leaving everything as-is inherits. */
export function RepeatOptions({
  interview,
  pending,
  onRepeat,
}: {
  interview: Interview
  pending: boolean
  onRepeat: (body: RepeatRequest) => void
}) {
  const [length, setLength] = React.useState<InterviewLength>(
    interview.interview_length
  )
  const [questions, setQuestions] = React.useState("")
  const [followups, setFollowups] = React.useState("")
  const invalid =
    (questions.trim() !== "" &&
      !(
        Number.isInteger(Number(questions)) &&
        Number(questions) >= 1 &&
        Number(questions) <= 12
      )) ||
    (followups.trim() !== "" &&
      !(
        Number.isInteger(Number(followups)) &&
        Number(followups) >= 0 &&
        Number(followups) <= 2
      ))
  return (
    <details className="w-full rounded-xl border p-3 text-left text-sm">
      <summary className="cursor-pointer">Repeat with changes…</summary>
      <form
        className="mt-3 grid gap-3 @xl/main:grid-cols-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (!invalid)
            onRepeat(repeatBody(interview, length, questions, followups))
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Duration</span>
          <select
            className="h-9 rounded-md border bg-background px-2"
            value={length}
            onChange={(event) =>
              setLength(event.target.value as InterviewLength)
            }
          >
            {(Object.keys(LENGTH_LABELS) as InterviewLength[]).map((value) => (
              <option key={value} value={value}>
                {LENGTH_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            Main questions (1–12, blank = profile)
          </span>
          <Input
            inputMode="numeric"
            value={questions}
            onChange={(event) => setQuestions(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            Follow-ups per topic (0–2, blank = profile)
          </span>
          <Input
            inputMode="numeric"
            value={followups}
            onChange={(event) => setFollowups(event.target.value)}
          />
        </label>
        <p className="text-xs text-muted-foreground @xl/main:col-span-2">
          Same duration keeps the saved limits unless you change them; a new
          duration recalculates them first. The final values are shown before
          you start.
        </p>
        <Button type="submit" disabled={pending || invalid}>
          {pending ? "Planning…" : "Repeat with these settings"}
        </Button>
      </form>
    </details>
  )
}
