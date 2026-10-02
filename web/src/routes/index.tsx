import { Link, createFileRoute } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import {
  AlertCircleIcon,
  ArrowRightIcon,
  ChevronRightIcon,
  HistoryIcon,
  MicIcon,
  PlayIcon,
  PlusIcon,
} from "lucide-react"

import {
  EvaluationSummary,
  InterviewStatusChip,
} from "@/components/interview-status"
import { HomeMascot } from "@/components/home-mascot"
import { Mascot } from "@/components/mascot"
import { Alert, AlertAction, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { LinkButton } from "@/components/ui/link-button"
import { PageContainer, PageShell, Section } from "@/components/ui/page"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError, SENIORITY_LABELS, durationLabel } from "@/lib/api"
import type { InterviewSummary } from "@/lib/api"
import { recentInterviewsQueryOptions } from "@/lib/queries"
import { pageHead } from "@/lib/head"

export const Route = createFileRoute("/")({
  head: () => pageHead(),
  component: HomePage,
})

const RECENT_LIMIT = 5

const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
})

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function HomePage() {
  const recent = useQuery(recentInterviewsQueryOptions({ limit: RECENT_LIMIT }))
  const planned = useQuery(
    recentInterviewsQueryOptions({ limit: 5, status: "planned" })
  )
  const interviewing = useQuery(
    recentInterviewsQueryOptions({ limit: 5, status: "interviewing" })
  )

  // Ready = planned and never started, or left mid-call with the room still
  // waiting. An `interviewing` row past its window cannot be joined.
  const ready = [
    ...(interviewing.data?.items.filter((row) => row.can_start) ?? []),
    ...(planned.data?.items ?? []),
  ]
  const readyIds = new Set(ready.map((row) => row.id))
  const latest = recent.data?.items.filter((row) => !readyIds.has(row.id))
  const total = recent.data?.total
  const continueRow = ready.at(0)

  return (
    <PageShell className="gap-12 @lg/main:py-8">
      <PageContainer variant="wide" className="flex flex-col gap-10">
        {/* Hero: one obvious action, and a way back into what is waiting. */}
        <section className="grid items-center gap-10 @4xl/main:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <div className="flex flex-col gap-6">
            <h1 className="font-heading text-[2.25rem] leading-[2.75rem] font-normal @2xl/main:text-[2.75rem] @2xl/main:leading-[3.25rem]">
              Practice interviews with your AI interviewer
            </h1>
            <p className="max-w-xl text-base leading-relaxed text-muted-foreground">
              Planned around your resume and the role, by voice, with feedback
              on every answer.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <LinkButton to="/new" size="lg" variant="create">
                <PlusIcon />
                New interview
              </LinkButton>
              {continueRow ? (
                <LinkButton
                  to="/interviews/$interviewId"
                  params={{ interviewId: continueRow.id }}
                  size="lg"
                  className="max-w-full"
                >
                  <PlayIcon />
                  <span className="truncate">
                    {continueRow.status === "interviewing"
                      ? "Rejoin interview"
                      : "Continue to preparation"}
                  </span>
                </LinkButton>
              ) : (
                <LinkButton to="/interviews" size="lg" variant="ghost">
                  <HistoryIcon />
                  View history
                </LinkButton>
              )}
            </div>
          </div>
          <div aria-hidden className="hidden justify-center @4xl/main:flex">
            <div className="flex aspect-square w-64 items-center justify-center rounded-full bg-primary-container/45">
              <HomeMascot className="w-44" />
            </div>
          </div>
        </section>

        {recent.isError && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>
              Your interviews could not be loaded — {errorMessage(recent.error)}
            </AlertDescription>
            <AlertAction>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void recent.refetch()}
              >
                Try again
              </Button>
            </AlertAction>
          </Alert>
        )}

        {ready.length > 0 && (
          <Section title="Ready to start">
            <ul className="divide-y overflow-hidden rounded-xl border">
              {ready.map((row) => (
                <ReadyRow key={row.id} row={row} />
              ))}
            </ul>
          </Section>
        )}

        {recent.isPending ? (
          <Section title="Recent interviews">
            <RowsSkeleton />
          </Section>
        ) : total === 0 ? (
          <EmptyState
            illustration={<Mascot state="idle" className="w-36" />}
            title="No interviews yet"
            description="Start a new interview and your interviewer will plan it from your resume."
            actions={
              <LinkButton to="/new" variant="create">
                <PlusIcon />
                Start your first interview
              </LinkButton>
            }
          />
        ) : latest && latest.length > 0 ? (
          <Section
            title="Recent interviews"
            actions={
              <LinkButton to="/interviews" variant="ghost" size="sm">
                View all
                <ArrowRightIcon />
              </LinkButton>
            }
          >
            <ul className="divide-y overflow-hidden rounded-xl border">
              {latest.map((row) => (
                <RecentRow key={row.id} row={row} />
              ))}
            </ul>
          </Section>
        ) : null}
      </PageContainer>
    </PageShell>
  )
}

function RowMeta({ row }: { row: InterviewSummary }) {
  return (
    <span className="truncate text-xs text-muted-foreground">
      {SENIORITY_LABELS[row.seniority]} ·{" "}
      {durationLabel(row.interview_length, row.max_minutes)} ·{" "}
      <time dateTime={row.created_at}>
        {dateFormat.format(new Date(row.created_at))}
      </time>
    </span>
  )
}

function ReadyRow({ row }: { row: InterviewSummary }) {
  return (
    <li className="flex items-center gap-4 bg-card px-4 py-3">
      <span
        aria-hidden
        className="flex size-10 shrink-0 items-center justify-center rounded-full bg-primary-container text-on-primary-container"
      >
        <MicIcon className="size-5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate text-sm font-medium">{row.title}</span>
        <RowMeta row={row} />
      </div>
      <LinkButton
        to="/interviews/$interviewId"
        params={{ interviewId: row.id }}
        variant="tonal"
        size="sm"
        aria-label={`${row.status === "interviewing" ? "Rejoin" : "Prepare"}: ${row.title}`}
      >
        {row.status === "interviewing" ? "Rejoin" : "Prepare"}
      </LinkButton>
    </li>
  )
}

function RecentRow({ row }: { row: InterviewSummary }) {
  return (
    <li>
      <Link
        to="/interviews/$interviewId"
        params={{ interviewId: row.id }}
        className="group flex items-center gap-4 bg-card px-4 py-3 transition-colors hover:bg-foreground/[0.04] focus-visible:-outline-offset-2"
      >
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="truncate text-sm font-medium">{row.title}</span>
          <RowMeta row={row} />
        </div>
        <EvaluationSummary
          evaluation={row.evaluation}
          className="hidden @xl/main:flex"
        />
        <InterviewStatusChip
          row={row}
          className="hidden @md/main:inline-flex"
        />
        <ChevronRightIcon
          aria-hidden
          className="size-5 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5"
        />
      </Link>
    </li>
  )
}

function RowsSkeleton() {
  return (
    <div className="flex flex-col divide-y overflow-hidden rounded-xl border">
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-4">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-64 max-w-full" />
            <Skeleton className="h-3 w-40" />
          </div>
          <Skeleton className="h-6 w-20 rounded-full" />
        </div>
      ))}
    </div>
  )
}
