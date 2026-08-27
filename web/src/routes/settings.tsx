import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useForm } from "@tanstack/react-form"
import * as z from "zod"

import {
  ApiError,
  LANGUAGE_LABELS,
  getSettings,
  updateSettings,
  voiceLabel,
} from "@/lib/api"
import type { Settings, SettingsUpdate } from "@/lib/api"
import { log } from "@/lib/log"
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
import { Skeleton } from "@/components/ui/skeleton"
import { PageContainer, PageShell } from "@/components/ui/page"

export const Route = createFileRoute("/settings")({ component: SettingsPage })

const SETTINGS_QUERY_KEY = ["settings"] as const

const settingsSchema = z.object({
  agent_name: z.string().trim().min(1, "Agent name is required."),
  language: z.string().min(1, "Pick a language."),
  voice: z.string().min(1, "Pick a voice."),
  persona: z.string(),
  custom_instructions: z.string(),
})

function SettingsPage() {
  const query = useQuery({ queryKey: SETTINGS_QUERY_KEY, queryFn: getSettings })

  // Top-aligned (not centered): the form is tall enough that vertical centering
  // pushed the last field under the footer on shorter viewports.
  return (
    <PageShell>
      <PageContainer variant="narrow">
        {query.isPending ? (
          <Card>
            <CardHeader>
              <Skeleton className="h-5 w-24" />
              <Skeleton className="h-4 w-64" />
            </CardHeader>
            <CardContent className="flex flex-col gap-5">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-20 w-full" />
            </CardContent>
          </Card>
        ) : query.isError ? (
          <Alert variant="destructive">
            <AlertDescription>{errorMessage(query.error)}</AlertDescription>
          </Alert>
        ) : (
          <SettingsForm settings={query.data} />
        )}
      </PageContainer>
    </PageShell>
  )
}

function SettingsForm({ settings }: { settings: Settings }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: (body: SettingsUpdate) => updateSettings(body),
    onSuccess: (updated) => {
      queryClient.setQueryData(SETTINGS_QUERY_KEY, updated)
      log("settings saved")
      navigate({ to: "/" })
    },
    onError: (error) => {
      console.error("[app] could not save settings:", error)
    },
  })

  const form = useForm({
    defaultValues: {
      agent_name: settings.agent_name,
      language: settings.language,
      voice: settings.voice,
      persona: settings.persona ?? "",
      custom_instructions: settings.custom_instructions ?? "",
    },
    validators: { onSubmit: settingsSchema },
    onSubmit: ({ value }) => {
      mutation.mutate({
        agent_name: value.agent_name.trim(),
        language: value.language,
        voice: value.voice,
        persona: value.persona.trim() || null,
        custom_instructions: value.custom_instructions.trim() || null,
      })
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Settings</CardTitle>
        <CardDescription>
          Global agent configuration — applies to every interview created from
          now on.
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
              name="agent_name"
              children={(field) => {
                const isInvalid =
                  field.state.meta.isTouched && !field.state.meta.isValid
                return (
                  <Field data-invalid={isInvalid}>
                    <FieldLabel htmlFor={field.name}>Agent name</FieldLabel>
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
                      autoComplete="off"
                    />
                    {isInvalid && (
                      <FieldError errors={field.state.meta.errors} />
                    )}
                  </Field>
                )
              }}
            />

            <form.Field
              name="language"
              children={(field) => {
                const isInvalid =
                  field.state.meta.isTouched && !field.state.meta.isValid
                return (
                  <Field data-invalid={isInvalid}>
                    <FieldLabel htmlFor={field.name}>Language</FieldLabel>
                    <Select
                      value={field.state.value}
                      onValueChange={(next) => {
                        if (!next) return
                        field.handleChange(next)
                        // Swap the voice list to the new language and reset the
                        // selection to that language's first voice.
                        const firstVoice = settings.voices[next]?.[0]
                        if (firstVoice)
                          form.setFieldValue("voice", firstVoice.id)
                      }}
                      disabled={mutation.isPending}
                    >
                      <SelectTrigger
                        id={field.name}
                        aria-invalid={isInvalid}
                        className="w-full"
                      >
                        <SelectValue>
                          {(value: string) => LANGUAGE_LABELS[value] ?? value}
                        </SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {Object.keys(settings.voices).map((code) => (
                          <SelectItem key={code} value={code}>
                            {LANGUAGE_LABELS[code] ?? code}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {isInvalid && (
                      <FieldError errors={field.state.meta.errors} />
                    )}
                  </Field>
                )
              }}
            />

            {/* Voice options depend on the currently-selected language, so this
                field subscribes to it and re-renders when it changes. */}
            <form.Subscribe selector={(state) => state.values.language}>
              {(language) => {
                const voiceOptions = settings.voices[language] ?? []
                return (
                  <form.Field
                    name="voice"
                    children={(field) => {
                      const isInvalid =
                        field.state.meta.isTouched && !field.state.meta.isValid
                      return (
                        <Field data-invalid={isInvalid}>
                          <FieldLabel htmlFor={field.name}>Voice</FieldLabel>
                          <Select
                            value={field.state.value}
                            onValueChange={(next) => {
                              if (next) field.handleChange(next)
                            }}
                            disabled={mutation.isPending}
                          >
                            <SelectTrigger
                              id={field.name}
                              aria-invalid={isInvalid}
                              className="w-full"
                            >
                              <SelectValue>
                                {(value: string) => {
                                  const selected = voiceOptions.find(
                                    (v) => v.id === value
                                  )
                                  return selected
                                    ? voiceLabel(language, selected)
                                    : value
                                }}
                              </SelectValue>
                            </SelectTrigger>
                            <SelectContent>
                              {voiceOptions.map((v) => (
                                <SelectItem key={v.id} value={v.id}>
                                  {voiceLabel(language, v)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          {isInvalid && (
                            <FieldError errors={field.state.meta.errors} />
                          )}
                        </Field>
                      )
                    }}
                  />
                )
              }}
            </form.Subscribe>

            <form.Field
              name="persona"
              children={(field) => (
                <Field>
                  <FieldLabel htmlFor={field.name}>Persona</FieldLabel>
                  <Textarea
                    id={field.name}
                    name={field.name}
                    value={field.state.value}
                    onBlur={field.handleBlur}
                    onChange={(event) => field.handleChange(event.target.value)}
                    disabled={mutation.isPending}
                    placeholder="Optional — describe the interviewer's personality."
                    className="min-h-20"
                  />
                </Field>
              )}
            />

            <form.Field
              name="custom_instructions"
              children={(field) => (
                <Field>
                  <FieldLabel htmlFor={field.name}>
                    Custom instructions
                  </FieldLabel>
                  <Textarea
                    id={field.name}
                    name={field.name}
                    value={field.state.value}
                    onBlur={field.handleBlur}
                    onChange={(event) => field.handleChange(event.target.value)}
                    disabled={mutation.isPending}
                    placeholder="Optional — extra guidance for the interviewer."
                    className="min-h-24"
                  />
                </Field>
              )}
            />

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
            {mutation.isPending ? "Saving…" : "Save"}
          </Button>
        </CardFooter>
      </form>
    </Card>
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}
