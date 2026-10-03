import { QueryClient } from "@tanstack/react-query"
import { createRouter as createTanStackRouter } from "@tanstack/react-router"
import { setupRouterSsrQueryIntegration } from "@tanstack/react-router-ssr-query"

import { NotFound } from "@/components/not-found"
import { RouteError, RoutePending } from "@/components/route-states"
import { setUnauthorizedHandler } from "@/lib/api"
import { safeRedirectPath } from "@/lib/auth"
import { captureRouteError, initErrorReporting } from "@/lib/error-reporting"
import { routeTree } from "./routeTree.gen"

export function getRouter() {
  // Browser only and only with a DSN: a no-op while the shell is prerendered.
  initErrorReporting()
  const queryClient = new QueryClient()

  const router = createTanStackRouter({
    routeTree,
    context: { queryClient },

    scrollRestoration: true,
    defaultPreload: "intent",
    defaultPreloadStaleTime: 0,
    defaultErrorComponent: RouteError,
    // Unknown paths have their own route ($.tsx); this covers a notFound()
    // thrown by a page that has no not-found component of its own.
    defaultNotFoundComponent: NotFound,
    defaultPendingComponent: RoutePending,
    defaultOnCatch: (error) => captureRouteError(error),
  })

  // Exposes `queryClient` on the router context (used by route loaders via
  // `context.queryClient.ensureQueryData`) and wraps the app in
  // `QueryClientProvider` — the documented TanStack Start + Query convention.
  setupRouterSsrQueryIntegration({ router, queryClient })

  // The API refused the session (expired, revoked) even with a token fresh
  // from the auth server: sign in again, then come back to the page that
  // asked. Never called while an interview is in progress (api.ts).
  setUnauthorizedHandler(() => {
    const { pathname, href } = router.state.location
    if (pathname.startsWith("/auth/")) return
    void router.navigate({
      to: "/auth/$pathname",
      params: { pathname: "sign-in" },
      search: { redirectTo: safeRedirectPath(href) },
    })
  })

  return router
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>
  }
}
