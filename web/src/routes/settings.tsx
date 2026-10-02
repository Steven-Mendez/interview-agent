import * as React from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useForm } from "@tanstack/react-form"
import { useTheme } from "next-themes"
import { toast } from "sonner"
import * as z from "zod"
import { AlertCircleIcon, MonitorIcon, MoonIcon, SunIcon } from "lucide-react"

import {
  ApiError,
  LANGUAGE_LABELS,
  getSettings,
  updateSettings,
  voiceLabel,
} from "@/lib/api"
import type { Settings, SettingsUpdate } from "@/lib/api"
import { log } from "@/lib/log"
import { cn } from "@/lib/utils"
import { Alert, AlertAction, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { FieldError } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { PageContainer, PageHeader, PageShell } from "@/components/ui/page"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { pageHead } from "@/lib/head"
import { requireSession } from "@/lib/route-guards"

export const Route = createFileRoute("/settings")({
  beforeLoad: ({ location }) => requireSession(location),
  head: () => pageHead("Settings"),
  component: SettingsPage,
})

const SETTINGS_QUERY_KEY = ["settings"] as const

const settingsSchema = z.object({
  agent_name: z.string().trim().min(1, "Agent name is required."),
  language: z.string().min(1, "Pick a language."),
  voice: z.string().min(1, "Pick a voice."),
  persona: z.string(),
  custom_instructions: z.string(),
})

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function SettingsPage() {
  const query = useQuery({ queryKey: SETTINGS_QUERY_KEY, queryFn: getSettings })

  return (
    <PageShell>
      <PageContainer variant="reading" className="flex flex-col gap-8">
        <PageHeader
          title="Settings"
          description="Defaults for new interviews, and how the app looks on this device."
        />
        {query.isPending ? (
          <SettingsSection
            title="Interviewer"
            description="Who runs your interviews by default."
          >
            {Array.from({ length: 4 }, (_, i) => (
              <SettingRow key={i} label={<Skeleton className="h-4 w-24" />}>
                <Skeleton className="h-11 w-full" />
              </SettingRow>
            ))}
          </SettingsSection>
        ) : query.isError ? (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>
              Your settings could not be loaded — {errorMessage(query.error)}
            </AlertDescription>
            <AlertAction>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void query.refetch()}
              >
                Try again
              </Button>
            </AlertAction>
          </Alert>
        ) : (
          <SettingsForm settings={query.data} />
        )}
        <AppearanceSection />
      </PageContainer>
    </PageShell>
  )
}

/** A titled group of setting rows on one quiet surface. */
function SettingsSection({
  title,
  description,
  children,
  footer,
}: {
  title: string
  description: string
  children: React.ReactNode
  footer?: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-3" aria-label={title}>
      <div className="flex flex-col gap-0.5 px-1">
        <h2 className="text-title">{title}</h2>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      <div className="overflow-hidden rounded-xl border bg-card">
        <div className="divide-y">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t bg-muted/50 px-5 py-3">
            {footer}
          </div>
        )}
      </div>
    </section>
  )
}

/** Label and help on the left, the control on the right; stacked when
 *  narrow. */
function SettingRow({
  label,
  htmlFor,
  description,
  children,
}: {
  label: React.ReactNode
  htmlFor?: string
  description?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-3 px-5 py-5 @2xl/main:flex-row @2xl/main:gap-8">
      <div className="flex flex-col gap-1 @2xl/main:w-56 @2xl/main:shrink-0 @2xl/main:pt-2.5">
        {htmlFor ? (
          <label htmlFor={htmlFor} className="text-label">
            {label}
          </label>
        ) : (
          <span className="text-label">{label}</span>
        )}
        {description && (
          <p className="text-xs leading-relaxed text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">{children}</div>
    </div>
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
      toast.success("Settings saved — they apply to new interviews.")
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
    <form
      onSubmit={(event) => {
        event.preventDefault()
        form.handleSubmit()
      }}
    >
      <SettingsSection
        title="Interviewer"
        description="Who runs your interviews by default. Applies to every interview created from now on; each new interview can still change it."
        footer={
          <>
            {mutation.isError && (
              <p role="alert" className="mr-auto text-sm text-destructive">
                {errorMessage(mutation.error)}
              </p>
            )}
            <form.Subscribe selector={(state) => state.isDirty}>
              {(dirty) => (
                <Button
                  type="button"
                  variant="ghost"
                  disabled={!dirty || mutation.isPending}
                  onClick={() => form.reset()}
                >
                  Discard changes
                </Button>
              )}
            </form.Subscribe>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? "Saving…" : "Save"}
            </Button>
          </>
        }
      >
        <form.Field
          name="agent_name"
          children={(field) => {
            const isInvalid =
              field.state.meta.isTouched && !field.state.meta.isValid
            return (
              <SettingRow
                label="Agent name"
                htmlFor={field.name}
                description="How the interviewer introduces themselves."
              >
                <Input
                  id={field.name}
                  name={field.name}
                  value={field.state.value}
                  onBlur={field.handleBlur}
                  onChange={(event) => field.handleChange(event.target.value)}
                  aria-invalid={isInvalid}
                  disabled={mutation.isPending}
                  autoComplete="off"
                />
                {isInvalid && <FieldError errors={field.state.meta.errors} />}
              </SettingRow>
            )
          }}
        />

        <form.Field
          name="language"
          children={(field) => {
            const isInvalid =
              field.state.meta.isTouched && !field.state.meta.isValid
            return (
              <SettingRow
                label="Language"
                htmlFor={field.name}
                description="The interview is conducted in this language."
              >
                <Select
                  value={field.state.value}
                  onValueChange={(next) => {
                    if (!next) return
                    field.handleChange(next)
                    // Swap the voice list to the new language and reset the
                    // selection to that language's first voice.
                    const firstVoice = settings.voices[next]?.[0]
                    if (firstVoice) form.setFieldValue("voice", firstVoice.id)
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
                {isInvalid && <FieldError errors={field.state.meta.errors} />}
              </SettingRow>
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
                    <SettingRow
                      label="Voice"
                      htmlFor={field.name}
                      description="What the interviewer sounds like."
                    >
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
                    </SettingRow>
                  )
                }}
              />
            )
          }}
        </form.Subscribe>

        <form.Field
          name="persona"
          children={(field) => (
            <SettingRow
              label="Persona"
              htmlFor={field.name}
              description="Optional — the interviewer’s personality."
            >
              <Textarea
                id={field.name}
                name={field.name}
                value={field.state.value}
                onBlur={field.handleBlur}
                onChange={(event) => field.handleChange(event.target.value)}
                disabled={mutation.isPending}
                placeholder="A warm but rigorous engineering manager…"
                className="max-h-48 min-h-20 overflow-y-auto"
              />
            </SettingRow>
          )}
        />

        <form.Field
          name="custom_instructions"
          children={(field) => (
            <SettingRow
              label="Custom instructions"
              htmlFor={field.name}
              description="Optional — extra guidance the interviewer keeps in mind."
            >
              <Textarea
                id={field.name}
                name={field.name}
                value={field.state.value}
                onBlur={field.handleBlur}
                onChange={(event) => field.handleChange(event.target.value)}
                disabled={mutation.isPending}
                placeholder="Focus on system design…"
                className="max-h-48 min-h-24 overflow-y-auto"
              />
            </SettingRow>
          )}
        />
      </SettingsSection>
    </form>
  )
}

const THEMES = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
  { value: "system", label: "Device default", icon: MonitorIcon },
] as const

/** Theme lives on this device only; it applies immediately. The live
 *  interview room is always dark. */
function AppearanceSection() {
  const { theme, setTheme } = useTheme()
  const [mounted, setMounted] = React.useState(false)
  React.useEffect(() => setMounted(true), [])
  const current = mounted ? (theme ?? "system") : undefined

  return (
    <SettingsSection
      title="Appearance"
      description="Saved on this device and applied right away. The live interview room is always dark."
    >
      <SettingRow label="Theme">
        <div
          role="radiogroup"
          aria-label="Theme"
          className="grid grid-cols-3 gap-2 @lg/main:max-w-md"
        >
          {THEMES.map(({ value, label, icon: Icon }) => {
            const checked = current === value
            return (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={checked}
                disabled={!mounted}
                onClick={() => setTheme(value)}
                className={cn(
                  "flex flex-col items-center gap-2 rounded-lg border px-3 py-3 text-sm transition-colors hover:bg-foreground/[0.04] disabled:opacity-40",
                  checked &&
                    "border-primary bg-primary-container/60 text-on-primary-container hover:bg-primary-container/60"
                )}
              >
                <Icon aria-hidden className="size-5" />
                {label}
              </button>
            )
          })}
        </div>
      </SettingRow>
    </SettingsSection>
  )
}
