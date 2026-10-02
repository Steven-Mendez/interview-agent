import { createFileRoute } from "@tanstack/react-router"
import * as React from "react"

import { MASCOT_STATE_LABELS, Mascot, type MascotState } from "@/components/mascot"

export const Route = createFileRoute("/mascot-lab")({ component: Lab })

function Lab() {
  const states = Object.keys(MASCOT_STATE_LABELS) as MascotState[]
  const [cycle, setCycle] = React.useState(0)
  const [level, setLevel] = React.useState(0)
  React.useEffect(() => {
    const t = setInterval(() => setCycle((c) => c + 1), 2500)
    const v = setInterval(() => setLevel(Math.random() * 0.3), 120)
    return () => (clearInterval(t), clearInterval(v))
  }, [])
  return (
    <div className="grid grid-cols-5 gap-6 p-8">
      {states.map((s) => (
        <div key={s} className="flex flex-col items-center">
          <Mascot state={s} className="w-40" />
          <span>{s}</span>
        </div>
      ))}
      <div className="flex flex-col items-center">
        <Mascot state={states[cycle % 4 === 0 ? 2 : cycle % 4 === 1 ? 3 : cycle % 4 === 2 ? 4 : 0]} className="w-40" />
        <span>cycling</span>
      </div>
      <div className="flex flex-col items-center">
        <Mascot state="speaking" level={level} className="w-40" />
        <span>metered</span>
      </div>
    </div>
  )
}
