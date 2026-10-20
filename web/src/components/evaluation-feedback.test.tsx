/** @vitest-environment jsdom */
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"
import type { Evaluation } from "@/lib/api"
import {
  AnswerComparisons,
  CriterionFeedback,
  EvaluationOutcome,
} from "./evaluation-feedback"

afterEach(cleanup)

it.each(["practice", "limited"] as const)(
  "shows %s scope without implying rejection",
  (scope) => {
    render(
      <EvaluationOutcome
        evaluation={{
          ...evaluation,
          assessment_scope: scope,
          evaluation_status: scope === "practice" ? "complete" : "partial",
          coverage: 1,
          uncovered_requirements: ["Accessibility"],
        }}
      />
    )
    expect(
      screen.getByText(
        scope === "practice" ? "Practice feedback" : "Limited role coverage"
      )
    ).toBeTruthy()
    expect(screen.getByText("Accessibility")).toBeTruthy()
    expect(
      screen.getByText(/Missing coverage is not a demonstrated weakness/)
    ).toBeTruthy()
    expect(screen.queryByText("Not hired")).toBeNull()
    expect(screen.queryByText("Hired")).toBeNull()
    expect(screen.queryByText("Partial assessment")).toBeNull()
  }
)

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
  render(<EvaluationOutcome evaluation={evaluation} />)
  expect(screen.getByText("Partial assessment")).toBeTruthy()
  expect(screen.queryByText("Not hired")).toBeNull()
  expect(screen.queryByText("/100")).toBeNull()
  expect(screen.getByText(/Selected criteria observed: 50%/)).toBeTruthy()
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
      <EvaluationOutcome evaluation={ev} />
      <CriterionFeedback evaluation={ev} milestones={[]} />
    </>
  )
  expect(screen.getByText("Insufficient evidence")).toBeTruthy()
  expect(screen.getByText(/I do not know how the index works/)).toBeTruthy()
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

it("summarizes a complete result once without repeating scoring details", () => {
  render(
    <EvaluationOutcome
      evaluation={{
        ...evaluation,
        evaluation_status: "complete",
        score: 98,
        hired: true,
        coverage: 1,
        rationale: "The answers meet the role's criteria.",
        score_gap: "The Docker answer stopped at a standard healthcheck.",
      }}
    />
  )
  expect(screen.getByText("The answers meet the role's criteria.")).toBeTruthy()
  expect(screen.queryByText(/The Docker answer/)).toBeNull()
  expect(screen.queryByText(/Selected criteria observed/)).toBeNull()
})

it("shows a grounded before/after comparison and the real details still needed", () => {
  render(
    <AnswerComparisons
      evaluation={{
        ...evaluation,
        answer_revisions: [
          {
            milestone_id: "debug",
            question_id: "41",
            question: "What bug did you fix?",
            original: {
              message_id: "42",
              message_version: 1,
              quote: "I changed the code.",
            },
            suggested_answer:
              "I fixed [your actual bug] by [the change you made].",
            explanation:
              "Specify the cause and the fix instead of only the activity.",
            basis: "clarification",
            missing_details: ["Identify the actual bug and your change."],
          },
        ],
      }}
    />
  )
  expect(
    screen.getByRole("region", { name: "Improve your answers" })
  ).toBeTruthy()
  expect(screen.getByText("What bug did you fix?")).toBeTruthy()
  expect(screen.getByText("You said")).toBeTruthy()
  expect(screen.getByText("You could say")).toBeTruthy()
  expect(screen.getByText("I changed the code.")).toBeTruthy()
  expect(screen.getByText(/I fixed \[your actual bug\]/)).toBeTruthy()
  expect(
    screen.getByText("Identify the actual bug and your change.")
  ).toBeTruthy()
})

it("labels missing knowledge as something to learn before using the example", () => {
  render(
    <AnswerComparisons
      evaluation={{
        ...evaluation,
        answer_revisions: [
          {
            milestone_id: "sql",
            question_id: "1",
            question: "How does EXPLAIN help?",
            original: { message_id: "2", quote: "EXPLAIN creates indexes." },
            suggested_answer:
              "EXPLAIN shows the execution plan; it does not create indexes.",
            explanation: "Learn to read a plan before applying this approach.",
            basis: "knowledge_example",
            missing_details: [],
          },
        ],
      }}
    />
  )
  expect(screen.getByText("After practising, you could explain")).toBeTruthy()
  expect(screen.queryByText("Add your real details")).toBeNull()
})

it("does not invent corrections for old evaluations or sufficient answers", () => {
  const { rerender } = render(<AnswerComparisons evaluation={evaluation} />)
  expect(screen.queryByRole("region")).toBeNull()
  rerender(
    <AnswerComparisons evaluation={{ ...evaluation, answer_revisions: [] }} />
  )
  expect(screen.queryByRole("region")).toBeNull()
})
