import { QueryClient } from "@tanstack/react-query"
import { createRouter as createTanStackRouter } from "@tanstack/react-router"
import { setupRouterSsrQueryIntegration } from "@tanstack/react-router-ssr-query"

import { RouteError, RoutePending } from "@/components/route-states"
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
    defaultPendingComponent: RoutePending,
    defaultOnCatch: (error) => captureRouteError(error),
  })

  // Exposes `queryClient` on the router context (used by route loaders via
  // `context.queryClient.ensureQueryData`) and wraps the app in
  // `QueryClientProvider` — the documented TanStack Start + Query convention.
  setupRouterSsrQueryIntegration({ router, queryClient })

  return router
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>
  }
}
