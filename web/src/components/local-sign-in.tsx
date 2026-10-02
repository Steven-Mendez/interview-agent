import * as React from "react"
import { useRouter } from "@tanstack/react-router"
import { AlertCircleIcon, EyeIcon, EyeOffIcon, LogInIcon } from "lucide-react"

import type { SignInActivity, SignInField } from "@/components/sign-in-page"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { resetAuthMode } from "@/lib/auth"
import { LocalSignInError, SIGN_IN_UNAVAILABLE, signIn } from "@/lib/local-auth"

const MISSING_FIELDS = "Enter your username and password."
/** Characters a field holds edge to edge, for guessing where the caret is
 *  where the text cannot be measured. */
const GUESS_CHARS = 30

/** Where the caret sits along a text field: 0 at the start of its box, 1 at
 *  the end. Measures the text before the caret in the field's own font (the
 *  dots, for a hidden password), less what the field scrolled; where there
 *  is no layout to measure, it guesses from the number of characters. */
export function caretRatio(input: HTMLInputElement): number {
  const caret = input.selectionStart ?? input.value.length
  const style = getComputedStyle(input)
  const box =
    input.clientWidth -
    parseFloat(style.paddingLeft) -
    parseFloat(style.paddingRight)
  const before =
    input.type === "password"
      ? "\u2022".repeat(caret)
      : input.value.slice(0, caret)
  const width = box > 0 ? measureText(before, style) : null
  const ratio =
    width === null ? caret / GUESS_CHARS : (width - input.scrollLeft) / box
  return Math.min(1, Math.max(0, ratio))
}

let measuring: CanvasRenderingContext2D | null | undefined
function measureText(text: string, style: CSSStyleDeclaration) {
  measuring ??= document.createElement("canvas").getContext("2d")
  if (!measuring) return null
  measuring.font =
    style.font ||
    `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
  return measuring.measureText(text).width
}

/** The dev login: a local account's username and password (LOCAL_ACCOUNTS
 *  of the API). `redirectTo` is already a path on this site — the route
 *  checked it — and may carry a query and a hash.
 *
 *  `onActivity` hears what the visitor is doing (typing the username or the
 *  password, showing or hiding the password, submitting, a failed attempt),
 *  `onCaret` where the caret is in the field being typed in (see
 *  caretRatio), and `beforeRedirect` is awaited once signed in, before
 *  leaving — the page uses all three for its character. */
export function LocalSignInForm({
  redirectTo,
  onActivity,
  onCaret,
  beforeRedirect,
}: {
  redirectTo?: string
  onActivity?: (activity: SignInActivity) => void
  onCaret?: (ratio: number, field: SignInField) => void
  beforeRedirect?: () => Promise<void>
}) {
  const router = useRouter()
  const [username, setUsername] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [showPassword, setShowPassword] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const passwordInput = React.useRef<HTMLInputElement>(null)
  // Focus the form moves itself (the username on arrival, the password after
  // a failed attempt) is not the visitor's activity.
  const ownFocus = React.useRef(true)
  React.useEffect(() => {
    ownFocus.current = false
  }, [])
  // The fields are disabled while an attempt is checked, which drops the
  // focus to the page; a failed attempt hands it back to the password.
  const refocus = React.useRef(false)
  React.useEffect(() => {
    if (pending || !refocus.current) return
    refocus.current = false
    ownFocus.current = true
    passwordInput.current?.focus()
    ownFocus.current = false
  }, [pending])

  const follow = (
    event: React.SyntheticEvent<HTMLInputElement>,
    field: SignInField
  ) => {
    if (onCaret) onCaret(caretRatio(event.currentTarget), field)
  }

  // Leaving with the password shown (the API switched how it signs in, say)
  // must not leave the listener thinking it still is.
  const shownOnLeave = React.useRef({ showPassword, onActivity })
  React.useEffect(() => {
    shownOnLeave.current = { showPassword, onActivity }
  })
  React.useEffect(
    () => () => {
      const { showPassword: shown, onActivity: report } = shownOnLeave.current
      if (shown) report?.("password-hidden")
    },
    []
  )

  const toggleShown = () => {
    const shown = !showPassword
    setShowPassword(shown)
    onActivity?.(shown ? "password-shown" : "password-hidden")
  }

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pending) return
    // Read what the fields hold now: a browser's autofill fills them without
    // the change events the state follows.
    const fields = new FormData(event.currentTarget)
    const name = String(fields.get("username") ?? "")
    const secret = String(fields.get("password") ?? "")
    if (name === "" || secret === "") {
      setError(MISSING_FIELDS)
      onActivity?.("failed")
      return
    }
    setPending(true)
    setError(null)
    onActivity?.("submitting")
    try {
      await signIn(name, secret)
    } catch (caught) {
      if (caught instanceof LocalSignInError && caught.signInOff) {
        // Re-run this route's check: it sends the visitor home, or to the
        // Neon notice, for whatever sign-in the API runs now.
        resetAuthMode()
        void router.invalidate()
      }
      setError(
        caught instanceof LocalSignInError
          ? caught.message
          : SIGN_IN_UNAVAILABLE
      )
      refocus.current = true
      setPending(false)
      onActivity?.("failed")
      return
    }
    await beforeRedirect?.()
    await router.navigate({ href: redirectTo ?? "/", replace: true })
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      noValidate
      className="flex flex-col gap-4"
    >
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="local-username">Username</FieldLabel>
          <Input
            id="local-username"
            name="username"
            value={username}
            onChange={(event) => {
              setUsername(event.target.value)
              onActivity?.("username")
              follow(event, "username")
            }}
            onFocus={(event) => {
              if (!ownFocus.current) onActivity?.("username")
              follow(event, "username")
            }}
            onSelect={(event) => follow(event, "username")}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            // The one thing to do on this page.
            autoFocus
            required
            disabled={pending}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor="local-password">Password</FieldLabel>
          <div className="relative">
            <Input
              id="local-password"
              name="password"
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(event) => {
                setPassword(event.target.value)
                onActivity?.("password")
                follow(event, "password")
              }}
              ref={passwordInput}
              onFocus={(event) => {
                if (!ownFocus.current) onActivity?.("password")
                follow(event, "password")
              }}
              onSelect={(event) => follow(event, "password")}
              autoComplete="current-password"
              required
              disabled={pending}
              className="pr-12"
            />
            <Button
              type="button"
              variant="quiet"
              size="icon-sm"
              aria-label="Show password"
              aria-pressed={showPassword}
              aria-controls="local-password"
              onClick={toggleShown}
              disabled={pending}
              className="absolute top-1/2 right-1.5 -translate-y-1/2"
            >
              {showPassword ? <EyeOffIcon /> : <EyeIcon />}
            </Button>
          </div>
        </Field>
        {error && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <Button type="submit" disabled={pending}>
          {pending ? <Spinner className="text-current" /> : <LogInIcon />}
          {pending ? "Signing in…" : "Sign in"}
        </Button>
      </FieldGroup>
    </form>
  )
}
