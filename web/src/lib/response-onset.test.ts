import { describe, expect, it } from "vitest"
import { ResponseOnsetDetector } from "./response-onset"

function run(frames: Array<[number, number, number]>) {
  const detector = new ResponseOnsetDetector()
  return frames
    .map(([t, mic, agent]) => detector.feed(t, mic, agent))
    .filter((value) => value !== null)
}

function series(from: number, to: number, mic: number, agent: number) {
  const frames: Array<[number, number, number]> = []
  for (let t = from; t < to; t += 50) frames.push([t, mic, agent])
  return frames
}

describe("ResponseOnsetDetector", () => {
  it("measures from the end of candidate speech to sustained interviewer audio", () => {
    const samples = run([
      ...series(0, 1000, 0.1, 0),
      ...series(1000, 3000, 0, 0),
      ...series(3000, 3400, 0, 0.2),
    ])
    expect(samples).toEqual([2])
  })

  it("ignores pauses inside an answer and short noises", () => {
    const samples = run([
      ...series(0, 100, 0.1, 0), // too short to be speech
      ...series(100, 900, 0, 0),
      ...series(900, 1500, 0.1, 0),
      ...series(1500, 1800, 0, 0), // pause shorter than the end silence
      ...series(1800, 2400, 0.1, 0),
      ...series(2400, 3400, 0, 0),
      ...series(3400, 3420, 0, 0.2), // a click, not an onset
      ...series(3420, 4000, 0, 0),
      ...series(4000, 4300, 0, 0.2),
    ])
    expect(samples).toEqual([1.6])
  })

  it("does not count audio that was already playing when speech ended", () => {
    const samples = run([
      ...series(0, 1000, 0.1, 0.2),
      ...series(1000, 2500, 0, 0.2),
    ])
    expect(samples).toEqual([])
  })
})
