import { Link, createFileRoute, useNavigate } from "@tanstack/react-router"
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query"
import {
  ArrowRightIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  GaugeIcon,
  HistoryIcon,
  ListChecksIcon,
  MicIcon,
  RotateCcwIcon,
} from "lucide-react"

import {
  ApiError,
  LENGTH_LABELS,
  SENIORITY_LABELS,
  repeatInterview,
} from "@/lib/api"
import type { InterviewStatus, InterviewSummary } from "@/lib/api"
import { HISTORY_PAGE_SIZE, interviewsQueryOptions } from "@/lib/queries"
import { log } from "@/lib/log"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { PageContainer, PageShell } from "@/components/ui/page"

// The filter and the page live in the URL: a history you can link to, and a
// back button that returns to the page you were actually on.
const STATUS_FILTERS = [
  "created",
  "planned",
  "interviewing",
  "completed",
  "evaluated",
  "evaluation_failed",
  "error",
] as const

// Both optional: the defaults are the first page, unfiltered, so plain
// `<Link to="/interviews">` (sidebar, scorecard) needs no search params.
interface HistorySearch {
  offset?: number
  status?: InterviewStatus
}

export const Route = createFileRoute("/interviews/")({
  validateSearch: (search: Record<string, unknown>): HistorySearch => {
    const offset = Number(search.offset)
    const status = String(search.status ?? "")
    return {
      offset:
        Number.isFinite(offset) && offset > 0 ? Math.floor(offset) : undefined,
      status: STATUS_FILTERS.includes(status as (typeof STATUS_FILTERS)[number])
        ? (status as InterviewStatus)
        : undefined,
    }
  },
  component: HistoryPage,
})

/** How each status reads, and the badge tone that carries it. */
const STATUS_META: Record<
  InterviewStatus,
  {
    label: string
    variant: "outline" | "secondary" | "destructive" | "default"
  }
> = {
  created: { label: "Planning…", variant: "outline" },
  planned: { label: "Ready to start", variant: "outline" },
  interviewing: { label: "In progress", variant: "default" },
  completed: { label: "Evaluating…", variant: "secondary" },
  evaluated: { label: "Evaluated", variant: "secondary" },
  evaluation_failed: { label: "Evaluation failed", variant: "destructive" },
  error: { label: "Failed", variant: "destructive" },
}

const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function HistoryPage() {
  const { offset = 0, status } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })

  const query = useQuery({
    ...interviewsQueryOptions({ offset, status }),
    // Paging swaps the query key; without this the list would blank out
    // between pages instead of dimming the one already on screen.
    placeholderData: keepPreviousData,
  })

  // Repeating from the list: plan a fresh run off the same resume and offer,
  // then jump straight into it — same landing as creating one from scratch.
  const repeat = useMutation({
    mutationFn: (interviewId: string) => repeatInterview(interviewId),
    onSuccess: (interview) => {
      log("interview repeated:", interview.id)
      void navigate({
        to: "/interviews/$interviewId",
        params: { interviewId: interview.id },
      })
    },
    onError: (error) => {
      console.error("[app] repeat failed:", error)
    },
  })

  const page = query.data
  const total = page?.total ?? 0
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + HISTORY_PAGE_SIZE, total)

  const goTo = (nextOffset: number) =>
    void navigate({
      search: { offset: nextOffset > 0 ? nextOffset : undefined, status },
    })

  return (
    <PageShell>
      <PageContainer variant="wide" className="flex flex-col gap-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h1 className="flex items-center gap-2 text-lg font-medium">
              <HistoryIcon className="size-5 text-muted-foreground" />
              Interview history
            </h1>
            <p className="text-sm text-muted-foreground">
              Every interview you have run — open one to review its scorecard
              and transcript, or run the same role again.
            </p>
          </div>
          <Select
            value={status ?? "all"}
            onValueChange={(next) =>
              next &&
              // Any filter change resets to the first page: page 3 of the old
              // filter is rarely page 3 of the new one.
              void navigate({
                search: {
                  offset: undefined,
                  status:
                    next === "all" ? undefined : (next as InterviewStatus),
                },
              })
            }
          >
            <SelectTrigger className="w-52">
              <SelectValue>
                {(value: string) =>
                  value === "all"
                    ? "All interviews"
                    : STATUS_META[value as InterviewStatus].label
                }
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All interviews</SelectItem>
              {STATUS_FILTERS.map((value) => (
                <SelectItem key={value} value={value}>
                  {STATUS_META[value].label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {repeat.isError && (
          <Alert variant="destructive">
            <AlertDescription>{errorMessage(repeat.error)}</AlertDescription>
          </Alert>
        )}
        {query.isError && (
          <Alert variant="destructive">
            <AlertDescription>{errorMessage(query.error)}</AlertDescription>
          </Alert>
        )}

        {query.isPending ? (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 4 }, (_, i) => (
              <Card key={i}>
                <CardContent className="flex flex-col gap-3">
                  <Skeleton className="h-5 w-64" />
                  <Skeleton className="h-4 w-96 max-w-full" />
                </CardContent>
              </Card>
            ))}
          </div>
        ) : page && page.items.length === 0 ? (
          <EmptyState filtered={status !== undefined} />
        ) : (
          <div
            className={cn(
              "flex flex-col gap-3",
              query.isPlaceholderData && "opacity-60"
            )}
          >
            {page?.items.map((item) => (
              <HistoryRow
                key={item.id}
                item={item}
                onRepeat={() => repeat.mutate(item.id)}
                repeating={repeat.isPending && repeat.variables === item.id}
                disabled={repeat.isPending}
              />
            ))}
          </div>
        )}

        {total > HISTORY_PAGE_SIZE && (
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm text-muted-foreground tabular-nums">
              {from}–{to} of {total}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={offset === 0}
                onClick={() => goTo(offset - HISTORY_PAGE_SIZE)}
              >
                <ChevronLeftIcon />
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={to >= total}
                onClick={() => goTo(offset + HISTORY_PAGE_SIZE)}
              >
                Next
                <ChevronRightIcon />
              </Button>
            </div>
          </div>
        )}
      </PageContainer>
    </PageShell>
  )
}

function EmptyState({ filtered }: { filtered: boolean }) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
        <span className="flex size-12 items-center justify-center rounded-full bg-primary/10 text-primary">
          <MicIcon className="size-6" />
        </span>
        <p className="text-sm text-muted-foreground">
          {filtered
            ? "No interviews in this state yet."
            : "You have not run any interviews yet."}
        </p>
        <Button render={<Link to="/" />}>Start an interview</Button>
      </CardContent>
    </Card>
  )
}

function HistoryRow({
  item,
  onRepeat,
  repeating,
  disabled,
}: {
  item: InterviewSummary
  onRepeat: () => void
  repeating: boolean
  disabled: boolean
}) {
  const meta = STATUS_META[item.status]
  const evaluation = item.evaluation
  // Same traffic light as the scorecard: green once hired, otherwise red/amber
  // by how far the score sits from a pass.
  const scoreClass = evaluation
    ? evaluation.hired
      ? "text-success"
      : evaluation.score < 40
        ? "text-destructive"
        : "text-warning"
    : ""

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 @2xl/main:flex-row @2xl/main:items-center">
        {evaluation && (
          <div className="flex shrink-0 items-baseline gap-1 @2xl/main:w-24">
            <span className={cn("text-3xl leading-none font-bold", scoreClass)}>
              {evaluation.score}
            </span>
            <span className="text-sm text-muted-foreground">/100</span>
          </div>
        )}

        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to="/interviews/$interviewId"
              params={{ interviewId: item.id }}
              className="truncate text-sm font-medium hover:underline"
            >
              {item.title}
            </Link>
            <Badge variant={meta.variant}>{meta.label}</Badge>
            {evaluation && (
              <Badge variant={evaluation.hired ? "secondary" : "outline"}>
                {evaluation.hired ? "Hired" : "Not hired"}
              </Badge>
            )}
            {item.repeat_of_id && (
              <Badge variant="outline" title="A re-run of an earlier interview">
                <RotateCcwIcon />
                Re-run
              </Badge>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <ClockIcon className="size-3.5" />
              <time dateTime={item.created_at}>
                {dateFormat.format(new Date(item.created_at))}
              </time>
            </span>
            <span className="flex items-center gap-1.5">
              <GaugeIcon className="size-3.5" />
              {SENIORITY_LABELS[item.seniority]}
              {item.seniority_source === "detected" && " · auto"}
            </span>
            <span>{LENGTH_LABELS[item.interview_length]}</span>
            {item.milestones_total > 0 && (
              <span className="flex items-center gap-1.5 tabular-nums">
                <ListChecksIcon className="size-3.5" />
                {item.milestones_completed}/{item.milestones_total} topics
              </span>
            )}
            {item.resume_filename && (
              <span className="truncate">{item.resume_filename}</span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onRepeat}
            disabled={disabled}
            title="Plan a fresh interview for the same role and resume"
          >
            <RotateCcwIcon />
            {repeating ? "Planning…" : "Repeat"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            render={
              <Link
                to="/interviews/$interviewId"
                params={{ interviewId: item.id }}
              />
            }
          >
            Open
            <ArrowRightIcon />
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
