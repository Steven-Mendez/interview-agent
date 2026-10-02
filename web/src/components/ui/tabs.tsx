import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"
import { cva } from "class-variance-authority"
import type { VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: TabsPrimitive.Root.Props) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      className={cn(
        "group/tabs flex gap-4 data-horizontal:flex-col",
        className
      )}
      {...props}
    />
  )
}

// "line": a label with a rounded 3px indicator under the active tab.
// "pill": a compact segmented variant for dense panels.
const tabsListVariants = cva(
  "group/tabs-list relative inline-flex w-fit items-center text-muted-foreground group-data-vertical/tabs:h-fit group-data-vertical/tabs:flex-col",
  {
    variants: {
      variant: {
        line: "gap-0 border-b group-data-horizontal/tabs:h-12 group-data-horizontal/tabs:w-full",
        pill: "gap-1 rounded-full bg-muted p-1 group-data-horizontal/tabs:h-10",
      },
    },
    defaultVariants: {
      variant: "line",
    },
  }
)

function TabsList({
  className,
  variant = "line",
  ...props
}: TabsPrimitive.List.Props & VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

function TabsTrigger({ className, ...props }: TabsPrimitive.Tab.Props) {
  return (
    <TabsPrimitive.Tab
      data-slot="tabs-trigger"
      className={cn(
        "relative inline-flex items-center justify-center gap-2 font-heading text-sm font-medium whitespace-nowrap text-muted-foreground transition-colors duration-150 ease-standard outline-none select-none hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-40 aria-disabled:pointer-events-none aria-disabled:opacity-40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-[18px]",
        // line
        "group-data-[variant=line]/tabs-list:h-full group-data-[variant=line]/tabs-list:px-4 group-data-[variant=line]/tabs-list:data-active:text-primary",
        "after:pointer-events-none after:absolute after:inset-x-3 after:bottom-0 after:h-[3px] after:rounded-t-full after:bg-primary after:opacity-0 after:transition-opacity after:duration-150 group-data-[variant=pill]/tabs-list:after:hidden group-data-[variant=line]/tabs-list:data-active:after:opacity-100",
        "before:pointer-events-none before:absolute before:inset-0 before:bg-current before:opacity-0 before:transition-opacity group-data-[variant=line]/tabs-list:before:rounded-t-md hover:before:opacity-[0.06]",
        // pill
        "group-data-[variant=pill]/tabs-list:h-full group-data-[variant=pill]/tabs-list:flex-1 group-data-[variant=pill]/tabs-list:rounded-full group-data-[variant=pill]/tabs-list:px-3 group-data-[variant=pill]/tabs-list:before:rounded-full group-data-[variant=pill]/tabs-list:data-active:bg-primary-container group-data-[variant=pill]/tabs-list:data-active:text-on-primary-container",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: TabsPrimitive.Panel.Props) {
  return (
    <TabsPrimitive.Panel
      data-slot="tabs-content"
      className={cn("flex-1 text-sm outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }
