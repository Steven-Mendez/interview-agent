import * as React from "react"
import { Link } from "@tanstack/react-router"

import { useImmersive } from "@/components/app-shell"
import { Mascot } from "@/components/mascot"
import type { MascotState } from "@/components/mascot"
import { GitHubMark, GoogleMark } from "@/components/provider-marks"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { PRODUCT_NAME } from "@/lib/head"
import { cn } from "@/lib/utils"

// Signing in is a room of its own: no top bar, no navigation — the
// Interviewer Agent on one side, the card on the other (stacked on narrow
// screens). The character reacts to the form: it waves on arrival, follows
// the caret along the field being typed in, covers its eyes while the
// password is shown, thinks while the account is checked, and is pleased
// (or upset) with the result.

/** What the sign-in form tells the page, for the character to react to. */
export type SignInActivity =
  | "username"
  | "password"
  | "password-shown"
  | "password-hidden"
  | "submitting"
  | "failed"

/** The form's fields the character can follow. */
export type SignInField = "username" | "password"

export const GREETING_MS = 2000
export const ERROR_MS = 2500
export const SUCCESS_MS = 700
export const TICKLE_MS = 1500

interface SignInReactions {
  react: (activity: SignInActivity) => void
  /** The caret moved in `field`: `ratio` is where it sits along the field,
   *  0 at its start and 1 at its end. The character's eyes go there. */
  follow: (ratio: number, field: SignInField) => void
  /** Shows the character pleased, resolving when the moment has passed. */
  celebrate: () => Promise<void>
}

const SignInReactionsContext = React.createContext<SignInReactions>({
  react: () => {},
  follow: () => {},
  celebrate: () => Promise.resolve(),
})

/** The page's character, for a form inside it to drive. Outside a
 *  SignInPage it does nothing. */
export function useSignInReactions(): SignInReactions {
  return React.useContext(SignInReactionsContext)
}

/** Where the character looks for a caret at either end of a field, as the
 *  `--mascot-look-x/-y` its watching pose reads (-1..1). Side by side (the
 *  md layout) the card is to its right and a little below; stacked, it is
 *  right below. Wider than the true angles, so the eyes visibly travel. */
const SIDE_BY_SIDE = "(min-width: 48rem)"
const GAZE = {
  side: { start: 0.2, end: 1, y: { username: 0.2, password: 0.4 } },
  stacked: { start: -0.55, end: 0.55, y: { username: 0.7, password: 0.85 } },
} as const

function clamp01(value: number) {
  return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0
}

/** The character's state: a resting state the form sets (idle, watching,
 *  covering, thinking) under a passing one that times out (greeting, error,
 *  tickled) — or success, which lasts until the page is left. While the
 *  password is shown it covers its eyes instead of watching. */
function useSignInMascot(resting: MascotState) {
  const [base, setBase] = React.useState<MascotState>(resting)
  const [passing, setPassing] = React.useState<MascotState | null>("greeting")
  const timer = React.useRef(0)
  const successTimer = React.useRef(0)
  const celebrating = React.useRef(false)
  const alive = React.useRef(true)
  const passwordShown = React.useRef(false)
  // Where it looks is written straight onto the element, not rendered:
  // the eyes follow every keystroke without a render for each.
  const lookRef = React.useRef<HTMLButtonElement>(null)
  // The last caret it followed, to aim again when the layout flips between
  // side by side and stacked (a turned tablet, a resized window).
  const caret = React.useRef<{ ratio: number; field: SignInField } | null>(null)

  const look = React.useCallback(() => {
    const el = lookRef.current
    if (!el || !caret.current) return
    const { ratio, field } = caret.current
    const gaze = window.matchMedia(SIDE_BY_SIDE).matches
      ? GAZE.side
      : GAZE.stacked
    const x = gaze.start + (gaze.end - gaze.start) * clamp01(ratio)
    el.style.setProperty("--mascot-look-x", x.toFixed(3))
    el.style.setProperty("--mascot-look-y", gaze.y[field].toFixed(3))
  }, [])

  React.useEffect(() => {
    const layout = window.matchMedia(SIDE_BY_SIDE)
    layout.addEventListener("change", look)
    return () => layout.removeEventListener("change", look)
  }, [look])

  const pass = React.useCallback((state: MascotState | null, ms?: number) => {
    if (!alive.current) return
    window.clearTimeout(timer.current)
    setPassing(state)
    if (state !== null && ms !== undefined) {
      timer.current = window.setTimeout(() => setPassing(null), ms)
    }
  }, [])

  React.useEffect(() => {
    alive.current = true
    pass("greeting", GREETING_MS)
    return () => {
      alive.current = false
      window.clearTimeout(timer.current)
      window.clearTimeout(successTimer.current)
    }
  }, [pass])

  const reactions = React.useMemo<SignInReactions>(() => {
    const settle = (state: MascotState) => {
      if (!alive.current) return
      celebrating.current = false
      setBase(state)
      pass(null)
    }
    const attentive = () => (passwordShown.current ? "covering" : "watching")
    return {
      react: (activity) => {
        switch (activity) {
          case "username":
          case "password":
            return settle(attentive())
          case "password-shown":
            passwordShown.current = true
            return settle("covering")
          case "password-hidden":
            // Hiding it (or the form going away with it shown) uncovers the
            // eyes, without cutting short an error or a giggle on screen.
            passwordShown.current = false
            return setBase((state) =>
              state === "covering" ? "watching" : state
            )
          case "submitting":
            return settle("thinking")
          case "failed":
            settle(attentive())
            return pass("error", ERROR_MS)
        }
      },
      follow: (ratio, field) => {
        caret.current = { ratio, field }
        look()
      },
      celebrate: () =>
        new Promise<void>((resolve) => {
          if (!alive.current) return
          celebrating.current = true
          pass("success")
          window.clearTimeout(successTimer.current)
          successTimer.current = window.setTimeout(resolve, SUCCESS_MS)
        }),
    }
  }, [pass, look])

  // Easter egg: it giggles when poked — unless it is busy being pleased.
  const tickle = React.useCallback(() => {
    if (!celebrating.current) pass("tickled", TICKLE_MS)
  }, [pass])

  return { state: passing ?? base, reactions, tickle, lookRef }
}

/** The whole-screen sign-in layout every view of /auth/$pathname uses.
 *  `children` go in the card. `resting` is where the character settles
 *  after its greeting — "greeting" again keeps it waving (signing out). */
export function SignInPage({
  resting = "idle",
  children,
}: {
  resting?: MascotState
  children: React.ReactNode
}) {
  useImmersive()
  const mascot = useSignInMascot(resting)
  return (
    <SignInReactionsContext.Provider value={mascot.reactions}>
      <div className="relative isolate flex min-h-dvh flex-1 flex-col overflow-hidden bg-background">
        <Backdrop />
        <div className="mx-auto flex w-full max-w-5xl flex-1 flex-col items-center gap-6 px-4 pt-2 pb-10 md:grid md:grid-cols-2 md:items-center md:gap-10 md:px-8 md:py-10">
          <div className="flex flex-col items-center gap-2 text-center md:gap-6">
            <button
              ref={mascot.lookRef}
              type="button"
              aria-label="Say hi to the interviewer"
              data-mascot-state={mascot.state}
              onClick={mascot.tickle}
              className="relative aspect-square w-28 cursor-pointer touch-manipulation rounded-full outline-offset-4 select-none [--mascot-look-x:0] [--mascot-look-y:0.8] md:w-64 md:[--mascot-look-x:0.8] md:[--mascot-look-y:0.25] lg:w-80"
            >
              <span
                aria-hidden
                className="absolute inset-[4%] rounded-full bg-primary-container/50 dark:bg-primary-container/35"
              />
              <Mascot state={mascot.state} className="relative w-full" />
            </button>
            <div className="flex flex-col items-center gap-1 md:gap-2">
              <p className="font-heading text-[1.375rem] leading-7 text-foreground md:text-[2.25rem] md:leading-[2.75rem] md:tracking-[-0.01em]">
                {PRODUCT_NAME}
              </p>
              <p className="max-w-xs text-sm text-pretty text-muted-foreground md:text-base">
                Practice interviews with an AI interviewer that reads your
                resume.
              </p>
            </div>
          </div>
          <div className="flex w-full max-w-sm flex-col items-center gap-4 md:justify-self-center">
            <div className="flex w-full flex-col gap-6 rounded-2xl border bg-card p-6 shadow-e1 dark:shadow-none">
              {children}
            </div>
            {/* Public, and linked from here: Google's consent screen points
                at them, and a visitor deciding whether to sign in reads them. */}
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <Link
                to="/privacy"
                className="rounded-sm underline-offset-4 hover:text-foreground hover:underline"
              >
                Privacy policy
              </Link>
              <span aria-hidden>·</span>
              <Link
                to="/terms"
                className="rounded-sm underline-offset-4 hover:text-foreground hover:underline"
              >
                Terms of service
              </Link>
            </p>
          </div>
        </div>
      </div>
    </SignInReactionsContext.Provider>
  )
}

/** Soft brand light behind everything, from the theme's own containers. */
function Backdrop() {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(42rem_30rem_at_12%_0%,var(--primary-container),transparent_70%),radial-gradient(38rem_28rem_at_100%_100%,var(--create-container),transparent_70%)] opacity-70 dark:opacity-35"
    />
  )
}

/** The card's title and one line under it. */
export function SignInHeading({
  title,
  description,
}: {
  title: React.ReactNode
  description?: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1">
      <h1 className="text-headline text-foreground">{title}</h1>
      {description && (
        <p className="text-sm text-muted-foreground">{description}</p>
      )}
    </div>
  )
}

/** One look for the provider buttons, ours and Neon Auth's alike: full
 *  width, a neutral outline, the brand mark before the label. */
export const providerButtonClasses =
  "h-11 w-full gap-3 rounded-full border-border bg-card font-heading text-sm font-medium text-foreground shadow-none"

/** Google and GitHub where they cannot be used yet: shown, disabled, and
 *  described by `description` (why, and what to do instead). Still
 *  focusable, so a keyboard user hears why. */
export function UnavailableProviders({
  description,
}: {
  description: React.ReactNode
}) {
  const id = React.useId()
  return (
    <div className="flex flex-col gap-3">
      {[
        { name: "Google", mark: <GoogleMark className="opacity-60" /> },
        { name: "GitHub", mark: <GitHubMark /> },
      ].map((provider) => (
        <Button
          key={provider.name}
          type="button"
          variant="outline"
          disabled
          focusableWhenDisabled
          aria-describedby={id}
          className={cn(providerButtonClasses, "has-[>svg:first-child]:pl-6")}
        >
          {provider.mark}
          Continue with {provider.name}
        </Button>
      ))}
      <div id={id}>{description}</div>
    </div>
  )
}

/** A rule across the card with a few words in the middle. */
export function SignInDivider({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3 text-xs text-muted-foreground">
      <Separator className="flex-1" />
      <span className="shrink-0">{children}</span>
      <Separator className="flex-1" />
    </div>
  )
}

/** A step in progress (signing out, finishing a sign-in). `spinner`
 *  replaces the default one — Neon Auth's views bring their own. */
export function SignInStatus({
  spinner,
  children,
}: {
  spinner?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div
      role="status"
      className="flex items-center gap-3 text-sm text-muted-foreground"
    >
      <span className="flex size-5 items-center justify-center text-primary [&_svg]:size-5">
        {spinner ?? <Spinner className="size-5" />}
      </span>
      {children}
    </div>
  )
}
