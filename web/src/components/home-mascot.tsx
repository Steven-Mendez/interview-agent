import * as React from "react"

import { Mascot } from "@/components/mascot"
import type { MascotState } from "@/components/mascot"
import { cn } from "@/lib/utils"

// The character on the home page has a life of its own: it says hello,
// then wanders between things to do — idling, following the pointer with
// its eyes, dancing, recharging, waving again — never the same thing twice
// in a row. Each step lasts whole cycles of its loop, so a
// state never cuts off mid-move.
//
// Easter egg: clicking it tickles it. Keep at it and it gets angry for a
// fixed while, then sulks; it ignores clicks until it has calmed down.

type Step = { state: MascotState; ms: number }
type Act = { steps: Step[]; weight: number; needsPointer?: boolean }

const GREETING: Step = { state: "greeting", ms: 5800 }

// Only things a character does on its own time: the interview gestures
// (thinking, success, celebrating) belong to the interview.
const ACTS: Act[] = [
  { steps: [{ state: "idle", ms: 8700 }], weight: 3 },
  { steps: [{ state: "watching", ms: 9000 }], weight: 3, needsPointer: true },
  { steps: [{ state: "dancing", ms: 7200 }], weight: 2 },
  { steps: [{ state: "charging", ms: 6400 }], weight: 1.5 },
  { steps: [GREETING], weight: 0.8 },
]

const TICKLE: Step = { state: "tickled", ms: 1800 }
const ANGRY: Step = { state: "angry", ms: 4200 }
/** After a tantrum it sulks in place before going back to its routine. */
const SULK: Step = { state: "idle", ms: 4300 }
/** Clicks closer together than this add up to the same tickling. */
const TICKLE_WINDOW_MS = 2500
const TICKLES_BEFORE_ANGRY = 4

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
}

function pickAct(last: Act | null, pointerSeen: boolean): Act {
  const options = ACTS.filter(
    (act) => act !== last && (pointerSeen || !act.needsPointer)
  )
  let roll = Math.random() * options.reduce((sum, act) => sum + act.weight, 0)
  for (const act of options) {
    roll -= act.weight
    if (roll <= 0) return act
  }
  return options[0]
}

export function HomeMascot({ className }: { className?: string }) {
  const [state, setState] = React.useState<MascotState>(GREETING.state)
  const wrapper = React.useRef<HTMLDivElement>(null)
  const timer = React.useRef(0)
  const queue = React.useRef<Step[]>([])
  const lastAct = React.useRef<Act | null>(null)
  const pointerSeen = React.useRef(false)
  const mood = React.useRef({ clicks: 0, lastClick: 0, calmAt: 0 })

  // Steps chain through timers; refs keep the chain on the latest closures.
  const play = React.useRef((step: Step) => {
    setState(step.state)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => advance.current(), step.ms)
  })
  const advance = React.useRef(() => {
    // With reduced motion it holds the greeting between reactions.
    if (reducedMotion()) {
      setState(GREETING.state)
      return
    }
    if (queue.current.length === 0) {
      const act = pickAct(lastAct.current, pointerSeen.current)
      lastAct.current = act
      queue.current = [...act.steps]
    }
    play.current(queue.current.shift()!)
  })

  React.useEffect(() => {
    if (!reducedMotion()) play.current(GREETING)
    return () => window.clearTimeout(timer.current)
  }, [])

  // The pointer's direction from the eyes, as -1..1 per axis; the
  // `watching` choreography reads it. Mouse and pen only — a touch has no
  // hover to follow.
  React.useEffect(() => {
    const el = wrapper.current
    if (!el) return
    let frame = 0
    let x = 0
    let y = 0
    const update = () => {
      frame = 0
      const box = el.getBoundingClientRect()
      const dx = x - (box.left + box.width / 2)
      const dy = y - (box.top + box.height * 0.4)
      el.style.setProperty(
        "--mascot-look-x",
        (dx / (Math.abs(dx) + 180)).toFixed(3)
      )
      el.style.setProperty(
        "--mascot-look-y",
        (dy / (Math.abs(dy) + 140)).toFixed(3)
      )
    }
    const onMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return
      pointerSeen.current = true
      x = event.clientX
      y = event.clientY
      if (!frame) frame = requestAnimationFrame(update)
    }
    window.addEventListener("pointermove", onMove, { passive: true })
    return () => {
      window.removeEventListener("pointermove", onMove)
      cancelAnimationFrame(frame)
    }
  }, [])

  const tickle = () => {
    if (!reducedMotion()) {
      wrapper.current?.animate(
        [
          { transform: "scale(1)" },
          { transform: "scale(1.08, 0.9)", offset: 0.3 },
          { transform: "scale(0.97, 1.04)", offset: 0.65 },
          { transform: "scale(1)" },
        ],
        { duration: 320, easing: "ease-out" }
      )
    }

    // While it is angry or sulking a click only bounces it: the tantrum
    // runs its course however much the clicking goes on.
    const now = performance.now()
    const m = mood.current
    if (now < m.calmAt) return

    m.clicks = now - m.lastClick < TICKLE_WINDOW_MS ? m.clicks + 1 : 1
    m.lastClick = now

    if (m.clicks >= TICKLES_BEFORE_ANGRY) {
      m.clicks = 0
      m.calmAt = now + ANGRY.ms + SULK.ms
      queue.current = [SULK]
      play.current(ANGRY)
    } else {
      queue.current = []
      play.current(TICKLE)
    }
  }

  return (
    <div
      ref={wrapper}
      onClick={tickle}
      className={cn(
        "origin-bottom cursor-pointer touch-manipulation select-none",
        className
      )}
    >
      <Mascot state={state} className="w-full" />
    </div>
  )
}
