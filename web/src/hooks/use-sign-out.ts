import { useQueryClient } from "@tanstack/react-query"
import { useRouter } from "@tanstack/react-router"

import { signOut } from "@/lib/auth"

/** Signs the account out from wherever it is shown — Neon Auth or a local
 *  account alike. */
export function useSignOut(): (userId: string) => Promise<void> {
  const router = useRouter()
  const queryClient = useQueryClient()
  return async (userId) => {
    // Off the private pages first, while the session still holds. From
    // signOut() on the app reports signed out — before Better Auth's own
    // session catches up — so the home page asks the API nothing; then
    // drop everything cached for this account.
    await router.navigate({ to: "/" })
    await signOut(userId)
    queryClient.clear()
  }
}
