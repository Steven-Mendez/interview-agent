/** @vitest-environment jsdom */
import { act, fireEvent, render } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { RoomContext } from "@livekit/components-react"
import { Room, RoomEvent } from "livekit-client"

import { InterviewAudioRecovery } from "./interview-audio-recovery"

describe("interview audio recovery", () => {
  it("is conditional on playback permission and retries from a user click", async () => {
    const room = new Room()
    let allowed = true
    vi.spyOn(room, "canPlaybackAudio", "get").mockImplementation(() => allowed)
    const startAudio = vi
      .spyOn(room, "startAudio")
      .mockImplementation(async () => {
        allowed = true
        room.emit(RoomEvent.AudioPlaybackStatusChanged, true)
      })
    const view = render(
      <RoomContext.Provider value={room}>
        <InterviewAudioRecovery />
      </RoomContext.Provider>
    )
    const button = view.getByText("Enable audio")
    expect(button.style.display).toBe("none")
    act(() => {
      allowed = false
      room.emit(RoomEvent.AudioPlaybackStatusChanged, false)
    })
    expect(button.style.display).toBe("block")
    await act(async () => {
      fireEvent.click(button)
    })
    expect(startAudio).toHaveBeenCalledOnce()
    expect(button.style.display).toBe("none")
    view.unmount()
    vi.restoreAllMocks()
  })
})
