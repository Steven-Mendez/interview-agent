import { Mascot } from "@/components/mascot"
import { EmptyState } from "@/components/ui/empty-state"
import { LinkButton } from "@/components/ui/link-button"
import { PageShell } from "@/components/ui/page"

/** A path the app does not know, or a page that said it has nothing to show.
 *  Pure markup with no browser state, so the server and the client render
 *  the same thing. */
export function NotFound() {
  return (
    <PageShell center>
      <EmptyState
        illustration={<Mascot state="thinking" className="w-36" />}
        title="This page doesn’t exist"
        description="The link may be out of date, or the interview was removed."
        actions={<LinkButton to="/">Back to Home</LinkButton>}
      />
    </PageShell>
  )
}
