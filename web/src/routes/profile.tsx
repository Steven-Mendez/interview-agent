import { createFileRoute } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import {
  AlertCircleIcon,
  ArrowRightIcon,
  LogOutIcon,
  ShieldCheckIcon,
} from "lucide-react"

import { Alert, AlertAction, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { LinkButton } from "@/components/ui/link-button"
import {
  PageContainer,
  PageHeader,
  PageShell,
  Section,
} from "@/components/ui/page"
import { Skeleton } from "@/components/ui/skeleton"
import { useMe } from "@/hooks/use-me"
import { useSignOut } from "@/hooks/use-sign-out"
import {
  AUTH_PROVIDER_LABELS,
  formatDate,
  formatDateTime,
  localUsername,
} from "@/lib/accounts"
import { ApiError } from "@/lib/api"
import type { Me } from "@/lib/api"
import { useAuth, useAuthMode } from "@/lib/auth"
import type { AuthMode } from "@/lib/auth"
import { pageHead } from "@/lib/head"
import { interviewCountQueryOptions } from "@/lib/queries"
import { requireSession } from "@/lib/route-guards"

export const Route = createFileRoute("/profile")({
  beforeLoad: ({ location }) => requireSession(location),
  head: () => pageHead("Profile"),
  component: ProfilePage,
})

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function displayName(me: Me): string {
  return me.name ?? localUsername(me.id) ?? me.email ?? "Your account"
}

function signInMethod(me: Me, mode: AuthMode | undefined): string {
  if (mode === "none") return "Local developer (no sign-in)"
  return AUTH_PROVIDER_LABELS[me.auth_provider]
}

function ProfilePage() {
  const me = useMe()
  const mode = useAuthMode()
  const { user } = useAuth()
  const signOut = useSignOut()
  const stored = useQuery(interviewCountQueryOptions())

  return (
    <PageShell>
      <PageContainer className="flex flex-col gap-8">
        <PageHeader
          title="Profile"
          description="Your account and your interviews."
          actions={
            mode !== "none" &&
            user && (
              <Button variant="outline" onClick={() => void signOut(user.id)}>
                <LogOutIcon />
                Sign out
              </Button>
            )
          }
        />

        {me.isError ? (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>
              Your profile could not be loaded — {errorMessage(me.error)}
            </AlertDescription>
            <AlertAction>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void me.refetch()}
              >
                Try again
              </Button>
            </AlertAction>
          </Alert>
        ) : !me.data ? (
          <ProfileSkeleton />
        ) : (
          <>
            <Section title="Account">
              <dl className="divide-y overflow-hidden rounded-xl border">
                <Row label="Name">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="break-all">{displayName(me.data)}</span>
                    {me.data.is_admin && (
                      <Badge>
                        <ShieldCheckIcon aria-hidden />
                        Admin
                      </Badge>
                    )}
                  </span>
                </Row>
                {me.data.email && (
                  <Row label="Email">
                    <span className="break-all">{me.data.email}</span>
                  </Row>
                )}
                <Row label="Sign-in method">{signInMethod(me.data, mode)}</Row>
                <Row label="Member since">
                  <time dateTime={me.data.created_at}>
                    {formatDate(me.data.created_at)}
                  </time>
                </Row>
                <Row label="Last seen">
                  {me.data.last_seen_at ? (
                    <time dateTime={me.data.last_seen_at}>
                      {formatDateTime(me.data.last_seen_at)}
                    </time>
                  ) : (
                    "—"
                  )}
                </Row>
              </dl>
            </Section>

            <Section title="Interviews">
              <dl className="divide-y overflow-hidden rounded-xl border">
                <Row label="Quota">
                  <Quota me={me.data} />
                </Row>
                <Row label="In your history">
                  <span className="flex flex-wrap items-center justify-between gap-2">
                    <span className="tabular-nums">
                      {stored.data === undefined
                        ? stored.isError
                          ? "—"
                          : "…"
                        : `${stored.data} ${stored.data === 1 ? "interview" : "interviews"}`}
                    </span>
                    <LinkButton to="/interviews" variant="ghost" size="sm">
                      View history
                      <ArrowRightIcon />
                    </LinkButton>
                  </span>
                </Row>
              </dl>
            </Section>
          </>
        )}
      </PageContainer>
    </PageShell>
  )
}

function Quota({ me }: { me: Me }) {
  if (me.is_admin || me.interview_limit === null) {
    return <>Unlimited interviews</>
  }
  const remaining =
    me.interviews_remaining ??
    Math.max(0, me.interview_limit - me.interviews_used)
  return (
    <span className="flex flex-col gap-0.5">
      <span className="tabular-nums">
        {me.interviews_used} of {me.interview_limit} interviews used
      </span>
      <span className="text-xs text-muted-foreground tabular-nums">
        {remaining} remaining
      </span>
    </span>
  )
}

function Row({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="grid gap-1 bg-card px-4 py-3 @md/main:grid-cols-[10rem_minmax(0,1fr)] @md/main:items-center @md/main:gap-4">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-sm text-foreground">{children}</dd>
    </div>
  )
}

function ProfileSkeleton() {
  return (
    <div className="flex flex-col divide-y overflow-hidden rounded-xl border">
      {Array.from({ length: 4 }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-4">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-4 w-48 max-w-full" />
        </div>
      ))}
    </div>
  )
}
