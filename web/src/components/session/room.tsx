import * as React from "react"

import type { MascotState } from "@/components/mascot"

/** The live room keeps its menus, tooltips and dialogs dark: they portal to
 *  <body>, so the dark tokens go there while the room is open. */
export function useDarkRoom() {
  React.useLayoutEffect(() => {
    document.body.classList.add("session-dark")
    return () => document.body.classList.remove("session-dark")
  }, [])
}

/** The character's state for LiveKit's `lk.agent.state`. Only what the
 *  agent actually reports — nothing is inferred. */
export function agentMascotState(
  state: string | null | undefined
): MascotState {
  switch (state) {
    case "listening":
      return "listening"
    case "thinking":
      return "thinking"
    case "speaking":
      return "speaking"
    default:
      return "waiting"
  }
}
