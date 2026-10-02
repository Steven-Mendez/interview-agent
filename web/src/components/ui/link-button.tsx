import * as React from "react"
import { Link } from "@tanstack/react-router"
import type { LinkComponent } from "@tanstack/react-router"
import type { VariantProps } from "class-variance-authority"

import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

type Variants = VariantProps<typeof buttonVariants>
type ButtonAnchor = (
  props: React.ComponentProps<"a"> & Variants
) => React.ReactNode

/** A router link styled as a button, with typed `to`/`params`/`search`.
 *  Links keep link semantics — a Button rendered as an anchor would announce
 *  itself as a button. */
const LinkButton = (({
  className,
  variant = "default",
  size = "default",
  ...props
}: { className?: string } & Variants & Record<string, unknown>) => (
  <Link
    data-slot="button"
    className={cn(buttonVariants({ variant, size }), className)}
    {...(props as React.ComponentProps<typeof Link>)}
  />
)) as LinkComponent<ButtonAnchor>

/** An external <a> styled as a button. */
function AnchorButton({
  className,
  variant = "default",
  size = "default",
  ...props
}: React.ComponentProps<"a"> & Variants) {
  return (
    <a
      data-slot="button"
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  )
}

export { AnchorButton, LinkButton }
