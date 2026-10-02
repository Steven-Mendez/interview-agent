import * as React from "react"

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
// screens). The character reacts to the form: it waves on arrival, watches
// the username being typed, politely waits through the password, thinks
// while the account is checked, and is pleased (or upset) with the result.

/** What the sign-in form tells the page, for the character to react to. */
export type SignInActivity = "username" | "password" | "submitting" | "failed"

export const GREETING_MS = 2000
export const ERROR_MS = 2500
export const SUCCESS_MS = 700
export const TICKLE_MS = 1500

interface SignInReactions {
  react: (activity: SignInActivity) => void
  /** Shows the character pleased, resolving when the moment has passed. */
  celebrate: () => Promise<void>
}

const SignInReactionsContext = React.createContext<SignInReactions>({
  react: () => {},
  celebrate: () => Promise.resolve(),
})

/** The page's character, for a form inside it to drive. Outside a
 *  SignInPage it does nothing. */
export function useSignInReactions(): SignInReactions {
  return React.useContext(SignInReactionsContext)
}

/** The character's state: a resting state the form sets (idle, watching,
 *  waiting, thinking) under a passing one that times out (greeting, error,
 *  tickled) — or success, which lasts until the page is left. */
function useSignInMascot(resting: MascotState) {
  const [base, setBase] = React.useState<MascotState>(resting)
  const [passing, setPassing] = React.useState<MascotState | null>("greeting")
  const timer = React.useRef(0)
  const successTimer = React.useRef(0)
  const celebrating = React.useRef(false)
  const alive = React.useRef(true)

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
    return {
      react: (activity) => {
        switch (activity) {
          case "username":
            return settle("watching")
          case "password":
            return settle("waiting")
          case "submitting":
            return settle("thinking")
          case "failed":
            settle("watching")
            return pass("error", ERROR_MS)
        }
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
  }, [pass])

  // Easter egg: it giggles when poked — unless it is busy being pleased.
  const tickle = React.useCallback(() => {
    if (!celebrating.current) pass("tickled", TICKLE_MS)
  }, [pass])

  return { state: passing ?? base, reactions, tickle }
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
          <div className="flex w-full max-w-sm flex-col gap-6 rounded-2xl border bg-card p-6 shadow-e1 md:justify-self-center dark:shadow-none">
            {children}
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
