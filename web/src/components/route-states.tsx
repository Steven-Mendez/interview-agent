import { useRouter } from "@tanstack/react-router"
import type { ErrorComponentProps } from "@tanstack/react-router"

import { Mascot } from "@/components/mascot"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { LinkButton } from "@/components/ui/link-button"
import { PageShell } from "@/components/ui/page"
import { Spinner } from "@/components/ui/spinner"
import { ApiError } from "@/lib/api"

/** A page that failed to load or render: what happened, that nothing was
 *  lost, and a way forward. */
export function RouteError({ error, reset }: ErrorComponentProps) {
  const router = useRouter()
  const notFound = error instanceof ApiError && error.status === 404
  // The router hands over whatever was thrown, which need not be an Error:
  // anything without a message gets the generic line.
  const message = error instanceof Error ? error.message : ""
  return (
    <PageShell center>
      <EmptyState
        illustration={
          <Mascot state={notFound ? "thinking" : "error"} className="w-36" />
        }
        title={notFound ? "Interview not found" : "This page couldn’t load"}
        description={
          notFound
            ? "It may have been removed, or the link is incomplete."
            : `${message || "Something went wrong."} Your interviews and results are safe — try again, or go back home.`
        }
        actions={
          <>
            <LinkButton to="/" variant="ghost">
              Back to Home
            </LinkButton>
            {!notFound && (
              <Button
                onClick={() => {
                  reset()
                  void router.invalidate()
                }}
              >
                Try again
              </Button>
            )}
          </>
        }
      />
    </PageShell>
  )
}

/** Shown only when a page takes a noticeable moment to load. */
export function RoutePending() {
  return (
    <PageShell center>
      <p
        role="status"
        className="flex items-center gap-3 text-sm text-muted-foreground"
      >
        <Spinner className="size-5" />
        Loading…
      </p>
    </PageShell>
  )
}
