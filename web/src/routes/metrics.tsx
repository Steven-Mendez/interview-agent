import * as React from "react"
import { createFileRoute } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"
import { getMetrics, getMetricTraces, getExternalDeletions } from "@/lib/api"
import type { MetricFilters } from "@/lib/api"
import { PageContainer, PageShell } from "@/components/ui/page"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"

export const Route = createFileRoute("/metrics")({ component: MetricsPage })
const FILTER_LABELS = {
  graph_version: "Graph version",
  model: "Model",
  language: "Language",
  seniority: "Level",
  length: "Duration profile",
} as const

function formatValue(value: number | null, name: string) {
  if (value === null) return "Unavailable"
  if (name.endsWith("seconds")) return `${value.toFixed(3)} s`
  if (name.endsWith("usd")) return `$${value.toFixed(5)}`
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(
    value
  )
}

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
  return (
    <PageShell>
      <PageContainer variant="wide">
        <div className="flex flex-col gap-6">
          <div>
            <h1 className="text-xl font-semibold">Interview metrics</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Compare quality, timing and consumption by configuration. Missing
              measurements stay unavailable. These traces contain technical
              metadata, without resumes or candidate answers.
            </p>
          </div>
          <Card>
            <CardContent className="flex flex-wrap gap-4">
              <label className="flex flex-col gap-1 text-xs">
                Period
                <select
                  className="rounded-md border bg-background p-2 text-sm"
                  value={filters.days}
                  onChange={(event) =>
                    setFilters({ ...filters, days: Number(event.target.value) })
                  }
                >
                  {[7, 30, 90, 365].map((days) => (
                    <option key={days} value={days}>
                      Last {days} days
                    </option>
                  ))}
                </select>
              </label>
              {Object.entries(FILTER_LABELS).map(([key, label]) => (
                <label key={key} className="flex flex-col gap-1 text-xs">
                  {label}
                  <select
                    className="rounded-md border bg-background p-2 text-sm"
                    value={filters[key as keyof typeof FILTER_LABELS] ?? ""}
                    onChange={(event) => {
                      setFilters({
                        ...filters,
                        [key]: event.target.value || undefined,
                      })
                      setTraceId(undefined)
                    }}
                  >
                    <option value="">All</option>
                    {(facets.data?.facets[key] ?? []).map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </CardContent>
          </Card>
          {report.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                Could not load metrics. {report.error.message}
              </AlertDescription>
            </Alert>
          )}
          <Card>
            <CardHeader>
              <CardTitle>Measurements</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="mb-3 text-xs text-muted-foreground">
                Percentiles use histogram buckets with a 5% relative width.
                Server timings and browser playback measurements are reported as
                separate series; a server chunk does not prove audible output.
              </p>
              {report.isPending ? (
                <p>Loading…</p>
              ) : !report.data?.items.length ? (
                <p className="text-sm text-muted-foreground">
                  No measurements match this configuration yet. Run a controlled
                  interview to establish its baseline.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr>
                        {[
                          "Component / metric",
                          "Configuration",
                          "Samples",
                          "Missing",
                          "Total",
                          "Mean",
                          "p50",
                          "p95",
                        ].map((label) => (
                          <th key={label} className="border-b p-2 font-medium">
                            {label}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {report.data.items.map((series) => (
                        <tr key={series.series_id}>
                          <td className="border-b p-2">
                            {series.component}
                            <br />
                            {series.name}
                          </td>
                          <td className="max-w-72 border-b p-2 break-words">
                            {Object.entries(series.dimensions)
                              .map(([key, value]) => `${key}: ${String(value)}`)
                              .join(" · ")}
                          </td>
                          <td className="border-b p-2">{series.count}</td>
                          <td className="border-b p-2">
                            {series.unknown_count}
                          </td>
                          {[
                            series.count ? series.total : null,
                            series.mean,
                            series.p50,
                            series.p95,
                          ].map((value, i) => (
                            <td key={i} className="border-b p-2 tabular-nums">
                              {formatValue(value, series.name)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Recent technical traces</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="flex flex-wrap gap-2">
                {traceId && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setTraceId(undefined)}
                  >
                    All recent traces
                  </Button>
                )}
                {!traceId &&
                  traceIds.map((id) => (
                    <Button
                      key={id}
                      size="sm"
                      variant="outline"
                      onClick={() => setTraceId(id)}
                    >
                      Trace {id.slice(0, 8)}
                    </Button>
                  ))}
              </div>
              {traces.isError ? (
                <p>Could not load traces.</p>
              ) : !traces.data?.items.length ? (
                <p className="text-sm text-muted-foreground">
                  No recent detail available. Anonymous aggregates remain after
                  detail expires.
                </p>
              ) : (
                <ul className="max-h-96 overflow-auto text-xs">
                  {traces.data.items.map((event) => (
                    <li key={event.id} className="border-b py-2">
                      <time>{new Date(event.created_at).toLocaleString()}</time>{" "}
                      · {event.component} / {event.name}:{" "}
                      {formatValue(event.value, event.name)}
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
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>External trace removal</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="mb-3 text-xs text-muted-foreground">
                Local detail is kept for 30 days. External removal remains
                pending until a separate check confirms the trace has
                disappeared. Anonymous aggregates are kept for 365 days.
              </p>
              {deletions.isError ? (
                <p>Could not check external removal status.</p>
              ) : deletions.isPending ? (
                <p>Loading…</p>
              ) : (
                <>
                  <p className="text-sm">
                    Pending:{" "}
                    {(deletions.data.counts.pending ?? 0) +
                      (deletions.data.counts.verifying ?? 0)}{" "}
                    · Failed: {deletions.data.counts.failed ?? 0} · Verified:{" "}
                    {deletions.data.counts.completed ?? 0}
                  </p>
                  <ul className="mt-3 max-h-64 overflow-auto text-xs">
                    {deletions.data.items.map((job) => (
                      <li key={job.id} className="border-b py-2">
                        Removal {job.id.slice(0, 8)} ·{" "}
                        {job.state === "verifying"
                          ? "Waiting for verification"
                          : job.state}
                        {job.last_error && (
                          <span>
                            {" "}
                            ·{" "}
                            {job.last_error === "missing_credentials"
                              ? "LangSmith credentials required"
                              : job.last_error}
                          </span>
                        )}
                        {job.verified_at && (
                          <span>
                            {" "}
                            · Verified{" "}
                            {new Date(job.verified_at).toLocaleString()}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </CardContent>
          </Card>
        </div>
      </PageContainer>
    </PageShell>
  )
}
