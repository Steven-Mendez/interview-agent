import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { RpcError } from "livekit-client"
import { requestInterviewEnd } from "./closing-request"

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

it("allows a normal reply beyond five milliseconds and uses SDK milliseconds", async () => {
  const participant = {
    performRpc: vi.fn(
      (params) =>
        new Promise<string>((resolve, reject) => {
          setTimeout(
            () => reject(new Error("SDK timeout")),
            params.responseTimeout
          )
          setTimeout(() => resolve('{"accepted":true}'), 50)
        })
    ),
  }
  const result = requestInterviewEnd(participant, "worker")
  await vi.advanceTimersByTimeAsync(50)
  await expect(result).resolves.toBe('{"accepted":true}')
  expect(participant.performRpc).toHaveBeenCalledWith(
    expect.objectContaining({
      destinationIdentity: "worker",
      responseTimeout: 5_000,
    })
  )
})

it("bounds the whole call when SDK publication itself remains pending", async () => {
  const participant = { performRpc: vi.fn(() => new Promise<string>(() => {})) }
  let settled = false
  const result = requestInterviewEnd(participant, "worker").catch(
    (error: Error) => {
      settled = true
      return error.message
    }
  )
  await vi.advanceTimersByTimeAsync(4_999)
  expect(settled).toBe(false)
  await vi.advanceTimersByTimeAsync(1)
  expect(await result).toContain("pending")
  expect(participant.performRpc).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(0)
})

it("preserves a definitive SDK error and cancels its local timer", async () => {
  const error = new RpcError(
    RpcError.ErrorCode.UNSUPPORTED_METHOD,
    "Unsupported method"
  )
  const participant = { performRpc: vi.fn().mockRejectedValue(error) }
  await expect(requestInterviewEnd(participant, "worker")).rejects.toBe(error)
  expect(vi.getTimerCount()).toBe(0)
})

it("does not re-send the request or change an uncertain timeout after a late SDK reply", async () => {
  let finish!: (value: string) => void
  const participant = {
    performRpc: vi.fn(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve
        })
    ),
  }
  const result = requestInterviewEnd(participant, "worker").catch(
    (error: Error) => error.message
  )
  await vi.advanceTimersByTimeAsync(5_000)
  const outcome = await result
  finish('{"accepted":true}')
  await vi.advanceTimersByTimeAsync(1)
  expect(await result).toBe(outcome)
  expect(participant.performRpc).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(0)
})
