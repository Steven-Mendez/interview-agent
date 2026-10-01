import type { Interview, Milestone } from "@/lib/api"

export function topicState(milestone: Milestone) {
  return milestone.lifecycle ?? (milestone.completed ? "closed" : "pending")
}

export function isSettled(milestone: Milestone) {
  return ["closed", "skipped"].includes(topicState(milestone))
}

const TOPIC_LABELS = {
  pending: "Pending",
  active: "In progress",
  closed: "Closed",
  skipped: "Skipped",
}

export function TopicStatus({ milestone }: { milestone: Milestone }) {
  return (
    <span className="text-xs text-muted-foreground">
      {TOPIC_LABELS[topicState(milestone)]}
    </span>
  )
}

function requested(
  interview: Interview,
  key: string,
  effective: number | null
) {
  const value = interview.run_config?.[key]
  return typeof value === "number" && effective !== null && value > effective
    ? value
    : null
}

export function InterviewLimits({ interview }: { interview: Interview }) {
  const limits = [
    [
      "Main questions",
      interview.question_limit,
      requested(
        interview,
        "requested_question_limit",
        interview.question_limit
      ),
      "one per planned topic",
    ],
    [
      "Follow-ups per topic",
      interview.followup_limit,
      requested(
        interview,
        "requested_followup_limit",
        interview.followup_limit
      ),
      "capped by level and length",
    ],
    [
      "Time limit",
      interview.max_minutes === null ? null : `${interview.max_minutes} min`,
      null,
      "",
    ],
  ] as const
  return (
    <div className="rounded-xl border bg-card p-4">
      <h2 className="mb-3 text-sm font-medium">Interview limits</h2>
      <dl className="grid grid-cols-3 gap-3 text-sm">
        {limits.map(([label, value, asked, why]) => (
          <div key={label}>
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="mt-1 font-medium tabular-nums">
              {value ?? "Not recorded"}
            </dd>
            {asked !== null && (
              <dd className="mt-1 text-xs text-muted-foreground">
                Requested {asked}; {why}.
              </dd>
            )}
          </div>
        ))}
      </dl>
      {interview.seniority_source === "detected" &&
        interview.seniority_evidence && (
          <p className="mt-3 text-xs text-muted-foreground">
            Level detected from the offer: “{interview.seniority_evidence}”
          </p>
        )}
    </div>
  )
}
