import { createFileRoute, redirect } from "@tanstack/react-router"
import { AuthView } from "@neondatabase/auth-ui"

import { AuthViewsProvider } from "@/components/auth-views"
import { PageShell } from "@/components/ui/page"
import { authEnabled, safeRedirectPath } from "@/lib/auth"
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

// Sign-in, sign-up, the OAuth callback and sign-out, all drawn by Neon
// Auth's own views. Local mode has no sign-in: back to the home page.
export const Route = createFileRoute("/auth/$pathname")({
  validateSearch: (search: Record<string, unknown>): AuthSearch => ({
    redirectTo: safeRedirectPath(search.redirectTo),
  }),
  beforeLoad: () => {
    if (!authEnabled) throw redirect({ to: "/" })
  },
  head: ({ params }) => pageHead(TITLES[params.pathname] ?? "Account"),
  component: AuthPage,
})

function AuthPage() {
  const { pathname } = Route.useParams()
  const { redirectTo } = Route.useSearch()
  return (
    <PageShell center>
      <AuthViewsProvider>
        {/* Always set: without the prop AuthView reads the raw query
            parameter itself and hands it to the OAuth flow unchecked. */}
        <AuthView path={pathname} redirectTo={redirectTo ?? "/"} />
      </AuthViewsProvider>
    </PageShell>
  )
}
