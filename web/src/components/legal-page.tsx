import * as React from "react"
import { Link } from "@tanstack/react-router"

import { LinkButton } from "@/components/ui/link-button"
import { useAuth, useAuthMode } from "@/lib/auth"

// Shared by the privacy policy and the terms of service: both are public
// pages a visitor reaches from Google's consent screen or the sign-in card.

/** Home for someone the app lets in (signed in, or no sign-in at all);
 *  the sign-in page for everyone else, the visitor these pages are for. */
export function LegalBackLink() {
  const mode = useAuthMode()
  const { user } = useAuth()
  if (mode === "none" || user !== null) {
    return (
      <LinkButton to="/" variant="outline">
        Back to Home
      </LinkButton>
    )
  }
  return (
    <LinkButton
      to="/auth/$pathname"
      params={{ pathname: "sign-in" }}
      variant="outline"
    >
      Back to sign in
    </LinkButton>
  )
}

/** The line at the bottom of each page that leads to the other one. */
export function LegalSeeAlso({
  to,
  children,
}: {
  to: "/privacy" | "/terms"
  children: React.ReactNode
}) {
  return (
    <p className="text-sm text-muted-foreground">
      See also:{" "}
      <Link
        to={to}
        className="rounded-sm text-primary underline underline-offset-4"
      >
        {children}
      </Link>
    </p>
  )
}
