import { StartAudio } from "@livekit/components-react"

import { buttonVariants } from "@/components/ui/button"

/** LiveKit hides this control unless the browser blocks audio playback. */
export function InterviewAudioRecovery() {
  return (
    <StartAudio
      label="Enable audio"
      className={buttonVariants({ variant: "outline", size: "sm" })}
    />
  )
}
