import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useRouter } from "@tanstack/react-router"

import { authClient, useAuth } from "@/lib/auth"
import { setErrorReportingUser } from "@/lib/error-reporting"

/** Follows the signed-in account wherever it changes — this tab's menu,
 *  another tab, the auth server ending the session. */
function SessionWatcher() {
  const { user, isPending } = useAuth()
  const queryClient = useQueryClient()
  const router = useRouter()
  const id = user?.id ?? null
  // undefined until the session is first known: loading it is no change.
  const lastId = React.useRef<string | null | undefined>(undefined)

  // Error reports name the signed-in account by its opaque id only.
  React.useEffect(() => setErrorReportingUser(id), [id])

  React.useEffect(() => {
    if (isPending) return
    const previous = lastId.current
    lastId.current = id
    if (previous === undefined || previous === id) return
    // Everything cached was fetched as someone else: drop it rather than
    // show it while it refetches. Then run the guards again, so a private
    // page sends a signed-out visitor to sign in.
    queryClient.clear()
    void router.invalidate()
  }, [id, isPending, queryClient, router])

  return null
}

/** What every page needs of the session. Neon Auth's own views and their
 *  provider stay with the /auth route (components/auth-views), so their
 *  code loads there only. */
export function AuthProvider({ children }: { children: React.ReactNode }) {
  return (
    <>
      {authClient && <SessionWatcher />}
      {children}
    </>
  )
}
