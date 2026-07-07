import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useMutation } from "@tanstack/react-query"
import { useForm } from "@tanstack/react-form"
import * as z from "zod"
import { FileTextIcon, UploadIcon } from "lucide-react"

import { ApiError, createInterview } from "@/lib/api"
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
import { Textarea } from "@/components/ui/textarea"
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
})

function UploadPage() {
  const navigate = useNavigate()

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
    },
    validators: { onSubmit: uploadSchema },
    onSubmit: ({ value }) => {
      log("uploading resume and requesting a plan…")
      const formData = new FormData()
      if (value.resume) formData.append("resume", value.resume)
      formData.append("job_offer", value.job_offer)
      mutation.mutate(formData)
    },
  })

  return (
    <PageShell>
      <PageContainer variant="narrow">
        <Card>
          <CardHeader>
            <CardTitle>New interview</CardTitle>
            <CardDescription>
              Upload a resume and paste the job offer — the interviewer plans
              the session from both.
            </CardDescription>
          </CardHeader>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              form.handleSubmit()
            }}
            className="flex flex-col gap-(--card-spacing)"
          >
            <CardContent>
              <FieldGroup>
                <form.Field
                  name="resume"
                  children={(field) => {
                    const isInvalid =
                      field.state.meta.isTouched && !field.state.meta.isValid
                    const file = field.state.value
                    return (
                      <Field data-invalid={isInvalid}>
                        <FieldLabel htmlFor={field.name}>
                          Resume (PDF)
                        </FieldLabel>
                        {/* Styled dropzone that hides the native file input so it
                            matches the rest of the form controls. */}
                        <FieldLabel
                          htmlFor={field.name}
                          className={cn(
                            "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-input px-4 py-6 text-center transition-colors hover:bg-muted/50 has-[:disabled]:pointer-events-none has-[:disabled]:opacity-50",
                            isInvalid && "border-destructive/60"
                          )}
                        >
                          <span className="flex size-9 items-center justify-center rounded-full bg-muted text-muted-foreground">
                            {file ? (
                              <FileTextIcon className="size-4" />
                            ) : (
                              <UploadIcon className="size-4" />
                            )}
                          </span>
                          {file ? (
                            <span className="text-sm font-medium break-all">
                              {file.name}
                            </span>
                          ) : (
                            <span className="text-sm text-muted-foreground">
                              <span className="font-medium text-foreground">
                                Click to upload
                              </span>{" "}
                              your resume
                            </span>
                          )}
                          <input
                            id={field.name}
                            name={field.name}
                            type="file"
                            accept="application/pdf,.pdf"
                            disabled={mutation.isPending}
                            onBlur={field.handleBlur}
                            onChange={(event) =>
                              field.handleChange(event.target.files?.[0] ?? null)
                            }
                            className="sr-only"
                          />
                        </FieldLabel>
                        {isInvalid && (
                          <FieldError errors={field.state.meta.errors} />
                        )}
                      </Field>
                    )
                  }}
                />

                <form.Field
                  name="job_offer"
                  children={(field) => {
                    const isInvalid =
                      field.state.meta.isTouched && !field.state.meta.isValid
                    return (
                      <Field data-invalid={isInvalid}>
                        <FieldLabel htmlFor={field.name}>Job offer</FieldLabel>
                        <Textarea
                          id={field.name}
                          name={field.name}
                          value={field.state.value}
                          onBlur={field.handleBlur}
                          onChange={(event) =>
                            field.handleChange(event.target.value)
                          }
                          aria-invalid={isInvalid}
                          disabled={mutation.isPending}
                          placeholder="Paste the job description here…"
                          className="min-h-40"
                        />
                        {isInvalid && (
                          <FieldError errors={field.state.meta.errors} />
                        )}
                      </Field>
                    )
                  }}
                />

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
            <CardFooter className="justify-end gap-2">
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "Planning…" : "Start interview"}
              </Button>
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
