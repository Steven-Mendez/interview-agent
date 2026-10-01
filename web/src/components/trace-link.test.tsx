/** @vitest-environment jsdom */
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"
import { TraceLink } from "./trace-link"

afterEach(cleanup)

it("links to the trace only when the API provides one", () => {
  const { container } = render(<TraceLink url={null} />)
  expect(container.textContent).toBe("")
  render(<TraceLink url="https://smith.langchain.com/o/t/projects/p/p/r/x" />)
  const link = screen.getByRole("link", { name: /View trace in LangSmith/ })
  expect(link.getAttribute("href")).toBe(
    "https://smith.langchain.com/o/t/projects/p/p/r/x"
  )
  expect(link.getAttribute("rel")).toBe("noreferrer")
})

it("links each voice session next to the interview trace", () => {
  render(
    <TraceLink
      url="https://smith.langchain.com/r/interview"
      voiceUrls={[
        "https://smith.langchain.com/r/voice-1",
        "https://smith.langchain.com/r/voice-2",
      ]}
    />
  )
  expect(
    screen.getByRole("link", { name: /Voice session 2/ }).getAttribute("href")
  ).toBe("https://smith.langchain.com/r/voice-2")
  expect(screen.getAllByRole("link")).toHaveLength(3)
})
