import type { LocalParticipant } from "livekit-client"

const CLOSE_RPC_TIMEOUT_MS = 5_000

/** Bound publication and response together; timeout leaves the outcome uncertain. */
export async function requestInterviewEnd(
  participant: Pick<LocalParticipant, "performRpc">,
  destinationIdentity: string
): Promise<string> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(
      () =>
        reject(
          new Error(
            "Closing confirmation is pending. Checking the saved interview."
          )
        ),
      CLOSE_RPC_TIMEOUT_MS
    )
  })
  try {
    return await Promise.race([
      deadline,
      Promise.resolve().then(() =>
        participant.performRpc({
          destinationIdentity,
          method: "interview.request_end",
          payload: "{}",
          // Unlike the Python SDK, the browser SDK takes milliseconds.
          responseTimeout: CLOSE_RPC_TIMEOUT_MS,
        })
      ),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}
