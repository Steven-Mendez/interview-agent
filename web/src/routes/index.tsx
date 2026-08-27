import * as React from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useForm } from "@tanstack/react-form"
import * as z from "zod"
import { ArrowLeftIcon, CheckCircle2Icon, UploadIcon } from "lucide-react"

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
import type { InterviewLength, Seniority } from "@/lib/api"
import { log } from "@/lib/log"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
import { Spinner } from "@/components/ui/spinner"
import { PageContainer, PageShell } from "@/components/ui/page"

export const Route = createFileRoute("/")({ component: UploadPage })

const uploadSchema = z.object({
  resume: z
    .instanceof(File, { message: "Choose a PDF resume." })
    .refine((file) => file.size > 0, "Choose a PDF resume.")
    .refine(
      (file) =>
        file.type === "application/pdf" ||
        file.name.toLowerCase().endsWith(".pdf"),
      "The resume must be a PDF."
    ),
  job_offer: z.string().trim().min(1, "Paste the job offer."),
  // Depth axis. "auto" lets the planner classify the role once from the offer
  // and the resume — without it the model infers the level from how advanced
  // the tech stack sounds, and grills a junior like a senior.
  seniority: z.enum(["auto", "trainee", "junior", "mid", "senior", "lead"]),
  // Volume axis, independent of the level: how much ground to cover.
  interview_length: z.enum(["short", "standard", "deep"]),
  // Who conducts it. Pre-filled from the global Settings screen; changing it
  // here applies to THIS interview only.
  agent_name: z.string().trim().min(1, "Give the interviewer a name."),
  language: z.string().min(1, "Pick a language."),
  voice: z.string().min(1, "Pick a voice."),
  persona: z.string(),
  custom_instructions: z.string(),
})

type FieldName = keyof z.infer<typeof uploadSchema>

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
    id: "calibration",
    title: "Calibration",
    description: "How deep the interview goes, and how much ground it covers.",
    fields: ["seniority", "interview_length"],
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

function StepProgress({ current }: { current: number }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-1.5" aria-hidden>
        {STEPS.map((step, i) => (
          <span
            key={step.id}
            className={cn(
              "h-1 flex-1 rounded-full transition-colors",
              i <= current ? "bg-primary" : "bg-muted"
            )}
          />
        ))}
      </div>
      <span className="text-xs text-muted-foreground">
        Step {current + 1} of {STEPS.length} · {STEPS[current].title}
      </span>
    </div>
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
    <div className="flex items-center gap-3 rounded-lg border border-input px-3 py-2.5">
      <CheckCircle2Icon className="size-4 shrink-0 text-success" />
      <div className="flex min-w-0 flex-1 items-baseline gap-2">
        <span className="truncate text-sm font-medium">{label}</span>
        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
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

function UploadPage() {
  const navigate = useNavigate()
  const [step, setStep] = React.useState(0)
  // Kept mounted so "Change" can reopen the picker from the collapsed row.
  const resumeInputRef = React.useRef<HTMLInputElement>(null)

  const mutation = useMutation({
    mutationFn: (formData: FormData) => createInterview(formData),
    onSuccess: (interview) => {
      log(
        "interview created:",
        interview.id,
        `(${interview.milestones.length} milestones)`
      )
      navigate({
        to: "/interviews/$interviewId",
        params: { interviewId: interview.id },
      })
    },
    onError: (error) => {
      console.error("[app] interview creation failed:", error)
    },
  })

  const form = useForm({
    defaultValues: {
      resume: null as File | null,
      job_offer: "",
      seniority: "auto",
      interview_length: "standard",
      agent_name: "",
      language: "",
      voice: "",
      persona: "",
      custom_instructions: "",
    },
    validators: { onSubmit: uploadSchema },
    onSubmit: ({ value }) => {
      log("uploading resume and requesting a plan…")
      const formData = new FormData()
      if (value.resume) formData.append("resume", value.resume)
      formData.append("job_offer", value.job_offer)
      formData.append("seniority", value.seniority)
      formData.append("interview_length", value.interview_length)
      // One JSON field, not five: an empty multipart field is
      // indistinguishable from an absent one, and here "" (run without a
      // persona) has to stay distinct from "inherit the global one".
      formData.append(
        "interviewer",
        JSON.stringify({
          agent_name: value.agent_name,
          language: value.language,
          voice: value.voice,
          persona: value.persona,
          custom_instructions: value.custom_instructions,
        })
      )
      mutation.mutate(formData)
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
    // "change" matches the cause the field validators are registered under;
    // any other cause finds nothing to run and the step blocks with no message.
    await form.validateAllFields("change")
    for (const name of STEPS[step].fields) {
      form.setFieldMeta(name, (meta) => ({ ...meta, isTouched: true }))
    }
    if (STEP_SCHEMAS[step].safeParse(form.state.values).success) {
      setStep((s) => Math.min(s + 1, LAST_STEP))
    }
  }

  return (
    <PageShell>
      <PageContainer variant="narrow">
        <Card>
          <CardHeader>
            <CardTitle>New interview</CardTitle>
            <CardDescription>{STEPS[step].description}</CardDescription>
            <div className="pt-2">
              <StepProgress current={step} />
            </div>
          </CardHeader>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              // Enter inside a field must not skip the remaining steps.
              if (step < LAST_STEP) {
                void goNext()
                return
              }
              form.handleSubmit()
            }}
            className="flex flex-col gap-(--card-spacing)"
          >
            <CardContent>
              <FieldGroup>
                {step === 0 && (
                  <>
                    <form.Field
                      name="resume"
                      validators={{ onChange: uploadSchema.shape.resume }}
                      children={(field) => {
                        const isInvalid =
                          field.state.meta.isTouched &&
                          !field.state.meta.isValid
                        const file = field.state.value
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
                                  "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-input px-4 py-6 text-center transition-colors hover:bg-muted/50 has-[:disabled]:pointer-events-none has-[:disabled]:opacity-50",
                                  isInvalid && "border-destructive/60"
                                )}
                              >
                                <span className="flex size-9 items-center justify-center rounded-full bg-muted text-muted-foreground">
                                  <UploadIcon className="size-4" />
                                </span>
                                <span className="text-sm text-muted-foreground">
                                  <span className="font-medium text-foreground">
                                    Click to upload
                                  </span>{" "}
                                  your resume
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
                                field.handleChange(
                                  event.target.files?.[0] ?? null
                                )
                              }
                              className="sr-only"
                            />
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

                {step === 1 && (
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
                  </>
                )}

                {step === 2 && (
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
                      <Alert variant="destructive">
                        <AlertDescription>
                          Could not load your saved settings —{" "}
                          {errorMessage(settingsQuery.error)}. Pick a language
                          and a voice below.
                        </AlertDescription>
                      </Alert>
                    )}

                    <form.Field
                      name="agent_name"
                      validators={{ onChange: uploadSchema.shape.agent_name }}
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
                              disabled={mutation.isPending}
                            >
                              <SelectTrigger id={field.name} className="w-full">
                                <SelectValue>
                                  {(value: string) =>
                                    LANGUAGE_LABELS[value] ??
                                    (value || "Pick one")
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
                                  disabled={mutation.isPending}
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
                  <FieldDescription className="flex items-center gap-2">
                    <Spinner />
                    Reading the resume and planning the interview… (can take ~1
                    min)
                  </FieldDescription>
                )}

                {mutation.isError && (
                  <Alert variant="destructive">
                    <AlertDescription>
                      {errorMessage(mutation.error)}
                    </AlertDescription>
                  </Alert>
                )}
              </FieldGroup>
            </CardContent>
            <CardFooter className="justify-between gap-2">
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
                  onClick={() => void goNext()}
                >
                  Continue
                </Button>
              ) : (
                <Button
                  key="submit"
                  type="button"
                  disabled={mutation.isPending}
                  onClick={() => form.handleSubmit()}
                >
                  {mutation.isPending ? "Planning…" : "Start interview"}
                </Button>
              )}
            </CardFooter>
          </form>
        </Card>
      </PageContainer>
    </PageShell>
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}
