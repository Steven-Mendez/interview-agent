import * as React from "react"
import { cva } from "class-variance-authority"
import type { VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/** Inline banner on a tonal container. Say what happened, what is safe, and
 *  what to do next; put the action in <AlertAction>. */
const alertVariants = cva(
  "group/alert relative grid w-full gap-1 rounded-lg px-4 py-3 text-left text-sm has-data-[slot=alert-action]:items-center has-[>svg]:grid-cols-[auto_1fr] has-[>svg]:gap-x-3 *:[svg]:row-span-3 *:[svg]:mt-0.5 *:[svg:not([class*='size-'])]:size-5",
  {
    variants: {
      variant: {
        default: "bg-muted text-foreground *:[svg]:text-muted-foreground",
        info: "bg-primary-container/70 text-on-primary-container *:[svg]:text-primary",
        success:
          "bg-success-container text-on-success-container *:[svg]:text-success",
        warning:
          "bg-warning-container text-on-warning-container *:[svg]:text-warning",
        destructive:
          "bg-destructive-container text-on-destructive-container *:[svg]:text-destructive",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Alert({
  className,
  variant,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  )
}

function AlertTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn(
        "text-label group-has-[>svg]/alert:col-start-2 [&_a]:underline [&_a]:underline-offset-3",
        className
      )}
      {...props}
    />
  )
}

function AlertDescription({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn(
        "text-sm text-pretty opacity-90 group-has-[>svg]/alert:col-start-2 [&_a]:underline [&_a]:underline-offset-3 [&_p:not(:last-child)]:mb-2",
        className
      )}
      {...props}
    />
  )
}

function AlertAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-action"
      className={cn(
        "mt-1 flex flex-wrap gap-2 group-has-[>svg]/alert:col-start-2",
        className
      )}
      {...props}
    />
  )
}

export { Alert, AlertTitle, AlertDescription, AlertAction }
