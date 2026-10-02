import * as React from "react"
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router"
import { useQueryClient } from "@tanstack/react-query"
import { AuthView } from "@neondatabase/auth-ui"
import { TriangleAlertIcon } from "lucide-react"

import { AuthViewsProvider } from "@/components/auth-views"
import { LocalSignInForm } from "@/components/local-sign-in"
import { EmptyState } from "@/components/ui/empty-state"
import { PageShell } from "@/components/ui/page"
import {
  neonAuthConfigured,
  resetAuthMode,
  resolveAuthMode,
  safeRedirectPath,
  useAuthMode,
} from "@/lib/auth"
import { signOutLocal } from "@/lib/local-auth"
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
// in the neon mode, the dev login in the local one. Without sign-in (mode
// none): back to the home page.
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
  return (
    <PageShell center>
      {mode === "neon" && neonAuthConfigured ? (
        <AuthViewsProvider>
          {/* Always set: without the prop AuthView reads the raw query
              parameter itself and hands it to the OAuth flow unchecked. */}
          <AuthView path={pathname} redirectTo={redirectTo ?? "/"} />
        </AuthViewsProvider>
      ) : mode === "neon" ? (
        <EmptyState
          icon={<TriangleAlertIcon />}
          tone="warning"
          title="Sign-in is not set up in this build"
          description="This build has no VITE_NEON_AUTH_URL, but the API signs in with Neon Auth. Build the web app with the project's Neon Auth URL, or run the API with AUTH_MODE=local."
        />
      ) : mode === "local" && pathname === "sign-out" ? (
        <LocalSignOut />
      ) : mode === "local" ? (
        <LocalSignInForm redirectTo={redirectTo} />
      ) : null}
    </PageShell>
  )
}

/** /auth/sign-out with a local account: drop it, and home. */
function LocalSignOut() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  React.useEffect(() => {
    signOutLocal()
    queryClient.clear()
    void navigate({ to: "/", replace: true })
  }, [navigate, queryClient])
  return null
}
