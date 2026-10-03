import { createFileRoute } from "@tanstack/react-router"

import { NotFound } from "@/components/not-found"
import { pageHead } from "@/lib/head"

// Every path no other route claims. Vercel answers those with the SPA shell,
// which was prerendered for `/` with a pending page under the root. Matched
// by this route, an unknown path hydrates like any known one: the router
// shows that same pending page first, then this one. Left unmatched, the
// root rendered its not-found page straight away, the HTML did not match and
// React threw (error 418).
export const Route = createFileRoute("/$")({
  head: () => pageHead("Page not found"),
  component: NotFound,
})
