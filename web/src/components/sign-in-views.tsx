import * as React from "react"
import { useNavigate } from "@tanstack/react-router"
import { useQueryClient } from "@tanstack/react-query"
import { AuthView } from "@neondatabase/auth-ui"
import { TriangleAlertIcon } from "lucide-react"

import { AuthViewsProvider } from "@/components/auth-views"
import { LocalSignInForm } from "@/components/local-sign-in"
import {
  SignInDivider,
  SignInHeading,
  SignInStatus,
  UnavailableProviders,
  providerButtonClasses,
  useSignInReactions,
} from "@/components/sign-in-page"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { signOutLocal } from "@/lib/local-auth"

// The card's contents on /auth/$pathname, one per way of signing in. Each
// goes inside a SignInPage.

const NEON_HEADINGS: Partial<
  Record<string, { title: string; description: string }>
> = {
  "sign-in": {
    title: "Welcome back",
    description: "Sign in with your Google or GitHub account.",
  },
  "sign-up": {
    title: "Create your account",
    description: "Sign up with your Google or GitHub account.",
  },
}

/** Neon Auth's own views, fitted into the card: its provider buttons, the
 *  OAuth callback and sign-out. */
export function NeonAuthViews({
  pathname,
  redirectTo,
}: {
  pathname: string
  redirectTo?: string
}) {
  const view = (
    <AuthViewsProvider>
      {/* Always set: without the prop AuthView reads the raw query
          parameter itself and hands it to the OAuth flow unchecked. */}
      <AuthView
        path={pathname}
        redirectTo={redirectTo ?? "/"}
        socialLayout="vertical"
        localization={{ SIGN_IN_WITH: "Continue with" }}
        cardHeader={
          NEON_HEADINGS[pathname] && (
            <SignInHeading {...NEON_HEADINGS[pathname]} />
          )
        }
        // The card is the page's: AuthView's own frame goes.
        className="max-w-none gap-6 rounded-none border-0 bg-transparent py-0 shadow-none"
        classNames={{
          header: "px-0",
          content: "gap-3 px-0",
          footer: "px-0",
          form: { providerButton: providerButtonClasses },
        }}
      />
    </AuthViewsProvider>
  )
  // Signing out and finishing a sign-in render only a spinner.
  if (pathname === "sign-out" || pathname === "callback") {
    return (
      <>
        <SignInHeading
          title={pathname === "sign-out" ? "See you soon" : "Almost there"}
        />
        <SignInStatus spinner={view}>
          {pathname === "sign-out" ? "Signing you out…" : "Signing you in…"}
        </SignInStatus>
      </>
    )
  }
  return view
}

/** The API signs in with Neon Auth, but this build cannot. */
export function NeonAuthMissing() {
  return (
    <>
      <SignInHeading title="Sign-in is not set up in this build" />
      <UnavailableProviders
        description={
          <Alert variant="warning">
            <TriangleAlertIcon />
            <AlertDescription>
              {
                "This build has no VITE_NEON_AUTH_URL, but the API signs in with Neon Auth. Build the web app with the project's Neon Auth URL, or run the API with AUTH_MODE=local."
              }
            </AlertDescription>
          </Alert>
        }
      />
    </>
  )
}

/** The dev login, with Google and GitHub shown for what is coming. */
export function LocalSignIn({ redirectTo }: { redirectTo?: string }) {
  const mascot = useSignInReactions()
  return (
    <>
      <SignInHeading
        title="Welcome back"
        description="Use a local development account."
      />
      <UnavailableProviders
        description={
          <p className="text-center text-xs text-pretty text-muted-foreground">
            {
              "Google and GitHub sign-in are coming soon — they need Neon Auth, which isn't set up in local development."
            }
          </p>
        }
      />
      <SignInDivider>or use a local account</SignInDivider>
      <LocalSignInForm
        redirectTo={redirectTo}
        onActivity={mascot.react}
        beforeRedirect={mascot.celebrate}
      />
    </>
  )
}

/** /auth/sign-out with a local account: drop it, and home. */
export function LocalSignOut() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  React.useEffect(() => {
    signOutLocal()
    queryClient.clear()
    void navigate({ to: "/", replace: true })
  }, [navigate, queryClient])
  return (
    <>
      <SignInHeading title="See you soon" />
      <SignInStatus>Signing you out…</SignInStatus>
    </>
  )
}
