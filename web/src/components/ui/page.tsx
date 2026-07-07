import * as React from "react"

import { cn } from "@/lib/utils"

const pageContainerVariants = {
  narrow: "max-w-md @lg/main:max-w-lg",
  reading: "max-w-2xl @2xl/main:max-w-3xl",
  wide: "max-w-2xl @2xl/main:max-w-5xl @5xl/main:max-w-6xl",
} as const

function PageContainer({
  variant = "reading",
  className,
  ...props
}: React.ComponentProps<"div"> & {
  variant?: keyof typeof pageContainerVariants
}) {
  return (
    <div
      data-slot="page-container"
      data-variant={variant}
      className={cn(
        "mx-auto w-full",
        pageContainerVariants[variant],
        className
      )}
      {...props}
    />
  )
}

function PageShell({
  center = false,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  // Vertically + horizontally centers short content in the full-height inset
  // instead of pinning it to the top (the void-below problem on idle/form pages).
  center?: boolean
}) {
  return (
    <div
      data-slot="page-shell"
      data-center={center}
      className={cn(
        "flex flex-1 flex-col gap-4 p-4 @lg/main:p-6",
        center && "items-center justify-center",
        className
      )}
      {...props}
    />
  )
}

export { PageContainer, PageShell }
