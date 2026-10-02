import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import {
  AlertCircleIcon,
  CalendarIcon,
  ListFilterIcon,
  XIcon,
} from "lucide-react"

import { getMetrics, getMetricTraces, getExternalDeletions } from "@/lib/api"
import type { MetricFilters } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Mascot } from "@/components/mascot"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import {
  PageContainer,
  PageHeader,
  PageShell,
  Section,
} from "@/components/ui/page"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { pageHead } from "@/lib/head"

export const Route = createFileRoute("/metrics")({
  head: () => pageHead("Metrics"),
  component: MetricsPage,
})

const FILTER_LABELS = {
  graph_version: "Graph version",
  model: "Model",
  language: "Language",
  seniority: "Level",
  length: "Duration profile",
} as const
type FilterKey = keyof typeof FILTER_LABELS

const PERIODS = [7, 30, 90, 365] as const
// Select items need a value; this one stands for "no filter".
const ALL = "__all"

function formatValue(value: number | null, name: string) {
  if (value === null) return "Unavailable"
  if (name.endsWith("seconds")) return `${value.toFixed(3)} s`
  if (name.endsWith("usd")) return `$${value.toFixed(5)}`
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(
    value
  )
}

const integer = new Intl.NumberFormat()

function MetricsPage() {
  const [filters, setFilters] = React.useState<MetricFilters>({ days: 30 })
  const [traceId, setTraceId] = React.useState<string>()
  const facets = useQuery({
    queryKey: ["metrics", "facets", filters.days],
    queryFn: () => getMetrics({ days: filters.days }),
    staleTime: 30_000,
  })
  const report = useQuery({
    queryKey: ["metrics", filters],
    queryFn: () => getMetrics(filters),
    refetchInterval: 15_000,
  })
  const traces = useQuery({
    queryKey: ["metrics", "traces", filters, traceId],
    queryFn: () => getMetricTraces({ ...filters, trace_id: traceId }),
    refetchInterval: 15_000,
  })
  const deletions = useQuery({
    queryKey: ["metrics", "deletions"],
    queryFn: getExternalDeletions,
    refetchInterval: 30_000,
  })
  const traceIds = [
    ...new Set(
      traces.data?.items
        .map((event) =>
          typeof event.dimensions.trace_id === "string"
            ? event.dimensions.trace_id
            : ""
        )
        .filter(Boolean)
    ),
  ]
  const activeFilters = (Object.keys(FILTER_LABELS) as FilterKey[]).filter(
    (key) => filters[key]
  )
  const items = report.data?.items ?? []
  const samples = items.reduce((sum, series) => sum + series.count, 0)
  const missing = items.reduce((sum, series) => sum + series.unknown_count, 0)

  return (
    <PageShell>
      <PageContainer variant="wide" className="flex flex-col gap-8">
        <PageHeader
          title="Metrics"
          description="Quality, timing and consumption by configuration. Technical metadata only — no resumes or answers."
        />

        {/* Filters as compact chips */}
        <div
          role="group"
          aria-label="Filters"
          className="flex flex-wrap items-center gap-2"
        >
          <Select
            value={String(filters.days)}
            onValueChange={(next) =>
              next && setFilters({ ...filters, days: Number(next) })
            }
          >
            <SelectTrigger size="sm" aria-label="Period" className="gap-2">
              <CalendarIcon className="size-4" />
              <SelectValue>
                {(value: string) => `Last ${value} days`}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {PERIODS.map((days) => (
                <SelectItem key={days} value={String(days)}>
                  Last {days} days
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {(Object.entries(FILTER_LABELS) as [FilterKey, string][]).map(
            ([key, label]) => {
              const value = filters[key]
              const options = facets.data?.facets[key] ?? []
              return (
                <Select
                  key={key}
                  value={value ?? ALL}
                  onValueChange={(next) => {
                    setFilters({
                      ...filters,
                      [key]: !next || next === ALL ? undefined : String(next),
                    })
                    setTraceId(undefined)
                  }}
                >
                  <SelectTrigger
                    size="sm"
                    aria-label={label}
                    className={cn(
                      "max-w-64 gap-2",
                      value &&
                        "border-transparent bg-primary-container text-on-primary-container"
                    )}
                  >
                    <SelectValue>
                      {(current: string) =>
                        current === ALL ? label : `${label}: ${current}`
                      }
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>All</SelectItem>
                    {options.map((option) => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )
            }
          )}
          {activeFilters.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setFilters({ days: filters.days })
                setTraceId(undefined)
              }}
            >
              <XIcon />
              Clear filters
            </Button>
          )}
        </div>

        {report.isError && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>
              Could not load metrics. {report.error.message}
            </AlertDescription>
          </Alert>
        )}

        {/* Compact totals — sums of what the report returned, nothing more. */}
        <dl className="grid grid-cols-2 gap-3 @2xl/main:grid-cols-4">
          <Stat label="Series" value={report.data ? items.length : undefined} />
          <Stat label="Samples" value={report.data ? samples : undefined} />
          <Stat
            label="Missing measurements"
            value={report.data ? missing : undefined}
          />
          <Stat
            label="Pending trace removals"
            value={
              deletions.data
                ? (deletions.data.counts.pending ?? 0) +
                  (deletions.data.counts.verifying ?? 0)
                : undefined
            }
          />
        </dl>

        <Section
          title="Measurements"
          description="Percentiles use histogram buckets with a 5% relative width. Server timings and browser playback measurements are reported as separate series; a server chunk does not prove audible output."
        >
          {report.isPending ? (
            <TableSkeleton />
          ) : items.length === 0 ? (
            <EmptyState
              illustration={<Mascot state="waiting" className="w-28" />}
              title="No measurements yet"
              description="No measurements match this configuration yet. Run a controlled interview to establish its baseline."
              className="rounded-xl border py-10"
            />
          ) : (
            <div className="max-h-[36rem] overflow-auto rounded-xl border">
              <Table>
                <TableHeader className="sticky top-0 z-10 bg-card">
                  <TableRow>
                    {[
                      "Component / metric",
                      "Configuration",
                      "Samples",
                      "Missing",
                      "Total",
                      "Mean",
                      "p50",
                      "p95",
                    ].map((label, i) => (
                      <TableHead
                        key={label}
                        className={cn(i > 1 && "text-right")}
                      >
                        {label}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((series) => (
                    <TableRow key={series.series_id}>
                      <TableCell className="align-top">
                        <span className="block text-xs text-muted-foreground">
                          {series.component}
                        </span>
                        <span className="font-medium">{series.name}</span>
                      </TableCell>
                      <TableCell className="max-w-72 align-top text-xs [overflow-wrap:anywhere] whitespace-normal text-muted-foreground">
                        {Object.entries(series.dimensions)
                          .map(([key, value]) => `${key}: ${String(value)}`)
                          .join(" · ")}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {integer.format(series.count)}
                      </TableCell>
                      <TableCell className="text-right text-muted-foreground tabular-nums">
                        {integer.format(series.unknown_count)}
                      </TableCell>
                      {[
                        series.count ? series.total : null,
                        series.mean,
                        series.p50,
                        series.p95,
                      ].map((value, i) => (
                        <TableCell
                          key={i}
                          className={cn(
                            "text-right tabular-nums",
                            value === null && "text-muted-foreground"
                          )}
                        >
                          {formatValue(value, series.name)}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </Section>

        <div className="grid gap-8 @5xl/main:grid-cols-2">
          <Section
            title="Recent technical traces"
            description="Detail is kept for 30 days; anonymous aggregates remain after it expires."
          >
            {traceIds.length > 0 || traceId ? (
              <div className="flex flex-wrap gap-2">
                <ListFilterIcon
                  aria-hidden
                  className="size-4 self-center text-muted-foreground"
                />
                {traceId ? (
                  <Button
                    size="xs"
                    variant="tonal"
                    onClick={() => setTraceId(undefined)}
                  >
                    Trace {traceId.slice(0, 8)}
                    <XIcon />
                  </Button>
                ) : (
                  traceIds.map((id) => (
                    <Button
                      key={id}
                      size="xs"
                      variant="outline"
                      onClick={() => setTraceId(id)}
                    >
                      Trace {id.slice(0, 8)}
                    </Button>
                  ))
                )}
              </div>
            ) : null}
            {traces.isError ? (
              <p role="alert" className="text-sm text-destructive">
                Could not load traces.
              </p>
            ) : traces.isPending ? (
              <TableSkeleton rows={4} />
            ) : !traces.data.items.length ? (
              <p className="rounded-xl border px-4 py-8 text-center text-sm text-muted-foreground">
                No recent detail available. Anonymous aggregates remain after
                detail expires.
              </p>
            ) : (
              <ul className="max-h-96 divide-y overflow-auto rounded-xl border text-xs">
                {traces.data.items.map((event) => (
                  <li
                    key={event.id}
                    className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-4 py-2.5"
                  >
                    <time className="text-muted-foreground tabular-nums">
                      {new Date(event.created_at).toLocaleString()}
                    </time>
                    <span className="min-w-0 flex-1 truncate">
                      {event.component} / {event.name}
                    </span>
                    <span className="font-medium tabular-nums">
                      {formatValue(event.value, event.name)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {traces.data?.has_more && (
              <p className="text-xs text-muted-foreground">
                Showing the most recent events; this view is not the entire
                trace.
              </p>
            )}
          </Section>

          <Section
            title="External trace removal"
            description="Local detail is kept for 30 days. External removal remains pending until a separate check confirms the trace has disappeared. Anonymous aggregates are kept for 365 days."
          >
            {deletions.isError ? (
              <p role="alert" className="text-sm text-destructive">
                Could not check external removal status.
              </p>
            ) : deletions.isPending ? (
              <TableSkeleton rows={3} />
            ) : (
              <>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="secondary">
                    Pending{" "}
                    {(deletions.data.counts.pending ?? 0) +
                      (deletions.data.counts.verifying ?? 0)}
                  </Badge>
                  <Badge
                    variant={
                      deletions.data.counts.failed ? "destructive" : "secondary"
                    }
                  >
                    Failed {deletions.data.counts.failed ?? 0}
                  </Badge>
                  <Badge variant="success">
                    Verified {deletions.data.counts.completed ?? 0}
                  </Badge>
                </div>
                {deletions.data.items.length > 0 && (
                  <ul className="max-h-64 divide-y overflow-auto rounded-xl border text-xs">
                    {deletions.data.items.map((job) => (
                      <li key={job.id} className="px-4 py-2.5">
                        <span className="font-medium">
                          Removal {job.id.slice(0, 8)}
                        </span>{" "}
                        ·{" "}
                        {job.state === "verifying"
                          ? "Waiting for verification"
                          : job.state}
                        {job.last_error && (
                          <span className="text-destructive">
                            {" "}
                            ·{" "}
                            {job.last_error === "missing_credentials"
                              ? "LangSmith credentials required"
                              : job.last_error}
                          </span>
                        )}
                        {job.verified_at && (
                          <span className="text-muted-foreground">
                            {" "}
                            · Verified{" "}
                            {new Date(job.verified_at).toLocaleString()}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </Section>
        </div>
      </PageContainer>
    </PageShell>
  )
}

function Stat({ label, value }: { label: string; value: number | undefined }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border px-4 py-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-heading text-2xl leading-8 tabular-nums">
        {value === undefined ? (
          <Skeleton className="mt-1 h-6 w-12" />
        ) : (
          integer.format(value)
        )}
      </dd>
    </div>
  )
}

function TableSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="flex flex-col divide-y overflow-hidden rounded-xl border">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-6 px-4 py-3.5">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  )
}
