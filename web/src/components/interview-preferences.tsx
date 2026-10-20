import type { InterviewPreferences } from "@/lib/api"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

const TYPES = {
  technical: "Technical",
  behavioral: "Behavioral",
  mixed: "Mixed",
}
const OBJECTIVES = {
  simulation: "Interview simulation",
  practice: "Focused practice",
}

export function InterviewPreferenceFields({
  value,
  onChange,
  disabled = false,
}: {
  value: InterviewPreferences
  onChange: (value: InterviewPreferences) => void
  disabled?: boolean
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="interview-type">Interview type</FieldLabel>
          <Select
            value={value.interview_type}
            disabled={disabled}
            onValueChange={(next) =>
              next && onChange({ ...value, interview_type: next })
            }
          >
            <SelectTrigger id="interview-type" className="w-full">
              <SelectValue>
                {(v: InterviewPreferences["interview_type"]) => TYPES[v]}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(TYPES) as (keyof typeof TYPES)[]).map((type) => (
                <SelectItem key={type} value={type}>
                  {TYPES[type]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor="interview-objective">Objective</FieldLabel>
          <Select
            value={value.objective}
            disabled={disabled}
            onValueChange={(next) =>
              next &&
              onChange({
                ...value,
                objective: next,
                practice_focus: next === "practice" ? value.practice_focus : "",
              })
            }
          >
            <SelectTrigger id="interview-objective" className="w-full">
              <SelectValue>
                {(v: InterviewPreferences["objective"]) => OBJECTIVES[v]}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(OBJECTIVES) as (keyof typeof OBJECTIVES)[]).map(
                (objective) => (
                  <SelectItem key={objective} value={objective}>
                    {OBJECTIVES[objective]}
                  </SelectItem>
                )
              )}
            </SelectContent>
          </Select>
        </Field>
      </div>
      {value.objective === "practice" && (
        <Field>
          <FieldLabel htmlFor="practice-focus">
            Practice focus (optional)
          </FieldLabel>
          <Input
            id="practice-focus"
            value={value.practice_focus}
            maxLength={1000}
            disabled={disabled}
            placeholder="For example: explaining a bug or asking for help"
            onChange={(event) =>
              onChange({ ...value, practice_focus: event.target.value })
            }
          />
          <FieldDescription>
            Feedback on the selected skills, without an overall hiring verdict.
          </FieldDescription>
        </Field>
      )}
    </div>
  )
}
