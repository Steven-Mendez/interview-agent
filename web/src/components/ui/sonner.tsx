"use client"

import { Toaster as Sonner } from "sonner"
import type { ToasterProps } from "sonner"
import {
  CircleAlertIcon,
  CircleCheckIcon,
  InfoIcon,
  TriangleAlertIcon,
} from "lucide-react"

import { Spinner } from "@/components/ui/spinner"

/** Snackbar-style toasts: inverse surface, bottom-left, one line where
 *  possible, an optional text action. */
const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      position="bottom-left"
      className="toaster group"
      icons={{
        success: <CircleCheckIcon className="size-5" />,
        info: <InfoIcon className="size-5" />,
        warning: <TriangleAlertIcon className="size-5" />,
        error: <CircleAlertIcon className="size-5" />,
        loading: <Spinner className="size-5 text-inverse-foreground" />,
      }}
      style={
        {
          "--normal-bg": "var(--inverse-surface)",
          "--normal-text": "var(--inverse-foreground)",
          "--normal-border": "transparent",
          "--border-radius": "var(--radius-md)",
        } as React.CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: "shadow-e2! font-sans! text-sm! gap-3!",
          actionButton:
            "bg-transparent! text-primary-container! font-heading! font-medium!",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
