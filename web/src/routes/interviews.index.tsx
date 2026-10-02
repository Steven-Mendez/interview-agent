import { Link, createFileRoute, useNavigate } from "@tanstack/react-router"
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import {
  AlertCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  FilterIcon,
  HistoryIcon,
  PlusIcon,
  RotateCcwIcon,
  SearchXIcon,
} from "lucide-react"

import {
  EvaluationSummary,
  InterviewStatusChip,
  STATUS_META,
} from "@/components/interview-status"
import { Mascot } from "@/components/mascot"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { EmptyState } from "@/components/ui/empty-state"
import { IconButton } from "@/components/ui/icon-button"
import { LinkButton } from "@/components/ui/link-button"
import { PageContainer, PageHeader, PageShell } from "@/components/ui/page"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  ApiError,
  SENIORITY_LABELS,
  durationLabel,
  repeatInterview,
} from "@/lib/api"
import type { InterviewStatus, InterviewSummary } from "@/lib/api"
import {
  HISTORY_PAGE_SIZE,
  interviewQueryOptions,
  interviewsQueryOptions,
} from "@/lib/queries"
import { log } from "@/lib/log"
import { cn } from "@/lib/utils"
import { pageHead } from "@/lib/head"

// The filter and the page live in the URL: a history you can link to, and a
// back button that returns to the page you were actually on.
const STATUS_FILTERS = [
  "created",
  "planned",
  "interviewing",
  "closing",
  "completed",
  "evaluating",
  "evaluated",
  "evaluation_failed",
  "error",
] as const

// Both optional: the defaults are the first page, unfiltered, so plain
// `<Link to="/interviews">` (navigation, results) needs no search params.
interface HistorySearch {
  offset?: number
  status?: InterviewStatus
}

export const Route = createFileRoute("/interviews/")({
  head: () => pageHead("History"),
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
  const queryClient = useQueryClient()

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
      // The response is the new row: seed its detail so the landing needs no
      // fetch, and drop every cached page of this list — the one on screen
      // right now stays fresh for 10 s, and Back would show it without the
      // interview just planned.
      queryClient.setQueryData(
        interviewQueryOptions(interview.id).queryKey,
        interview
      )
      void queryClient.invalidateQueries({ queryKey: ["interviews"] })
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
  // The URL can name any page — one beyond the end (a stale link, a row
  // deleted since) comes back empty although the history is not.
  const lastOffset =
    total > 0
      ? Math.floor((total - 1) / HISTORY_PAGE_SIZE) * HISTORY_PAGE_SIZE
      : 0
  const pastEnd =
    page !== undefined &&
    !query.isPlaceholderData &&
    page.items.length === 0 &&
    total > 0

  const goTo = (nextOffset: number) =>
    void navigate({
      search: { offset: nextOffset > 0 ? nextOffset : undefined, status },
    })

  const setStatus = (next: InterviewStatus | undefined) =>
    // Any filter change resets to the first page: page 3 of the old filter
    // is rarely page 3 of the new one.
    void navigate({ search: { offset: undefined, status: next } })

  const rowProps = (item: InterviewSummary) => ({
    item,
    onRepeat: () => repeat.mutate(item.id),
    repeating: repeat.isPending && repeat.variables === item.id,
    disabled: repeat.isPending,
  })

  return (
    <PageShell>
      <PageContainer variant="wide" className="flex flex-col gap-6">
        <PageHeader
          title="History"
          description="Your interviews and their results."
        />

        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={status ?? "all"}
            onValueChange={(next) =>
              next &&
              setStatus(next === "all" ? undefined : (next as InterviewStatus))
            }
          >
            <SelectTrigger
              size="sm"
              aria-label="Filter by status"
              className={cn(
                "gap-2",
                status &&
                  "border-transparent bg-primary-container text-on-primary-container"
              )}
            >
              <FilterIcon className="size-4" />
              <SelectValue>
                {(value: string) =>
                  value === "all"
                    ? "All statuses"
                    : STATUS_META[value as InterviewStatus].label
                }
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {STATUS_FILTERS.map((value) => (
                <SelectItem key={value} value={value}>
                  {STATUS_META[value].label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {status && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setStatus(undefined)}
            >
              Clear filter
            </Button>
          )}
          {total > 0 && !pastEnd && (
            <span className="ml-auto text-xs text-muted-foreground tabular-nums">
              {total} {total === 1 ? "interview" : "interviews"}
            </span>
          )}
        </div>

        {repeat.isError && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>{errorMessage(repeat.error)}</AlertDescription>
          </Alert>
        )}
        {query.isError && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>{errorMessage(query.error)}</AlertDescription>
          </Alert>
        )}

        {query.isPending ? (
          <HistorySkeleton />
        ) : pastEnd ? (
          <EmptyState
            icon={<HistoryIcon />}
            title="Nothing this far down"
            description={`The history ends at page ${Math.floor(lastOffset / HISTORY_PAGE_SIZE) + 1}.`}
            actions={
              <Button variant="outline" onClick={() => goTo(lastOffset)}>
                <ChevronLeftIcon />
                Go to the last page
              </Button>
            }
          />
        ) : page && page.items.length === 0 ? (
          status !== undefined ? (
            <EmptyState
              icon={<SearchXIcon />}
              title="No interviews in this state"
              description={`Nothing is “${STATUS_META[status].label}” right now.`}
              actions={
                <Button variant="outline" onClick={() => setStatus(undefined)}>
                  Show all interviews
                </Button>
              }
            />
          ) : (
            <EmptyState
              illustration={<Mascot state="idle" className="w-36" />}
              title="No interviews yet"
              description="Interviews you run appear here with their results."
              actions={
                <LinkButton to="/new" variant="create">
                  <PlusIcon />
                  Start an interview
                </LinkButton>
              }
            />
          )
        ) : (
          page && (
            <div
              className={cn(
                "transition-opacity duration-150",
                query.isPlaceholderData && "opacity-60"
              )}
              aria-busy={query.isPlaceholderData}
            >
              {/* Wide: a quiet table. Narrow: the same rows as cards. */}
              <div className="hidden overflow-hidden rounded-xl border @4xl/main:block">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[40%]">Interview</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Result</TableHead>
                      <TableHead>Level</TableHead>
                      <TableHead>Topics</TableHead>
                      <TableHead>Date</TableHead>
                      <TableHead>
                        <span className="sr-only">Actions</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {page.items.map((item) => (
                      <HistoryTableRow key={item.id} {...rowProps(item)} />
                    ))}
                  </TableBody>
                </Table>
              </div>
              <ul className="flex flex-col gap-3 @4xl/main:hidden">
                {page.items.map((item) => (
                  <HistoryCard key={item.id} {...rowProps(item)} />
                ))}
              </ul>
            </div>
          )
        )}

        {total > HISTORY_PAGE_SIZE && !pastEnd && (
          <nav
            aria-label="Pagination"
            className="flex items-center justify-end gap-2"
          >
            <span className="text-sm text-muted-foreground tabular-nums">
              {from}–{to} of {total}
            </span>
            <IconButton
              label="Previous page"
              disabled={offset === 0}
              onClick={() => goTo(offset - HISTORY_PAGE_SIZE)}
            >
              <ChevronLeftIcon />
            </IconButton>
            <IconButton
              label="Next page"
              disabled={to >= total}
              onClick={() => goTo(offset + HISTORY_PAGE_SIZE)}
            >
              <ChevronRightIcon />
            </IconButton>
          </nav>
        )}
      </PageContainer>
    </PageShell>
  )
}

interface RowProps {
  item: InterviewSummary
  onRepeat: () => void
  repeating: boolean
  disabled: boolean
}

function TitleCell({ item }: { item: InterviewSummary }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="flex min-w-0 items-center gap-2">
        <Link
          to="/interviews/$interviewId"
          params={{ interviewId: item.id }}
          className="truncate text-sm font-medium text-foreground after:absolute after:inset-0 hover:underline focus-visible:outline-offset-[-2px]"
        >
          {item.title}
        </Link>
        {item.repeat_of_id && (
          <Badge
            variant="outline"
            className="relative h-5 px-2"
            title="A re-run of an earlier interview"
          >
            <RotateCcwIcon aria-hidden />
            Re-run
          </Badge>
        )}
      </span>
      <span className="truncate text-xs text-muted-foreground">
        {durationLabel(item.interview_length, item.max_minutes)}
        {item.resume_filename && ` · ${item.resume_filename}`}
      </span>
    </div>
  )
}

function RepeatButton({
  onRepeat,
  repeating,
  disabled,
}: Omit<RowProps, "item">) {
  return (
    <IconButton
      label={
        repeating
          ? "Planning…"
          : "Repeat — plan a fresh interview for the same role and resume"
      }
      size="icon-sm"
      onClick={onRepeat}
      disabled={disabled}
      className="relative z-10"
    >
      <RotateCcwIcon className={cn(repeating && "animate-spin")} />
    </IconButton>
  )
}

/** The whole row opens the interview (the title link stretches over it);
 *  the repeat action sits above that layer. */
function HistoryTableRow({ item, ...actions }: RowProps) {
  return (
    <TableRow className="relative">
      <TableCell className="max-w-0">
        <TitleCell item={item} />
      </TableCell>
      <TableCell>
        <InterviewStatusChip row={item} />
      </TableCell>
      <TableCell>
        {item.evaluation ? (
          <EvaluationSummary evaluation={item.evaluation} />
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell className="text-muted-foreground">
        {SENIORITY_LABELS[item.seniority]}
        {item.seniority_source === "detected" && " · auto"}
      </TableCell>
      <TableCell className="text-muted-foreground tabular-nums">
        {item.milestones_total > 0
          ? `${item.milestones_completed}/${item.milestones_total}`
          : "—"}
      </TableCell>
      <TableCell className="text-muted-foreground">
        <time dateTime={item.created_at}>
          {dateFormat.format(new Date(item.created_at))}
        </time>
      </TableCell>
      <TableCell className="w-12 text-right">
        <RepeatButton {...actions} />
      </TableCell>
    </TableRow>
  )
}

function HistoryCard({ item, ...actions }: RowProps) {
  return (
    <li className="relative flex flex-col gap-3 rounded-xl border bg-card p-4 transition-colors hover:bg-foreground/[0.03]">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <TitleCell item={item} />
        </div>
        <RepeatButton {...actions} />
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
        <InterviewStatusChip row={item} />
        <EvaluationSummary evaluation={item.evaluation} />
        <span>{SENIORITY_LABELS[item.seniority]}</span>
        {item.milestones_total > 0 && (
          <span className="tabular-nums">
            {item.milestones_completed}/{item.milestones_total} topics
          </span>
        )}
        <time dateTime={item.created_at}>
          {dateFormat.format(new Date(item.created_at))}
        </time>
      </div>
    </li>
  )
}

function HistorySkeleton() {
  return (
    <div className="flex flex-col divide-y overflow-hidden rounded-xl border">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="flex items-center gap-6 px-4 py-4">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-72 max-w-full" />
            <Skeleton className="h-3 w-40" />
          </div>
          <Skeleton className="h-6 w-24 rounded-full" />
          <Skeleton className="hidden h-4 w-24 @3xl/main:block" />
        </div>
      ))}
    </div>
  )
}
