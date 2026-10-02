import * as React from "react"

import { cn } from "@/lib/utils"

const pageContainerVariants = {
  narrow: "max-w-xl",
  reading: "max-w-3xl",
  wide: "max-w-6xl",
} as const

/** Horizontal measure for page content. */
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

/** The scrolling content area of a management page. */
function PageShell({
  center = false,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  // Centers short content (empty, processing and error states) in the
  // available height instead of pinning it to the top.
  center?: boolean
}) {
  return (
    <div
      data-slot="page-shell"
      data-center={center}
      className={cn(
        "flex flex-1 flex-col gap-6 px-4 py-6 @lg/main:px-8 @lg/main:py-8",
        center && "items-center justify-center",
        className
      )}
      {...props}
    />
  )
}

/** Page title row: a calm title, an optional one-line description, and the
 *  page's actions on the right. */
function PageHeader({
  title,
  description,
  actions,
  leading,
  className,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  leading?: React.ReactNode
  className?: string
}) {
  return (
    <header
      data-slot="page-header"
      className={cn(
        "flex flex-col gap-4 @2xl/main:flex-row @2xl/main:items-end @2xl/main:justify-between",
        className
      )}
    >
      <div className="flex min-w-0 items-start gap-4">
        {leading}
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="text-headline text-foreground">{title}</h1>
          {description && (
            <p className="max-w-2xl text-sm text-muted-foreground">
              {description}
            </p>
          )}
        </div>
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </header>
  )
}

/** An open section: a quiet heading on the page background, no card. */
function Section({
  title,
  description,
  actions,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"section">, "title"> & {
  title?: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
}) {
  return (
    <section
      data-slot="section"
      className={cn("flex flex-col gap-3", className)}
      {...props}
    >
      {(title || actions) && (
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div className="flex flex-col gap-0.5">
            {title && <h2 className="text-title text-foreground">{title}</h2>}
            {description && (
              <p className="text-sm text-muted-foreground">{description}</p>
            )}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  )
}

export { PageContainer, PageShell, PageHeader, Section }
