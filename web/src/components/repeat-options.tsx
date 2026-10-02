import * as React from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
  footer,
}: {
  interview: Interview
  pending: boolean
  onRepeat: (body: RepeatRequest) => void
  /** Extra actions next to the submit button (e.g. a dialog's Cancel). */
  footer?: React.ReactNode
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
    <form
      className="flex flex-col gap-5 text-left"
      onSubmit={(event) => {
        event.preventDefault()
        if (!invalid)
          onRepeat(repeatBody(interview, length, questions, followups))
      }}
    >
      <Field>
        <FieldLabel htmlFor="repeat-length">Duration</FieldLabel>
        <Select
          value={length}
          onValueChange={(next) => next && setLength(next)}
        >
          <SelectTrigger id="repeat-length" className="w-full">
            <SelectValue>
              {(value: InterviewLength) => LENGTH_LABELS[value]}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {(Object.keys(LENGTH_LABELS) as InterviewLength[]).map((value) => (
              <SelectItem key={value} value={value}>
                {LENGTH_LABELS[value]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="repeat-questions">
            Main questions (1–12, blank = profile)
          </FieldLabel>
          <Input
            id="repeat-questions"
            inputMode="numeric"
            value={questions}
            aria-invalid={
              questions.trim() !== "" &&
              !(Number(questions) >= 1 && Number(questions) <= 12)
            }
            onChange={(event) => setQuestions(event.target.value)}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor="repeat-followups">
            Follow-ups per topic (0–2, blank = profile)
          </FieldLabel>
          <Input
            id="repeat-followups"
            inputMode="numeric"
            value={followups}
            onChange={(event) => setFollowups(event.target.value)}
          />
        </Field>
      </div>
      <p className="text-xs text-muted-foreground">
        Same duration keeps the saved limits unless you change them; a new
        duration recalculates them first. The final values are shown before you
        start.
      </p>
      <DialogFooter>
        {footer}
        <Button type="submit" disabled={pending || invalid}>
          {pending ? "Planning…" : "Repeat with these settings"}
        </Button>
      </DialogFooter>
    </form>
  )
}

/** "Repeat with changes…" as a dialog: same role and resume, new limits. */
export function RepeatOptionsDialog({
  open,
  onOpenChange,
  interview,
  pending,
  onRepeat,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  interview: Interview
  pending: boolean
  onRepeat: (body: RepeatRequest) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="[--dialog-width:32rem]">
        <DialogTitle>Repeat with changes</DialogTitle>
        <DialogDescription>
          Plans a new interview from the same resume and job offer. Leave a
          field blank to keep what the profile decides.
        </DialogDescription>
        <RepeatOptions
          interview={interview}
          pending={pending}
          onRepeat={onRepeat}
          footer={
            <DialogClose render={<Button type="button" variant="ghost" />}>
              Cancel
            </DialogClose>
          }
        />
      </DialogContent>
    </Dialog>
  )
}
