import * as React from "react"
import { useRouter } from "@tanstack/react-router"
import { AlertCircleIcon, LogInIcon } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { resetAuthMode } from "@/lib/auth"
import { LocalSignInError, SIGN_IN_UNAVAILABLE, signIn } from "@/lib/local-auth"

/** The dev login: a local account's username and password (LOCAL_ACCOUNTS
 *  of the API). `redirectTo` is already a path on this site — the route
 *  checked it — and may carry a query and a hash. */
export function LocalSignInForm({ redirectTo }: { redirectTo?: string }) {
  const router = useRouter()
  const [username, setUsername] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pending) return
    setPending(true)
    setError(null)
    try {
      await signIn(username, password)
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
      setPending(false)
      return
    }
    await router.navigate({ href: redirectTo ?? "/", replace: true })
  }

  return (
    <div className="flex w-full max-w-sm flex-col gap-6 rounded-2xl border bg-card p-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-headline text-foreground">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          With a local development account.
        </p>
      </div>
      <form onSubmit={(event) => void submit(event)} noValidate>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="local-username">Username</FieldLabel>
            <Input
              id="local-username"
              name="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              disabled={pending}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="local-password">Password</FieldLabel>
            <Input
              id="local-password"
              name="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
              disabled={pending}
            />
          </Field>
          {error && (
            <Alert variant="destructive">
              <AlertCircleIcon />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button
            type="submit"
            disabled={pending || username === "" || password === ""}
          >
            {pending ? <Spinner className="text-current" /> : <LogInIcon />}
            {pending ? "Signing in…" : "Sign in"}
          </Button>
        </FieldGroup>
      </form>
    </div>
  )
}
