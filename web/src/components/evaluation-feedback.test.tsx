/** @vitest-environment jsdom */
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"
import type { Evaluation } from "@/lib/api"
import { CriterionFeedback, EvaluationOutcome } from "./evaluation-feedback"

afterEach(cleanup)

const evaluation: Evaluation = {
  score: null,
  hired: null,
  evaluation_status: "partial",
  coverage: 0.5,
  strengths: [],
  weaknesses: [],
  rationale: "Some criteria were not asked.",
  seniority_evaluated: "junior",
  calibration_notes: [],
  ended_by: "timeout",
}

it("shows useful partial feedback without inventing a verdict or zero score", () => {
  render(
    <EvaluationOutcome
      evaluation={evaluation}
      level="Junior"
      duration="Up to 8 min"
    />
  )
  expect(screen.getByText("Partial assessment")).toBeTruthy()
  expect(screen.queryByText("Not hired")).toBeNull()
  expect(screen.queryByText("/100")).toBeNull()
  expect(screen.getByText(/Criteria observed: 50%/)).toBeTruthy()
})

it("explains insufficient evidence and shows candidate quotes with actionable practice", () => {
  const ev: Evaluation = {
    ...evaluation,
    evaluation_status: "insufficient",
    criteria: [
      {
        milestone_id: "m1",
        assessment: "below",
        rationale: "The response shows a specific gap.",
        evidence: [
          { message_id: "42", quote: "I do not know how the index works." },
        ],
        practice: "Create an index and compare EXPLAIN before and after.",
      },
    ],
  }
  render(
    <>
      <EvaluationOutcome
        evaluation={ev}
        level="Junior"
        duration="Up to 8 min"
      />
      <CriterionFeedback evaluation={ev} milestones={[]} />
    </>
  )
  expect(screen.getByText("Insufficient evidence")).toBeTruthy()
  expect(screen.getByText(/I do not know how the index works/)).toBeTruthy()
  expect(screen.getByText("Candidate answer #42")).toBeTruthy()
  expect(screen.getByText(/Create an index and compare/)).toBeTruthy()
})

it("hides a formerly complete global result while capture integrity needs review", () => {
  render(
    <EvaluationOutcome
      evaluation={{
        ...evaluation,
        evaluation_status: "complete",
        score: 82,
        hired: true,
      }}
      level="Junior"
      duration="Up to 8 min"
      captureIntegrityPending
    />
  )
  expect(screen.queryByText("82")).toBeNull()
  expect(screen.queryByText(/Hired ·/)).toBeNull()
  expect(screen.getByRole("alert").textContent).toContain(
    "pending review or a confirmed omission"
  )
})

it("labels a criterion answered above its bar", () => {
  const ev: Evaluation = {
    ...evaluation,
    evaluation_status: "complete",
    score: 93,
    hired: true,
    criteria: [
      {
        milestone_id: "m1",
        assessment: "exceeds",
        rationale: "Adds a composite index the bar did not ask for.",
        evidence: [{ message_id: "7", quote: "plus a composite index" }],
        practice: "",
      },
    ],
  }
  render(<CriterionFeedback evaluation={ev} milestones={[]} />)
  expect(screen.getByText("Exceeds the criterion")).toBeTruthy()
})

it("explains the points a complete score is missing", () => {
  render(
    <EvaluationOutcome
      evaluation={{
        ...evaluation,
        evaluation_status: "complete",
        score: 98,
        hired: true,
        score_gap: "The Docker answer stopped at a standard healthcheck.",
      }}
      level="Junior"
      duration="Up to 15 min"
    />
  )
  expect(screen.getByText(/Why not higher: The Docker answer/)).toBeTruthy()
})
