/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import type { Interview } from "@/lib/api"
import { RepeatOptions, repeatBody } from "./repeat-options"

afterEach(cleanup)

it("sends only explicit changes so unchanged settings are inherited", () => {
  const source = { interview_length: "standard" } as const
  expect(repeatBody(source, "standard", "", "")).toEqual({})
  expect(repeatBody(source, "deep", "10", "0")).toEqual({
    interview_length: "deep",
    question_limit: 10,
    followup_limit: 0,
  })
})

it("rejects values outside the typed contract before calling the API", () => {
  const onRepeat = vi.fn()
  render(
    <RepeatOptions
      interview={{ interview_length: "short" } as Interview}
      pending={false}
      onRepeat={onRepeat}
    />
  )
  fireEvent.change(screen.getByLabelText(/Main questions/), {
    target: { value: "13" },
  })
  const submit = screen.getByRole("button", {
    name: "Repeat with these settings",
    hidden: true,
  })
  expect(submit).toHaveProperty("disabled", true)
  fireEvent.change(screen.getByLabelText(/Main questions/), {
    target: { value: "12" },
  })
  fireEvent.click(submit)
  expect(onRepeat).toHaveBeenCalledWith({ question_limit: 12 })
})
