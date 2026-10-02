import { createFileRoute, redirect } from "@tanstack/react-router"

import { SignInPage, SignInStatus } from "@/components/sign-in-page"
import {
  LocalSignIn,
  LocalSignOut,
  NeonAuthMissing,
  NeonAuthViews,
} from "@/components/sign-in-views"
import {
  neonAuthConfigured,
  resetAuthMode,
  resolveAuthMode,
  safeRedirectPath,
  useAuthMode,
} from "@/lib/auth"
import { pageHead } from "@/lib/head"

interface AuthSearch {
  /** Where to land once signed in; only a path on this site is honored. */
  redirectTo?: string
}

const TITLES: Partial<Record<string, string>> = {
  "sign-in": "Sign in",
  "sign-up": "Sign up",
  "sign-out": "Sign out",
}

// Sign-in, sign-up, the OAuth callback and sign-out: Neon Auth's own views
// in the neon mode, the dev login in the local one — each on a full-screen
// page of its own, without the app's chrome. Without sign-in (mode none):
// back to the home page.
export const Route = createFileRoute("/auth/$pathname")({
  validateSearch: (search: Record<string, unknown>): AuthSearch => ({
    redirectTo: safeRedirectPath(search.redirectTo),
  }),
  beforeLoad: async ({ params, search }) => {
    // Never on the server, which cannot know the mode.
    if (typeof window === "undefined") return
    // Ask the API afresh: a mode remembered from before it restarted could
    // send a refused session back and forth between here and home.
    resetAuthMode()
    const mode = await resolveAuthMode()
    if (mode === "none") throw redirect({ to: "/" })
    if (
      mode === "local" &&
      params.pathname !== "sign-in" &&
      params.pathname !== "sign-out"
    ) {
      throw redirect({
        to: "/auth/$pathname",
        params: { pathname: "sign-in" },
        search: { redirectTo: search.redirectTo },
      })
    }
  },
  head: ({ params }) => pageHead(TITLES[params.pathname] ?? "Account"),
  component: AuthPage,
})

function AuthPage() {
  const { pathname } = Route.useParams()
  const { redirectTo } = Route.useSearch()
  const mode = useAuthMode()
  const leaving = pathname === "sign-out"
  return (
    // Waving goodbye on the way out.
    <SignInPage resting={leaving ? "greeting" : "idle"}>
      {mode === "neon" && neonAuthConfigured ? (
        <NeonAuthViews pathname={pathname} redirectTo={redirectTo} />
      ) : mode === "neon" ? (
        <NeonAuthMissing />
      ) : mode === "local" && leaving ? (
        <LocalSignOut />
      ) : mode === "local" ? (
        <LocalSignIn redirectTo={redirectTo} />
      ) : (
        <SignInStatus>Loading…</SignInStatus>
      )}
    </SignInPage>
  )
}
