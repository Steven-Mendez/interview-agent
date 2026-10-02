import * as React from "react"

import { cn } from "@/lib/utils"

// The Interviewer Agent character — the product's logo, its AI avatar and
// its illustration, drawn from one set of shapes. Colors are brand constants
// (`--mascot-*` in styles.css), not theme tokens: the character is the same
// white robot on the light workspace and in the dark interview room.

export type MascotState =
  | "idle"
  | "greeting"
  | "listening"
  | "speaking"
  | "thinking"
  | "generating"
  | "notes"
  | "evaluating"
  | "waiting"
  | "success"
  | "warning"
  | "error"
  | "complete"
  | "dancing"
  | "watching"
  | "charging"
  | "tickled"
  | "angry"

export const MASCOT_STATE_LABELS: Record<MascotState, string> = {
  idle: "Ready",
  greeting: "Saying hello",
  listening: "Listening",
  speaking: "Speaking",
  thinking: "Thinking",
  generating: "Preparing a follow-up",
  notes: "Taking notes",
  evaluating: "Evaluating",
  waiting: "Waiting",
  success: "Done",
  warning: "Needs attention",
  error: "Something went wrong",
  complete: "Interview complete",
  dancing: "Dancing",
  watching: "Watching",
  charging: "Recharging",
  tickled: "Giggling",
  angry: "Grumpy",
}

type Eyes =
  "dot" | "happy" | "up" | "down" | "ring" | "cross" | "laugh" | "angry"

const EYES: Record<MascotState, Eyes> = {
  idle: "dot",
  greeting: "happy",
  listening: "dot",
  speaking: "happy",
  thinking: "up",
  generating: "up",
  notes: "down",
  evaluating: "down",
  waiting: "dot",
  success: "happy",
  warning: "ring",
  error: "cross",
  complete: "happy",
  dancing: "happy",
  watching: "dot",
  charging: "down",
  tickled: "laugh",
  angry: "angry",
}

const BAR_SHAPE = [0.55, 1, 0.7]

/** The full character with a state. `level` (0..1) is the live loudness of
 *  the voice while speaking; without it the speaking bars animate on their
 *  own. Decorative unless `label` is given.
 *
 *  Every moving part is its own group — body, head, antenna, eyes, arms —
 *  and styles.css choreographs them per `data-state`, so a state is one
 *  attribute and the motion lives in one place. */
export function Mascot({
  state = "idle",
  level,
  label,
  className,
}: {
  state?: MascotState
  level?: number
  label?: string
  className?: string
}) {
  const id = React.useId().replace(/:/g, "")
  const svg = useStateBridge(state)
  const eyes = useBlinkSwap(EYES[state])
  const speaking = state === "speaking"
  const metered = speaking && level !== undefined
  const loud = metered ? Math.min(1, level * 3.2) : 0
  const shell = `url(#${id}-shell)`

  return (
    <svg
      ref={svg.ref}
      viewBox="0 0 200 200"
      data-state={svg.initialState}
      data-metered={metered || undefined}
      style={
        metered ? ({ "--mascot-loud": loud } as React.CSSProperties) : undefined
      }
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("mascot aspect-square overflow-visible", className)}
    >
      <defs>
        <linearGradient id={`${id}-shell`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--mascot-shell)" />
          <stop offset="1" stopColor="var(--mascot-shade)" />
        </linearGradient>
      </defs>

      {/* Ground shadow: shrinks as the body lifts */}
      <ellipse
        cx="100"
        cy="190"
        rx="38"
        ry="5"
        fill="var(--mascot-ground)"
        className="mascot-shadow"
      />

      <g className="mascot-react">
        <g className="mascot-body">
          <g className="mascot-arm mascot-arm-left">
            <rect
              x="56"
              y="130"
              width="14"
              height="34"
              rx="7"
              fill={shell}
              stroke="var(--mascot-outline)"
              strokeWidth="1.5"
            />
            <circle cx="63" cy="166" r="8.5" fill="var(--mascot-accent)" />
          </g>

          {/* Torso, collar and chest light */}
          <rect
            x="67"
            y="122"
            width="66"
            height="56"
            rx="24"
            fill={shell}
            stroke="var(--mascot-outline)"
            strokeWidth="1.5"
          />
          <rect
            x="84"
            y="117"
            width="32"
            height="10"
            rx="5"
            fill="var(--mascot-accent)"
          />
          <rect
            x={metered ? 100 - (8 + loud * 8) : 92}
            y="144"
            width={metered ? 16 + loud * 16 : 16}
            height="7"
            rx="3.5"
            fill={
              speaking ? "var(--mascot-accent)" : "var(--mascot-accent-soft)"
            }
            className="mascot-chest transition-[x,width] duration-100"
          />

          {state === "notes" && (
            <g className="mascot-notepad">
              <rect
                x="80"
                y="136"
                width="40"
                height="34"
                rx="5"
                fill="var(--mascot-accent)"
              />
              <rect
                x="86"
                y="144"
                width="22"
                height="3"
                rx="1.5"
                fill="var(--mascot-shell)"
              />
              <rect
                x="86"
                y="151"
                width="28"
                height="3"
                rx="1.5"
                fill="var(--mascot-accent-soft)"
                className="mascot-note-line"
              />
              <rect
                x="86"
                y="158"
                width="18"
                height="3"
                rx="1.5"
                fill="var(--mascot-accent-soft)"
                className="mascot-note-line"
                style={{ animationDelay: "600ms" }}
              />
            </g>
          )}

          {/* Head: everything above the collar moves together */}
          <g className="mascot-head">
            <g className="mascot-antenna">
              <rect
                x="98.5"
                y="22"
                width="3"
                height="18"
                rx="1.5"
                fill="var(--mascot-face)"
              />
              <circle
                cx="100"
                cy="19"
                r="8"
                fill="var(--mascot-accent)"
                className="mascot-antenna-light"
              />
            </g>
            <rect
              x="35"
              y="64"
              width="14"
              height="34"
              rx="7"
              fill="var(--mascot-accent)"
            />
            <rect
              x="151"
              y="64"
              width="14"
              height="34"
              rx="7"
              fill="var(--mascot-accent)"
            />
            <rect
              x="44"
              y="37"
              width="112"
              height="86"
              rx="36"
              fill={shell}
              stroke="var(--mascot-outline)"
              strokeWidth="1.5"
            />
            <rect
              x="57"
              y="50"
              width="86"
              height="61"
              rx="25"
              fill="var(--mascot-face)"
            />
            <rect
              x="57"
              y="50"
              width="86"
              height="61"
              rx="25"
              fill="var(--mascot-signal-error)"
              className="mascot-flush"
            />
            {/* A soft reflection across the screen */}
            <path
              d="M70 58q14-5 30-4"
              fill="none"
              stroke="var(--mascot-eye)"
              strokeOpacity="0.14"
              strokeWidth="4"
              strokeLinecap="round"
            />
            <g className="mascot-eyes">
              <g ref={eyes.ref} className="mascot-eyelid">
                <MascotEyes eyes={eyes.shown} />
              </g>
            </g>
          </g>

          {/* Right arm last, so a raised hand passes in front */}
          <g className="mascot-arm mascot-arm-right">
            <rect
              x="130"
              y="130"
              width="14"
              height="34"
              rx="7"
              fill={shell}
              stroke="var(--mascot-outline)"
              strokeWidth="1.5"
            />
            <circle cx="137" cy="166" r="8.5" fill="var(--mascot-accent)" />
          </g>
        </g>
      </g>

      <g key={state} className="mascot-symbol">
        <StateSymbol state={state} level={metered ? loud : undefined} />
      </g>
    </svg>
  )
}

// ---- State changes ----------------------------------------------------------

const PARTS =
  ".mascot-body, .mascot-head, .mascot-antenna, .mascot-eyes, .mascot-arm, .mascot-shadow"
const BRIDGE_MS = 560
const BRIDGE_EASING = "cubic-bezier(0.34, 1.3, 0.64, 1)"

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
}

/** Carries the character from one state's choreography into the next.
 *
 *  Swapping `data-state` swaps CSS animations, and a new animation starts
 *  from its own first frame wherever the part happened to be — a visible
 *  jump that CSS transitions cannot smooth. So `data-state` is driven here
 *  rather than rendered: before the swap every part's live pose is read,
 *  after it the new loops are held on their first frame while each part
 *  springs from the old pose into it, then the loops are released. The
 *  body also squashes and settles, so the change reads as a reaction. */
function useStateBridge(state: MascotState) {
  const ref = React.useRef<SVGSVGElement>(null)
  const [initialState] = React.useState(state)
  const pending = React.useRef<{
    bridges: Animation[]
    held: Animation[]
  } | null>(null)

  React.useLayoutEffect(() => {
    const svg = ref.current
    if (!svg || svg.dataset.state === state) return
    const parts = Array.from(svg.querySelectorAll<SVGGElement>(PARTS))
    const from = parts.map((el) => getComputedStyle(el).transform)

    // Interrupting a bridge: its poses were just read, so drop it.
    if (pending.current) {
      pending.current.bridges.forEach((a) => a.cancel())
      pending.current.held.forEach((a) => a.play())
      pending.current = null
    }
    svg.dataset.state = state
    if (reducedMotion()) return

    const bridges: Animation[] = []
    const held: Animation[] = []
    parts.forEach((el, i) => {
      const to = getComputedStyle(el).transform
      if (to === from[i]) return
      const loops = el.getAnimations()
      loops.forEach((a) => a.pause())
      held.push(...loops)
      bridges.push(
        el.animate([{ transform: from[i] }, { transform: to }], {
          duration: BRIDGE_MS,
          easing: BRIDGE_EASING,
        })
      )
    })
    svg
      .querySelector(".mascot-react")
      ?.animate(
        [
          { transform: "none" },
          { transform: "translateY(2px) scale(1.06, 0.92)", offset: 0.25 },
          { transform: "translateY(-3px) scale(0.98, 1.03)", offset: 0.6 },
          { transform: "none" },
        ],
        { duration: BRIDGE_MS, easing: "ease-in-out" }
      )

    const current = { bridges, held }
    pending.current = current
    Promise.all(bridges.map((a) => a.finished)).then(
      () => {
        if (pending.current !== current) return
        held.forEach((a) => a.play())
        pending.current = null
      },
      () => {}
    )
  }, [state])

  return { ref, initialState }
}

/** Changes the eyes behind a blink, so a new expression never pops in. */
function useBlinkSwap(eyes: Eyes) {
  const ref = React.useRef<SVGGElement>(null)
  const [shown, setShown] = React.useState(eyes)
  const last = React.useRef(eyes)

  React.useEffect(() => {
    if (last.current === eyes) return
    last.current = eyes
    if (!reducedMotion()) {
      ref.current?.animate(
        [
          { transform: "scaleY(1)" },
          { transform: "scaleY(0.06)", offset: 0.4 },
          { transform: "scaleY(0.06)", offset: 0.55 },
          { transform: "scaleY(1)" },
        ],
        { duration: 240, easing: "ease-in-out" }
      )
    }
    const timer = window.setTimeout(() => setShown(eyes), 110)
    return () => window.clearTimeout(timer)
  }, [eyes])

  return { ref, shown }
}

function MascotEyes({ eyes }: { eyes: Eyes }) {
  const stroke = {
    fill: "none",
    stroke: "var(--mascot-eye)",
    strokeWidth: 6,
    strokeLinecap: "round" as const,
  }
  switch (eyes) {
    case "happy":
      return (
        <g {...stroke}>
          <path d="M74 86q8-11 16 0" />
          <path d="M110 86q8-11 16 0" />
        </g>
      )
    case "down":
      return (
        <g {...stroke}>
          <path d="M74 80q8 9 16 0" />
          <path d="M110 80q8 9 16 0" />
        </g>
      )
    case "ring":
      return (
        <g {...stroke} strokeWidth={4.5}>
          <circle cx="82" cy="81" r="6.5" />
          <circle cx="118" cy="81" r="6.5" />
        </g>
      )
    case "cross":
      return (
        <g {...stroke} stroke="var(--mascot-error-eye)">
          <path d="M75 85l14-6" />
          <path d="M125 85l-14-6" />
        </g>
      )
    case "laugh":
      return (
        <g {...stroke} strokeLinejoin="round">
          <path d="M75 74l13 7-13 7" />
          <path d="M125 74l-13 7 13 7" />
        </g>
      )
    case "angry":
      return (
        <g>
          <g {...stroke} strokeWidth={5}>
            <path d="M71 68l20 8" />
            <path d="M129 68l-20 8" />
          </g>
          <g fill="var(--mascot-eye)">
            <circle cx="83" cy="86" r="7" />
            <circle cx="117" cy="86" r="7" />
          </g>
        </g>
      )
    case "up":
      return (
        <g fill="var(--mascot-eye)" className="mascot-blink">
          <circle cx="86" cy="76" r="8.5" />
          <circle cx="122" cy="76" r="8.5" />
        </g>
      )
    default:
      return (
        <g fill="var(--mascot-eye)" className="mascot-blink">
          <circle cx="82" cy="81" r="8.5" />
          <circle cx="118" cy="81" r="8.5" />
        </g>
      )
  }
}

/** The small graphic beside the character that names its state. */
function StateSymbol({ state, level }: { state: MascotState; level?: number }) {
  const line = {
    fill: "none",
    stroke: "var(--mascot-accent)",
    strokeWidth: 4.5,
    strokeLinecap: "round" as const,
  }
  switch (state) {
    case "greeting":
      return (
        <g {...line}>
          <path d="M177 84l9-6" className="mascot-ray" />
          <path
            d="M181 98l11-1"
            className="mascot-ray"
            style={{ animationDelay: "150ms" }}
          />
          <path
            d="M178 112l9 5"
            className="mascot-ray"
            style={{ animationDelay: "300ms" }}
          />
        </g>
      )
    case "listening":
      return (
        <g {...line}>
          {["M173 72q7 9 0 18", "M181 64q12 17 0 34", "M189 56q17 25 0 50"].map(
            (d, i) => (
              <path
                key={d}
                d={d}
                className="mascot-wave"
                style={{ animationDelay: `${i * 220}ms` }}
              />
            )
          )}
        </g>
      )
    case "speaking":
      return (
        <g fill="var(--mascot-accent)">
          {[172, 181, 190].map((x, i) => {
            const h =
              level === undefined
                ? 26 * BAR_SHAPE[i]
                : 6 + level * 26 * BAR_SHAPE[i]
            return (
              <rect
                key={x}
                x={x - 2.5}
                y={81 - h / 2}
                width="5"
                height={h}
                rx="2.5"
                className={cn(
                  "transition-[height,y] duration-75",
                  level === undefined && "mascot-bar"
                )}
                style={
                  level === undefined
                    ? { animationDelay: `${i * 140}ms` }
                    : undefined
                }
              />
            )
          })}
        </g>
      )
    case "thinking":
      return (
        <g fill="var(--mascot-accent)">
          {[150, 163, 176].map((x, i) => (
            <circle
              key={x}
              cx={x}
              cy="18"
              r="4.5"
              className="mascot-dot"
              style={{ animationDelay: `${i * 160}ms` }}
            />
          ))}
        </g>
      )
    case "generating":
      return (
        <g fill="var(--mascot-signal-warning)">
          <path
            d="M166 14l3.5 9 9 3.5-9 3.5-3.5 9-3.5-9-9-3.5 9-3.5z"
            className="mascot-twinkle"
          />
          <path
            d="M186 40l2 5 5 2-5 2-2 5-2-5-5-2 5-2z"
            className="mascot-twinkle"
            style={{ animationDelay: "400ms" }}
          />
          <path
            d="M148 36l1.5 4 4 1.5-4 1.5-1.5 4-1.5-4-4-1.5 4-1.5z"
            fill="var(--mascot-accent)"
            className="mascot-twinkle"
            style={{ animationDelay: "800ms" }}
          />
        </g>
      )
    case "evaluating":
      return (
        <g>
          <rect
            x="156"
            y="96"
            width="40"
            height="32"
            rx="6"
            fill="var(--mascot-accent-container)"
          />
          <g fill="var(--mascot-accent)">
            {[
              { x: 163, h: 8 },
              { x: 172, h: 14 },
              { x: 181, h: 19 },
            ].map((bar, i) => (
              <rect
                key={bar.x}
                x={bar.x}
                y={122 - bar.h}
                width="5"
                height={bar.h}
                rx="1.5"
                className="mascot-grow"
                style={{ animationDelay: `${i * 200}ms` }}
              />
            ))}
          </g>
        </g>
      )
    case "success":
      return (
        <g className="mascot-pop">
          <circle
            cx="166"
            cy="150"
            r="15"
            fill="var(--mascot-signal-success)"
            className="mascot-halo"
          />
          <circle
            cx="166"
            cy="150"
            r="15"
            fill="var(--mascot-signal-success)"
          />
          <path
            d="M159 150l5 5 9-10"
            fill="none"
            stroke="#fff"
            strokeWidth="4"
            strokeLinecap="round"
            strokeLinejoin="round"
            pathLength={1}
            className="mascot-draw"
          />
        </g>
      )
    case "warning":
      return (
        <g className="mascot-pop">
          <g className="mascot-wiggle">
            <path
              d="M166 132l17 30h-34z"
              fill="var(--mascot-signal-warning)"
              stroke="var(--mascot-signal-warning)"
              strokeWidth="5"
              strokeLinejoin="round"
            />
            <rect x="164" y="141" width="4" height="11" rx="2" fill="#3d2a00" />
            <circle cx="166" cy="157" r="2.4" fill="#3d2a00" />
          </g>
        </g>
      )
    case "error":
      return (
        <g className="mascot-pop">
          <circle
            cx="166"
            cy="150"
            r="15"
            fill="var(--mascot-signal-error)"
            className="mascot-halo"
          />
          <circle cx="166" cy="150" r="15" fill="var(--mascot-signal-error)" />
          <rect x="164" y="140" width="4" height="12" rx="2" fill="#fff" />
          <circle cx="166" cy="157.5" r="2.4" fill="#fff" />
        </g>
      )
    case "dancing":
      return (
        <g fill="var(--mascot-accent)">
          {[
            { x: 172, y: 52, delay: 0 },
            { x: 24, y: 70, delay: 800 },
            { x: 182, y: 104, delay: 1600 },
          ].map((n) => (
            <g
              key={n.x}
              className="mascot-music"
              style={{ animationDelay: `${n.delay}ms` }}
            >
              <ellipse
                cx={n.x}
                cy={n.y}
                rx="5"
                ry="4"
                transform={`rotate(-20 ${n.x} ${n.y})`}
              />
              <rect x={n.x + 3} y={n.y - 18} width="2.5" height="18" rx="1" />
              <path
                d={`M${n.x + 4} ${n.y - 18}q8 3 6 11`}
                fill="none"
                stroke="var(--mascot-accent)"
                strokeWidth="2.5"
                strokeLinecap="round"
              />
            </g>
          ))}
        </g>
      )
    case "charging":
      return (
        <g>
          <path
            d="M174 102l-9 15h7l-3 12 11-16h-7l3-11z"
            fill="var(--mascot-signal-warning)"
            className="mascot-bolt"
          />
          <rect
            x="156"
            y="133"
            width="30"
            height="17"
            rx="4"
            fill="none"
            stroke="var(--mascot-accent)"
            strokeWidth="3"
          />
          <rect
            x="187.5"
            y="138"
            width="3.5"
            height="7"
            rx="1.5"
            fill="var(--mascot-accent)"
          />
          {[160, 168, 176].map((x, i) => (
            <rect
              key={x}
              x={x}
              y="137"
              width="6"
              height="9"
              rx="1.5"
              fill="var(--mascot-signal-success)"
              className="mascot-cell"
              style={{ animationDelay: `${400 + i * 1700}ms` }}
            />
          ))}
        </g>
      )
    case "tickled":
      return (
        <g
          fill="var(--mascot-accent)"
          fontFamily="var(--font-heading)"
          fontWeight="700"
        >
          {[
            { x: 158, y: 42, size: 17, delay: 0 },
            { x: 170, y: 72, size: 13, delay: 300 },
            { x: 12, y: 56, size: 15, delay: 550 },
          ].map((ha) => (
            <text
              key={ha.x}
              x={ha.x}
              y={ha.y}
              fontSize={ha.size}
              className="mascot-haha"
              style={{ animationDelay: `${ha.delay}ms` }}
            >
              ha
            </text>
          ))}
        </g>
      )
    case "angry":
      return (
        <g>
          <g
            fill="none"
            stroke="var(--mascot-signal-error)"
            strokeWidth="4"
            strokeLinecap="round"
            className="mascot-vein"
          >
            <path d="M160 28q5 0 5-5" />
            <path d="M173 23q0 5 5 5" />
            <path d="M178 40q-5 0-5 5" />
            <path d="M165 45q0-5-5-5" />
          </g>
          {[
            { x: 36, dx: -10, delay: 0 },
            { x: 164, dx: 10, delay: 350 },
            { x: 40, dx: -6, delay: 700 },
          ].map((puff, i) => (
            <circle
              key={i}
              cx={puff.x}
              cy="62"
              r="6"
              fill="var(--mascot-outline)"
              className="mascot-steam"
              style={
                {
                  "--steam-x": `${puff.dx}px`,
                  animationDelay: `${puff.delay}ms`,
                } as React.CSSProperties
              }
            />
          ))}
        </g>
      )
    case "complete":
      return (
        <g>
          {[
            { x: 30, y: 40, r: -35, c: "warning" },
            { x: 22, y: 72, r: 20, c: "accent" },
            { x: 170, y: 34, r: 40, c: "success" },
            { x: 182, y: 66, r: -20, c: "error" },
            { x: 46, y: 18, r: 60, c: "success" },
            { x: 156, y: 12, r: -50, c: "warning" },
          ].map((p, i) => (
            <g
              key={i}
              className="mascot-confetti"
              style={{ animationDelay: `${i * 180}ms` }}
            >
              <rect
                x={p.x - 4}
                y={p.y - 2}
                width="8"
                height="4"
                rx="2"
                transform={`rotate(${p.r} ${p.x} ${p.y})`}
                fill={
                  p.c === "accent"
                    ? "var(--mascot-accent)"
                    : `var(--mascot-signal-${p.c})`
                }
              />
            </g>
          ))}
        </g>
      )
    default:
      return null
  }
}

// ---- Head mark ---------------------------------------------------------------

/** The head alone, drawn on a 48 grid so it holds up at 16–24 px: the
 *  product's brand mark and the AI avatar. `outlined` traces the head in
 *  blue for surfaces where a white head would vanish. */
export function MascotHead({
  className,
  outlined = false,
  eyes = "dot",
}: {
  className?: string
  outlined?: boolean
  eyes?: "dot" | "happy"
}) {
  return (
    <svg viewBox="0 0 48 48" aria-hidden className={cn("shrink-0", className)}>
      <circle cx="24" cy="8" r="4" fill="var(--mascot-accent)" />
      <rect
        x="22.75"
        y="10"
        width="2.5"
        height="6"
        rx="1.25"
        fill="var(--mascot-face)"
      />
      <rect
        x="2.5"
        y="23"
        width="6"
        height="13"
        rx="3"
        fill="var(--mascot-accent)"
      />
      <rect
        x="39.5"
        y="23"
        width="6"
        height="13"
        rx="3"
        fill="var(--mascot-accent)"
      />
      <rect
        x="6.5"
        y="15"
        width="35"
        height="28"
        rx="12"
        fill="var(--mascot-shell)"
        stroke={outlined ? "var(--mascot-accent)" : "var(--mascot-outline)"}
        strokeWidth={outlined ? 2.5 : 1.25}
      />
      <rect
        x="11"
        y="19.5"
        width="26"
        height="19"
        rx="8"
        fill="var(--mascot-face)"
      />
      {eyes === "happy" ? (
        <g
          fill="none"
          stroke="var(--mascot-eye)"
          strokeWidth="2.6"
          strokeLinecap="round"
        >
          <path d="M16 31q2.75-3.6 5.5 0" />
          <path d="M26.5 31q2.75-3.6 5.5 0" />
        </g>
      ) : (
        <g fill="var(--mascot-eye)">
          <circle cx="18.75" cy="29" r="3" />
          <circle cx="29.25" cy="29" r="3" />
        </g>
      )}
    </svg>
  )
}

const AVATAR_TONES = {
  /** No container: the head on whatever surface it sits on. */
  plain: "",
  light: "bg-card ring-1 ring-border",
  tonal: "bg-primary-container",
  dark: "bg-[#202124]",
  chip: "bg-create-container",
} as const

/** The AI's avatar: the head mark in a circle. Used wherever the agent is
 *  credited — attribution, side panels, the participant identity. */
export function MascotAvatar({
  size = 32,
  tone = "tonal",
  active = false,
  label,
  className,
}: {
  size?: number
  tone?: keyof typeof AVATAR_TONES
  /** A ring for "speaking now" — the call's active-speaker treatment. */
  active?: boolean
  label?: string
  className?: string
}) {
  return (
    <span
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      style={{ width: size, height: size }}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full transition-shadow duration-200",
        AVATAR_TONES[tone],
        active && "shadow-[0_0_0_3px_var(--primary)]",
        className
      )}
    >
      <MascotHead
        className={tone === "plain" ? "size-full" : "size-[78%]"}
        outlined={size <= 20}
      />
    </span>
  )
}
