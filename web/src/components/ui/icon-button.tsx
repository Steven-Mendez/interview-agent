import * as React from "react"
import type { VariantProps } from "class-variance-authority"

import type { buttonVariants } from "@/components/ui/button"
import { Button } from "@/components/ui/button"
import { LinkButton } from "@/components/ui/link-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

type IconButtonProps = Omit<React.ComponentProps<typeof Button>, "size"> & {
  /** Accessible name AND tooltip text — an icon-only control always has both. */
  label: string
  /** Optional keyboard shortcut shown in the tooltip, e.g. "⌘ D". */
  shortcut?: string
  size?: "icon" | "icon-xs" | "icon-sm" | "icon-lg"
  tooltipSide?: React.ComponentProps<typeof TooltipContent>["side"]
  variant?: VariantProps<typeof buttonVariants>["variant"]
}

/** A circular icon-only button with its label as tooltip and aria-label. */
function IconButton({
  label,
  shortcut,
  size = "icon",
  variant = "quiet",
  tooltipSide,
  className,
  ...props
}: IconButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            aria-label={label}
            size={size}
            variant={variant}
            className={cn(className)}
            {...props}
          />
        }
      />
      <TooltipContent side={tooltipSide}>
        {label}
        {shortcut && <kbd>{shortcut}</kbd>}
      </TooltipContent>
    </Tooltip>
  )
}

/** IconButton's link twin: navigates, so it stays a link for assistive tech. */
function IconLink({
  label,
  size = "icon",
  variant = "quiet",
  tooltipSide,
  className,
  ...props
}: Omit<React.ComponentProps<typeof LinkButton>, "size" | "variant"> & {
  label: string
  size?: "icon" | "icon-xs" | "icon-sm" | "icon-lg"
  variant?: VariantProps<typeof buttonVariants>["variant"]
  tooltipSide?: React.ComponentProps<typeof TooltipContent>["side"]
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <LinkButton
            aria-label={label}
            size={size}
            variant={variant}
            className={className}
            {...props}
          />
        }
      />
      <TooltipContent side={tooltipSide}>{label}</TooltipContent>
    </Tooltip>
  )
}

export { IconButton, IconLink }
