import * as React from "react"
import { useNavigate } from "@tanstack/react-router"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useForm } from "@tanstack/react-form"
import * as z from "zod"
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  CheckIcon,
  FileTextIcon,
  TicketIcon,
  UploadIcon,
} from "lucide-react"

import {
  ApiError,
  LANGUAGE_LABELS,
  LENGTH_LABELS,
  LENGTH_TOPICS,
  SENIORITY_LABELS,
  createInterview,
  getSettings,
  voiceLabel,
} from "@/lib/api"
import type {
  InterviewLength,
  InterviewerInput,
  Me,
  Seniority,
} from "@/lib/api"
import { interviewQueryOptions } from "@/lib/queries"
import {
  interviewErrorMessage,
  quotaBlock,
  quotaNotice,
  refreshMeAfter,
  useMe,
} from "@/hooks/use-me"
import { useResumePreview } from "@/hooks/use-resume-preview"
import { log } from "@/lib/log"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { LinearProgress, Spinner } from "@/components/ui/spinner"
import { PageContainer, PageHeader, PageShell } from "@/components/ui/page"

function optionalInteger(min: number, max: number) {
  return z
    .string()
    .trim()
    .refine(
      (value) =>
        value === "" ||
        (/^\d+$/.test(value) && Number(value) >= min && Number(value) <= max),
      `Use a whole number from ${min} to ${max}, or leave blank.`
    )
}

const uploadSchema = z.object({
  resume: z
    .instanceof(File, { message: "Choose a PDF resume." })
    .refine((file) => file.size > 0, "Choose a PDF resume.")
    .refine(
      (file) => file.name.toLowerCase().endsWith(".pdf"),
      "The resume must be a PDF."
    )
    .refine(
      (file) => file.size <= 10 * 1024 * 1024,
      "The PDF must be 10 MB or smaller."
    ),
  resume_text: z
    .string()
    .refine((text) => !!text.trim(), "Review the extracted resume text.")
    .max(30000, "The resume text must be 30,000 characters or fewer."),
  job_offer: z
    .string()
    .refine((text) => !!text.trim(), "Paste the job offer.")
    .max(20000, "The offer must be 20,000 characters or fewer."),
  // Depth axis. "auto" lets the planner classify the role once from the offer
  // and the resume — without it the model infers the level from how advanced
  // the tech stack sounds, and grills a junior like a senior.
  seniority: z.enum(["auto", "trainee", "junior", "mid", "senior", "lead"]),
  // Volume axis, independent of the level: how much ground to cover.
  interview_length: z.enum(["short", "standard", "deep"]),
  // Who conducts it. Pre-filled from the global Settings screen; changing it
  // here applies to THIS interview only. Blank is allowed here because the
  // server falls back to the saved value for each of these — see
  // `interviewerSchema` for when a pick is actually expected.
  agent_name: z.string().trim(),
  language: z.string(),
  voice: z.string(),
  persona: z.string(),
  custom_instructions: z.string(),
  question_limit: optionalInteger(1, 12),
  followup_limit: optionalInteger(0, 2),
  max_minutes: optionalInteger(1, 25),
})

/** With the saved settings on screen the interviewer fields are pre-filled,
 *  so a blank one is a mistake worth flagging. Without them (GET /settings
 *  failed) the language and voice selects have no options to pick from, and
 *  blanks are the only way through — the server then uses the saved values. */
const interviewerSchema = uploadSchema.extend({
  agent_name: z.string().trim().min(1, "Give the interviewer a name."),
  language: z.string().min(1, "Pick a language."),
  voice: z.string().min(1, "Pick a voice."),
})

type FieldName = keyof z.infer<typeof uploadSchema>

/** The keys of the `interviewer` JSON field, in the order they are shown. */
const INTERVIEWER_FIELDS = [
  "agent_name",
  "language",
  "voice",
  "persona",
  "custom_instructions",
] as const satisfies ReadonlyArray<keyof InterviewerInput>

const BUDGET_FIELDS = [
  { name: "question_limit", label: "Main questions", min: 1, max: 12 },
  { name: "followup_limit", label: "Follow-ups per topic", min: 0, max: 2 },
  { name: "max_minutes", label: "Time limit (minutes)", min: 1, max: 25 },
] as const

/** The wizard, declared once.
 *
 * Adding a step is adding an entry here plus its case in `StepBody`: the
 * progress bar, the per-step gate and the footer all read from this. That
 * matters because the agent name / language / voice / persona settings are
 * moving from the global Settings screen to per-interview, which is what
 * pushed this form past the point where one long page stays comfortable. */
const STEPS = [
  {
    id: "role",
    title: "Role",
    description:
      "Upload a resume and paste the job offer — the interviewer plans the session from both.",
    fields: ["resume", "job_offer"],
  },
  {
    id: "resume-review",
    title: "Resume review",
    description:
      "Check the text read from your PDF. Correct missing or misplaced content before the interview is planned.",
    fields: ["resume_text"],
  },
  {
    id: "calibration",
    title: "Calibration",
    description: "How deep the interview goes, and how much ground it covers.",
    fields: [
      "seniority",
      "interview_length",
      "question_limit",
      "followup_limit",
      "max_minutes",
    ],
  },
  {
    id: "interviewer",
    title: "Interviewer",
    description:
      "Who runs it, and in which voice. Pre-filled from Settings — changing it here applies to this interview only.",
    fields: [
      "agent_name",
      "language",
      "voice",
      "persona",
      "custom_instructions",
    ],
  },
] as const satisfies ReadonlyArray<{
  id: string
  title: string
  description: string
  fields: ReadonlyArray<FieldName>
}>

const LAST_STEP = STEPS.length - 1

/** Per-step gate: only the fields owned by the step block advancing. */
const STEP_SCHEMAS = STEPS.map((step) =>
  uploadSchema.pick(
    Object.fromEntries(step.fields.map((f) => [f, true])) as Record<
      FieldName,
      true
    >
  )
)

const SENIORITY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "auto", label: "Auto — detect from the offer" },
  ...(Object.keys(SENIORITY_LABELS) as Seniority[]).map((value) => ({
    value,
    label: SENIORITY_LABELS[value],
  })),
]

const LENGTH_OPTIONS = (Object.keys(LENGTH_LABELS) as InterviewLength[]).map(
  (value) => ({
    value,
    label: LENGTH_LABELS[value],
    detail: LENGTH_TOPICS[value],
  })
)

const OPTION_LABELS: Record<string, string> = Object.fromEntries(
  [...SENIORITY_OPTIONS, ...LENGTH_OPTIONS].map((o) => [o.value, o.label])
)

/** Where the wizard is: a vertical list of steps on wide screens, a compact
 *  "Step n of m" with a bar on narrow ones. */
function Stepper({
  current,
  onSelect,
  disabled,
}: {
  current: number
  onSelect: (step: number) => void
  disabled: boolean
}) {
  return (
    <>
      <div className="flex flex-col gap-2 @3xl/main:hidden">
        <span className="text-xs text-muted-foreground">
          Step {current + 1} of {STEPS.length} · {STEPS[current].title}
        </span>
        <div className="flex gap-1.5" aria-hidden>
          {STEPS.map((step, i) => (
            <span
              key={step.id}
              className={cn(
                "h-1 flex-1 rounded-full transition-colors duration-200",
                i <= current ? "bg-primary" : "bg-surface-high"
              )}
            />
          ))}
        </div>
      </div>
      <nav aria-label="Steps" className="hidden @3xl/main:block">
        <ol className="flex flex-col gap-1">
          {STEPS.map((step, i) => {
            const done = i < current
            const active = i === current
            return (
              <li key={step.id}>
                <button
                  type="button"
                  // Only completed steps can be revisited; later ones open
                  // through Continue, which validates on the way.
                  disabled={!done || disabled}
                  onClick={() => onSelect(i)}
                  aria-current={active ? "step" : undefined}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-full py-2 pr-4 pl-2 text-left text-sm transition-colors",
                    active && "bg-primary-container text-on-primary-container",
                    done && "hover:bg-foreground/[0.06]",
                    !done && !active && "text-muted-foreground"
                  )}
                >
                  <span
                    aria-hidden
                    className={cn(
                      "flex size-7 shrink-0 items-center justify-center rounded-full font-heading text-xs font-medium",
                      active && "bg-primary text-primary-foreground",
                      done && "bg-success-container text-success",
                      !done && !active && "border border-border"
                    )}
                  >
                    {done ? (
                      <CheckIcon className="size-4" strokeWidth={3} />
                    ) : (
                      i + 1
                    )}
                  </span>
                  <span className="font-heading font-medium">{step.title}</span>
                  {done && <span className="sr-only">(done)</span>}
                </button>
              </li>
            )
          })}
        </ol>
      </nav>
    </>
  )
}

/** One already-satisfied input, collapsed to a compact row.
 *
 * Used for the picked resume, and again on the last step to review what the
 * earlier steps captured without dragging their bulky inputs along. */
function FilledRow({
  label,
  detail,
  action,
  onAction,
  disabled,
}: {
  label: string
  detail: string
  action: string
  onAction: () => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl bg-muted py-2 pr-2 pl-3">
      <span
        aria-hidden
        className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-card text-primary"
      >
        <FileTextIcon className="size-5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium">{label}</span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {detail}
        </span>
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={onAction}
        disabled={disabled}
      >
        {action}
      </Button>
    </div>
  )
}

/** The sample shipped with the app (public/sample-resume.pdf), as a File
 *  the picker could have produced. */
async function fetchSampleResume(): Promise<File> {
  const res = await fetch("/sample-resume.pdf")
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const blob = await res.blob()
  return new File([blob], "sample-resume.pdf", { type: "application/pdf" })
}

/** What is left of the user's interviews, under the page header. */
function QuotaNotice({ me, blocked }: { me: Me; blocked: string | null }) {
  if (blocked) {
    return (
      <Alert variant="warning">
        <AlertCircleIcon />
        <AlertDescription>{blocked}</AlertDescription>
      </Alert>
    )
  }
  return (
    <p className="flex items-center gap-2 text-sm text-muted-foreground">
      <TicketIcon aria-hidden className="size-4" />
      {quotaNotice(me)}
    </p>
  )
}

export function UploadPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [step, setStep] = React.useState(0)
  const resumePreview = useResumePreview()
  // Kept mounted so "Change" can reopen the picker from the collapsed row.
  const resumeInputRef = React.useRef<HTMLInputElement>(null)
  const me = useMe()
  // Out of interviews (or the demo is, this month): the wizard can still be
  // filled in, but the final submit stays closed and says why.
  const quotaBlocked = quotaBlock(me.data)
  const [sampleError, setSampleError] = React.useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: (formData: FormData) => createInterview(formData),
    onSuccess: (interview) => {
      log(
        "interview created:",
        interview.id,
        `(${interview.milestones.length} milestones)`
      )
      // Seed the landing page's detail and drop the cached history pages,
      // which do not have this row yet.
      queryClient.setQueryData(
        interviewQueryOptions(interview.id).queryKey,
        interview
      )
      void queryClient.invalidateQueries({ queryKey: ["interviews"] })
      refreshMeAfter(queryClient)
      navigate({
        to: "/interviews/$interviewId",
        params: { interviewId: interview.id },
      })
    },
    onError: (error) => {
      console.error("[app] interview creation failed:", error)
      refreshMeAfter(queryClient, error)
    },
  })

  // The global settings are the defaults for this interview. Hydrated once,
  // and only into fields the user has not already filled in — a late response
  // must never overwrite what they typed.
  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: getSettings,
  })
  const settings = settingsQuery.data
  const hydrated = React.useRef(false)
  // Without the saved settings there is no catalog to pick a language or a
  // voice from, and no name pre-filled: blanks have to pass, and the server
  // fills them from the saved settings.
  const schema = settingsQuery.isError ? uploadSchema : interviewerSchema

  const form = useForm({
    defaultValues: {
      resume: null as File | null,
      resume_text: "",
      job_offer: "",
      seniority: "auto",
      interview_length: "standard",
      agent_name: "",
      language: "",
      voice: "",
      persona: "",
      custom_instructions: "",
      question_limit: "",
      followup_limit: "",
      max_minutes: "",
    },
    validators: { onSubmit: schema },
    onSubmit: ({ value }) => {
      if (!resumePreview.data || !value.resume) {
        setStep(0)
        return
      }
      log("uploading resume and requesting a plan…")
      const formData = new FormData()
      formData.append("resume", value.resume)
      formData.append("job_offer", value.job_offer)
      formData.append("seniority", value.seniority)
      formData.append("interview_length", value.interview_length)
      formData.append(
        "resume_review",
        JSON.stringify({
          pdf_sha256: resumePreview.data.pdf_sha256,
          text: value.resume_text,
        })
      )
      // One JSON field, not five: an empty multipart field is
      // indistinguishable from an absent one, and here "" (run without a
      // persona) has to stay distinct from "inherit the global one".
      //
      // That distinction only holds once the saved settings were shown: a
      // blank the user could never have seen filled is "no answer", not
      // "none" — left out of the JSON, the server keeps the saved value
      // instead of clearing the persona for this interview.
      const interviewer: InterviewerInput = {}
      for (const key of INTERVIEWER_FIELDS) {
        if (hydrated.current || value[key]) interviewer[key] = value[key]
      }
      for (const { name } of BUDGET_FIELDS) {
        if (value[name].trim()) interviewer[name] = Number(value[name])
      }
      formData.append("interviewer", JSON.stringify(interviewer))
      mutation.mutate(formData)
    },
  })

  React.useEffect(() => {
    if (hydrated.current || !settings) return
    hydrated.current = true
    const defaults = {
      agent_name: settings.agent_name,
      language: settings.language,
      voice: settings.voice,
      persona: settings.persona ?? "",
      custom_instructions: settings.custom_instructions ?? "",
    } as const
    for (const [name, value] of Object.entries(defaults)) {
      const field = name as keyof typeof defaults
      if (!form.state.values[field]) form.setFieldValue(field, value)
    }
  }, [settings, form])

  /** The voices available for a language, from the catalog that rides along
   *  with the settings. */
  const voicesFor = (language: string) => settings?.voices[language] ?? []

  /** Advance only if this step's own fields validate.
   *
   * The gate is a zod slice rather than the form's validity: the form is not
   * valid until the LAST step is filled in, so asking it would block every
   * step. Touching the fields first is what surfaces the inline errors. */
  const goNext = async () => {
    if (mutation.isPending || resumePreview.pending) return
    // "change" matches the cause the field validators are registered under;
    // any other cause finds nothing to run and the step blocks with no message.
    await form.validateAllFields("change")
    for (const name of STEPS[step].fields) {
      form.setFieldMeta(name, (meta) => ({ ...meta, isTouched: true }))
    }
    if (STEP_SCHEMAS[step].safeParse(form.state.values).success) {
      if (STEPS[step].id === "role") {
        const file = form.state.values.resume
        if (!file) return
        const alreadyReviewed = resumePreview.data !== null
        const preview = await resumePreview.load(file)
        if (!preview || form.state.values.resume !== file) return
        // Going back must preserve the edits already reviewed for this PDF.
        if (!alreadyReviewed) form.setFieldValue("resume_text", preview.text)
      }
      setStep(Math.min(step + 1, LAST_STEP))
    }
  }

  return (
    <PageShell>
      <PageContainer variant="wide" className="flex flex-col gap-8">
        <div className="flex flex-col gap-4">
          <PageHeader
            title="New interview"
            description="Four short steps, planned from your resume and the job offer."
          />
          {me.data && <QuotaNotice me={me.data} blocked={quotaBlocked} />}
        </div>
        <div className="grid gap-8 @3xl/main:grid-cols-[14rem_minmax(0,1fr)] @3xl/main:gap-12">
          <Stepper
            current={step}
            onSelect={setStep}
            disabled={mutation.isPending || resumePreview.pending}
          />
          <form
            onSubmit={(event) => {
              event.preventDefault()
              // Enter inside a field must not skip the remaining steps.
              if (step < LAST_STEP) {
                void goNext()
                return
              }
              if (quotaBlocked !== null) return
              form.handleSubmit()
            }}
            className="flex max-w-2xl min-w-0 flex-col gap-8"
          >
            <div className="flex flex-col gap-1">
              <h2 className="text-title-lg">{STEPS[step].title}</h2>
              <p className="text-sm text-muted-foreground">
                {STEPS[step].description}
              </p>
            </div>
            <div>
              <FieldGroup className="gap-6">
                {STEPS[step].id === "role" && (
                  <>
                    <form.Field
                      name="resume"
                      validators={{ onChange: uploadSchema.shape.resume }}
                      children={(field) => {
                        const isInvalid =
                          field.state.meta.isTouched &&
                          !field.state.meta.isValid
                        const file = field.state.value
                        // Picked or the sample, the same way in: text
                        // extracted from the previous PDF no longer applies.
                        const pickResume = (next: File | null) => {
                          resumePreview.reset()
                          form.setFieldValue("resume_text", "")
                          field.handleChange(next)
                        }
                        return (
                          <Field data-invalid={isInvalid}>
                            <FieldLabel htmlFor={field.name}>
                              Resume (PDF)
                            </FieldLabel>
                            {/* Once a file is picked the dropzone has nothing
                                left to communicate, so it shrinks to a row. */}
                            {file ? (
                              <FilledRow
                                label={file.name}
                                detail={`${Math.round(file.size / 1024).toLocaleString()} KB`}
                                action="Change"
                                disabled={mutation.isPending}
                                onAction={() => resumeInputRef.current?.click()}
                              />
                            ) : (
                              /* Styled dropzone that hides the native file input
                                 so it matches the rest of the form controls. */
                              <FieldLabel
                                htmlFor={field.name}
                                className={cn(
                                  "flex w-full cursor-pointer flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-input/60 px-4 py-8 text-center transition-colors hover:border-primary hover:bg-primary-container/30 has-[:disabled]:pointer-events-none has-[:disabled]:opacity-50",
                                  isInvalid && "border-destructive"
                                )}
                              >
                                <span className="flex size-12 items-center justify-center rounded-full bg-primary-container text-on-primary-container">
                                  <UploadIcon className="size-5" />
                                </span>
                                <span className="flex flex-col gap-0.5 text-sm text-muted-foreground">
                                  <span>
                                    <span className="font-medium text-primary">
                                      Click to upload
                                    </span>{" "}
                                    your resume
                                  </span>
                                  <span className="text-xs">
                                    PDF, up to 10 MB
                                  </span>
                                </span>
                              </FieldLabel>
                            )}
                            <input
                              ref={resumeInputRef}
                              id={field.name}
                              name={field.name}
                              type="file"
                              accept="application/pdf,.pdf"
                              disabled={mutation.isPending}
                              onBlur={field.handleBlur}
                              onChange={(event) =>
                                pickResume(event.target.files?.[0] ?? null)
                              }
                              className="sr-only"
                            />
                            <FieldDescription className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                              Your resume is processed by OpenAI to plan the
                              interview.
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="-mr-3"
                                disabled={
                                  mutation.isPending || resumePreview.pending
                                }
                                onClick={() => {
                                  setSampleError(null)
                                  void fetchSampleResume().then(
                                    pickResume,
                                    () =>
                                      setSampleError(
                                        "The sample resume could not be loaded."
                                      )
                                  )
                                }}
                              >
                                Use sample resume
                              </Button>
                            </FieldDescription>
                            {sampleError && (
                              <FieldError>{sampleError}</FieldError>
                            )}
                            {isInvalid && (
                              <FieldError errors={field.state.meta.errors} />
                            )}
                          </Field>
                        )
                      }}
                    />

                    <form.Field
                      name="job_offer"
                      validators={{ onChange: uploadSchema.shape.job_offer }}
                      children={(field) => {
                        const isInvalid =
                          field.state.meta.isTouched &&
                          !field.state.meta.isValid
                        const value = field.state.value
                        return (
                          <Field data-invalid={isInvalid}>
                            <FieldLabel htmlFor={field.name}>
                              Job offer
                            </FieldLabel>
                            {/* max-h: the shared Textarea uses
                                field-sizing-content, which grows without a
                                ceiling — a pasted offer used to push the
                                footer off screen. */}
                            <Textarea
                              id={field.name}
                              name={field.name}
                              value={value}
                              onBlur={field.handleBlur}
                              onChange={(event) =>
                                field.handleChange(event.target.value)
                              }
                              aria-invalid={isInvalid}
                              disabled={mutation.isPending}
                              placeholder="Paste the job description here…"
                              className="max-h-64 min-h-40 overflow-y-auto"
                            />
                            {value.length > 0 && (
                              <FieldDescription className="tabular-nums">
                                {value.length.toLocaleString()} chars
                              </FieldDescription>
                            )}
                            {isInvalid && (
                              <FieldError errors={field.state.meta.errors} />
                            )}
                          </Field>
                        )
                      }}
                    />
                  </>
                )}

                {STEPS[step].id === "resume-review" && (
                  <form.Field
                    name="resume_text"
                    validators={{ onChange: uploadSchema.shape.resume_text }}
                    children={(field) => {
                      const invalid =
                        field.state.meta.isTouched && !field.state.meta.isValid
                      return (
                        <Field data-invalid={invalid}>
                          <FieldLabel htmlFor="resume_text">
                            Extracted resume text
                          </FieldLabel>
                          <Textarea
                            id="resume_text"
                            name="resume_text"
                            value={field.state.value}
                            onBlur={field.handleBlur}
                            onChange={(event) =>
                              field.handleChange(event.target.value)
                            }
                            aria-invalid={invalid}
                            disabled={mutation.isPending}
                            className="max-h-96 min-h-64 overflow-y-auto"
                          />
                          <FieldDescription>
                            {field.state.value.length.toLocaleString()} / 30,000
                            characters. This reviewed text will be used to plan
                            the interview.
                          </FieldDescription>
                          {invalid && (
                            <FieldError errors={field.state.meta.errors} />
                          )}
                        </Field>
                      )
                    }}
                  />
                )}

                {STEPS[step].id === "calibration" && (
                  <>
                    {/* What the previous step captured, reviewable without
                        dragging the dropzone and textarea along. */}
                    <form.Subscribe
                      selector={(state) => ({
                        resume: state.values.resume,
                        offer: state.values.job_offer,
                      })}
                    >
                      {({ resume, offer }) => (
                        <div className="flex flex-col gap-2">
                          {resume && (
                            <FilledRow
                              label={resume.name}
                              detail={`${Math.round(resume.size / 1024).toLocaleString()} KB`}
                              action="Edit"
                              disabled={mutation.isPending}
                              onAction={() => setStep(0)}
                            />
                          )}
                          {offer.trim() && (
                            <FilledRow
                              label={offer.trim().split("\n")[0]}
                              detail={`${offer.length.toLocaleString()} chars`}
                              action="Edit"
                              disabled={mutation.isPending}
                              onAction={() => setStep(0)}
                            />
                          )}
                        </div>
                      )}
                    </form.Subscribe>

                    <div className="grid items-start gap-4 @md/main:grid-cols-2">
                      <form.Field
                        name="seniority"
                        children={(field) => (
                          <Field>
                            <FieldLabel htmlFor={field.name}>
                              Expected level
                            </FieldLabel>
                            <Select
                              value={field.state.value}
                              onValueChange={(next) =>
                                next && field.handleChange(next)
                              }
                              disabled={mutation.isPending}
                            >
                              <SelectTrigger id={field.name} className="w-full">
                                <SelectValue>
                                  {(value: string) =>
                                    OPTION_LABELS[value] ?? value
                                  }
                                </SelectValue>
                              </SelectTrigger>
                              <SelectContent>
                                {SENIORITY_OPTIONS.map((option) => (
                                  <SelectItem
                                    key={option.value}
                                    value={option.value}
                                  >
                                    {option.label}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            <FieldDescription>
                              {/* The Auto caveat only earns its space on Auto. */}
                              {field.state.value === "auto"
                                ? "The offer decides — not how advanced the stack sounds."
                                : "Sets how deep the questions go and how answers are scored."}
                            </FieldDescription>
                          </Field>
                        )}
                      />

                      <form.Field
                        name="interview_length"
                        children={(field) => (
                          <Field>
                            <FieldLabel htmlFor={field.name}>Length</FieldLabel>
                            <Select
                              value={field.state.value}
                              onValueChange={(next) =>
                                next && field.handleChange(next)
                              }
                              disabled={mutation.isPending}
                            >
                              <SelectTrigger id={field.name} className="w-full">
                                <SelectValue>
                                  {(value: string) =>
                                    OPTION_LABELS[value] ?? value
                                  }
                                </SelectValue>
                              </SelectTrigger>
                              <SelectContent>
                                {LENGTH_OPTIONS.map((option) => (
                                  <SelectItem
                                    key={option.value}
                                    value={option.value}
                                  >
                                    {option.label}
                                    <span className="text-muted-foreground">
                                      {option.detail}
                                    </span>
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            <FieldDescription>
                              How much ground to cover. Independent of the
                              level.
                            </FieldDescription>
                          </Field>
                        )}
                      />
                    </div>
                    <div className="grid items-start gap-4 @md/main:grid-cols-3">
                      {BUDGET_FIELDS.map(({ name, label, min, max }) => (
                        <form.Field
                          key={name}
                          name={name}
                          validators={{ onChange: uploadSchema.shape[name] }}
                          children={(field) => {
                            const invalid =
                              field.state.meta.isTouched &&
                              !field.state.meta.isValid
                            return (
                              <Field data-invalid={invalid}>
                                <FieldLabel htmlFor={name}>{label}</FieldLabel>
                                <Input
                                  id={name}
                                  name={name}
                                  type="number"
                                  min={min}
                                  max={max}
                                  step={1}
                                  value={field.state.value}
                                  placeholder="Automatic"
                                  onBlur={field.handleBlur}
                                  onChange={(event) =>
                                    field.handleChange(event.target.value)
                                  }
                                  aria-invalid={invalid}
                                  disabled={mutation.isPending}
                                />
                                {invalid && (
                                  <FieldError
                                    errors={field.state.meta.errors}
                                  />
                                )}
                              </Field>
                            )
                          }}
                        />
                      ))}
                    </div>
                    <FieldDescription>
                      Leave limits blank to use the selected length and level.
                      Follow-ups may be lower for shorter or less advanced
                      interviews; the final limits are shown before you start.
                    </FieldDescription>
                  </>
                )}

                {STEPS[step].id === "interviewer" && (
                  <>
                    {/* What the calibration step settled, reviewable without
                        its two selects. */}
                    <form.Subscribe
                      selector={(state) => ({
                        seniority: state.values.seniority,
                        length: state.values.interview_length,
                      })}
                    >
                      {({ seniority, length }) => (
                        <FilledRow
                          label={OPTION_LABELS[seniority] ?? seniority}
                          detail={LENGTH_LABELS[length as InterviewLength]}
                          action="Edit"
                          disabled={mutation.isPending}
                          onAction={() => setStep(1)}
                        />
                      )}
                    </form.Subscribe>

                    {settingsQuery.isError && (
                      <Alert variant="warning">
                        <AlertCircleIcon />
                        <AlertDescription>
                          Could not load your saved settings —{" "}
                          {errorMessage(settingsQuery.error)}. The interview
                          will use the saved name, language and voice; the
                          fields below only apply if you fill them in.
                        </AlertDescription>
                      </Alert>
                    )}

                    <form.Field
                      name="agent_name"
                      validators={{ onChange: schema.shape.agent_name }}
                      children={(field) => {
                        const isInvalid =
                          field.state.meta.isTouched &&
                          !field.state.meta.isValid
                        return (
                          <Field data-invalid={isInvalid}>
                            <FieldLabel htmlFor={field.name}>Name</FieldLabel>
                            <Input
                              id={field.name}
                              name={field.name}
                              value={field.state.value}
                              onBlur={field.handleBlur}
                              onChange={(event) =>
                                field.handleChange(event.target.value)
                              }
                              aria-invalid={isInvalid}
                              disabled={mutation.isPending}
                              placeholder="Emma"
                            />
                            <FieldDescription>
                              How the interviewer introduces themselves.
                            </FieldDescription>
                            {isInvalid && (
                              <FieldError errors={field.state.meta.errors} />
                            )}
                          </Field>
                        )
                      }}
                    />

                    <div className="grid items-start gap-4 @md/main:grid-cols-2">
                      <form.Field
                        name="language"
                        children={(field) => (
                          <Field>
                            <FieldLabel htmlFor={field.name}>
                              Language
                            </FieldLabel>
                            <Select
                              value={field.state.value}
                              onValueChange={(next) => {
                                if (!next) return
                                field.handleChange(next)
                                // Voices are per language: keep the pair
                                // valid instead of letting the server 400.
                                const voices = voicesFor(next)
                                const current = form.state.values.voice
                                if (!voices.some((v) => v.id === current)) {
                                  form.setFieldValue(
                                    "voice",
                                    voices[0]?.id ?? ""
                                  )
                                }
                              }}
                              // No catalog, no options: a select with
                              // nothing in it is worse than one that says
                              // what will be used.
                              disabled={
                                mutation.isPending || settingsQuery.isError
                              }
                            >
                              <SelectTrigger id={field.name} className="w-full">
                                <SelectValue>
                                  {(value: string) =>
                                    LANGUAGE_LABELS[value] ??
                                    (value ||
                                      (settingsQuery.isError
                                        ? "Saved default"
                                        : "Pick one"))
                                  }
                                </SelectValue>
                              </SelectTrigger>
                              <SelectContent>
                                {Object.keys(settings?.voices ?? {}).map(
                                  (code) => (
                                    <SelectItem key={code} value={code}>
                                      {LANGUAGE_LABELS[code] ?? code}
                                    </SelectItem>
                                  )
                                )}
                              </SelectContent>
                            </Select>
                            <FieldDescription>
                              The interview is conducted in this language.
                            </FieldDescription>
                          </Field>
                        )}
                      />

                      <form.Subscribe
                        selector={(state) => state.values.language}
                      >
                        {(language) => (
                          <form.Field
                            name="voice"
                            children={(field) => (
                              <Field>
                                <FieldLabel htmlFor={field.name}>
                                  Voice
                                </FieldLabel>
                                <Select
                                  value={field.state.value}
                                  onValueChange={(next) =>
                                    next && field.handleChange(next)
                                  }
                                  disabled={
                                    mutation.isPending || settingsQuery.isError
                                  }
                                >
                                  <SelectTrigger
                                    id={field.name}
                                    className="w-full"
                                  >
                                    <SelectValue>
                                      {(value: string) => {
                                        const voice = voicesFor(language).find(
                                          (v) => v.id === value
                                        )
                                        return voice
                                          ? voiceLabel(language, voice)
                                          : settingsQuery.isError
                                            ? "Saved default"
                                            : "Pick one"
                                      }}
                                    </SelectValue>
                                  </SelectTrigger>
                                  <SelectContent>
                                    {voicesFor(language).map((voice) => (
                                      <SelectItem
                                        key={voice.id}
                                        value={voice.id}
                                      >
                                        {voiceLabel(language, voice)}
                                      </SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                                <FieldDescription>
                                  What the interviewer sounds like.
                                </FieldDescription>
                              </Field>
                            )}
                          />
                        )}
                      </form.Subscribe>
                    </div>

                    <form.Field
                      name="persona"
                      children={(field) => (
                        <Field>
                          <FieldLabel htmlFor={field.name}>
                            Personality{" "}
                            <span className="text-muted-foreground">
                              (optional)
                            </span>
                          </FieldLabel>
                          <Textarea
                            id={field.name}
                            name={field.name}
                            value={field.state.value}
                            onBlur={field.handleBlur}
                            onChange={(event) =>
                              field.handleChange(event.target.value)
                            }
                            disabled={mutation.isPending}
                            placeholder="A warm but rigorous engineering manager…"
                            className="max-h-40 min-h-20 overflow-y-auto"
                          />
                          <FieldDescription>
                            Leave it empty to run this interview without one.
                          </FieldDescription>
                        </Field>
                      )}
                    />

                    <form.Field
                      name="custom_instructions"
                      children={(field) => (
                        <Field>
                          <FieldLabel htmlFor={field.name}>
                            Extra instructions{" "}
                            <span className="text-muted-foreground">
                              (optional)
                            </span>
                          </FieldLabel>
                          <Textarea
                            id={field.name}
                            name={field.name}
                            value={field.state.value}
                            onBlur={field.handleBlur}
                            onChange={(event) =>
                              field.handleChange(event.target.value)
                            }
                            disabled={mutation.isPending}
                            placeholder="Focus on system design; ask in English but let me answer in Spanish…"
                            className="max-h-40 min-h-20 overflow-y-auto"
                          />
                          <FieldDescription>
                            Anything the interviewer should keep in mind.
                          </FieldDescription>
                        </Field>
                      )}
                    />
                  </>
                )}

                {mutation.isPending && (
                  <div
                    role="status"
                    className="flex flex-col gap-3 rounded-xl bg-primary-container/40 px-4 py-4"
                  >
                    <span className="text-sm font-medium">
                      Planning from the reviewed resume… (can take ~1 min)
                    </span>
                    <LinearProgress label="Planning the interview" />
                    <span className="text-xs text-muted-foreground">
                      The preparation room opens when the plan is ready.
                    </span>
                  </div>
                )}

                {resumePreview.pending && (
                  <FieldDescription className="flex items-center gap-2">
                    <Spinner />
                    Reading the PDF…
                  </FieldDescription>
                )}
                {resumePreview.error && (
                  <Alert variant="destructive">
                    <AlertCircleIcon />
                    <AlertDescription>
                      {errorMessage(resumePreview.error)}
                    </AlertDescription>
                  </Alert>
                )}

                {mutation.isError && (
                  <Alert variant="destructive">
                    <AlertCircleIcon />
                    <AlertDescription>
                      {interviewErrorMessage(mutation.error, me.data)}
                    </AlertDescription>
                  </Alert>
                )}
              </FieldGroup>
            </div>
            <div className="flex items-center justify-between gap-2 border-t pt-6">
              {step > 0 ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setStep((s) => s - 1)}
                  disabled={mutation.isPending}
                >
                  <ArrowLeftIcon />
                  Back
                </Button>
              ) : (
                <span />
              )}
              {/* Both are type="button", and the keys force distinct DOM
                  nodes. React would otherwise reuse ONE <button> across the
                  ternary and merely flip its `type`: `goNext` awaits before
                  setStep, so the swap to type="submit" lands while the click
                  is still being processed, and the browser then runs the
                  activation behaviour against the button it has become —
                  advancing a step AND submitting on a single Continue click.
                  The form's onSubmit still covers Enter inside a field. */}
              {step < LAST_STEP ? (
                <Button
                  key="continue"
                  type="button"
                  disabled={mutation.isPending || resumePreview.pending}
                  onClick={() => void goNext()}
                >
                  Continue
                </Button>
              ) : (
                <Button
                  key="submit"
                  type="button"
                  disabled={mutation.isPending || quotaBlocked !== null}
                  title={quotaBlocked ?? undefined}
                  onClick={() => form.handleSubmit()}
                >
                  {mutation.isPending ? "Planning…" : "Prepare interview"}
                </Button>
              )}
            </div>
          </form>
        </div>
      </PageContainer>
    </PageShell>
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}
