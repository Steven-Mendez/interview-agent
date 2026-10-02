import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useRouter } from "@tanstack/react-router"
import { NeonAuthUIProvider } from "@neondatabase/auth-ui"

import { authClient, sameSitePath, useAuthMode } from "@/lib/auth"

/** Neon Auth's links, routed through the app's router instead of a full
 *  page load. */
function AuthLink({
  href,
  className,
  children,
}: {
  href: string
  className?: string
  children: React.ReactNode
}) {
  const router = useRouter()
  return (
    <a
      href={href}
      className={className}
      onClick={(event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return
        }
        event.preventDefault()
        void router.navigate({ href: sameSitePath(href) ?? "/" })
      }}
    >
      {children}
    </a>
  )
}

/** Neon Auth's views (sign-in, the OAuth callback, sign-out) wired to the
 *  router and the query cache. Rendered in the neon mode only; a build
 *  without Neon Auth has no client to give them: children only. */
export function AuthViewsProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const queryClient = useQueryClient()
  const mode = useAuthMode()
  if (mode !== "neon" || !authClient) return children
  // Every destination goes through the router, and only within this site:
  // the views also navigate to a `redirectTo` taken from the URL.
  const go = (href: string, replace: boolean) =>
    void router.navigate({ href: sameSitePath(href) ?? "/", replace })
  return (
    // `contents`: the provider's wrapper div must not change the layout.
    <NeonAuthUIProvider
      authClient={authClient}
      className="contents"
      social={{ providers: ["google", "github"] }}
      // Email and password are off in Neon Auth too: social sign-in only.
      credentials={false}
      navigate={(href) => go(href, false)}
      replace={(href) => go(href, true)}
      Link={AuthLink}
      // Whatever was cached belongs to the previous session.
      onSessionChange={() => void queryClient.invalidateQueries()}
    >
      {children}
    </NeonAuthUIProvider>
  )
}
