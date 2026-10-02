import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva } from "class-variance-authority"
import type { VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

// Pill-shaped buttons: 14px/500 labels, and a
// state layer (8% hover, 12% press) instead of ad-hoc hover colors — so one
// rule works on white surfaces, tonal containers and the dark call room.
const buttonVariants = cva(
  "group/button relative inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full border border-transparent font-heading text-sm font-medium whitespace-nowrap transition-[background-color,box-shadow,color,border-color] duration-150 ease-standard outline-none select-none before:pointer-events-none before:absolute before:inset-0 before:bg-current before:opacity-0 before:transition-opacity before:duration-150 hover:before:opacity-[0.08] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring active:before:opacity-[0.12] disabled:pointer-events-none aria-invalid:border-destructive data-disabled:pointer-events-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-[18px]",
  {
    variants: {
      variant: {
        /** Filled: the one primary action on a surface. */
        default:
          "bg-primary text-primary-foreground hover:shadow-e1 active:bg-primary-pressed disabled:bg-disabled disabled:text-disabled-foreground data-disabled:bg-disabled data-disabled:text-disabled-foreground",
        /** Outlined: a secondary action next to a filled one. */
        outline:
          "border-primary/70 bg-transparent text-primary disabled:border-disabled disabled:text-disabled-foreground dark:border-input data-disabled:text-disabled-foreground",
        /** Tonal: a quiet emphasis — blue container. */
        tonal:
          "bg-primary-container text-on-primary-container disabled:bg-disabled disabled:text-disabled-foreground",
        /** Neutral tonal: chips-like secondary actions on white. */
        secondary:
          "bg-secondary text-secondary-foreground disabled:bg-disabled disabled:text-disabled-foreground",
        /** Create / positive: the green tonal "new" action. */
        create:
          "bg-create-container text-on-create-container hover:shadow-e1 disabled:bg-disabled disabled:text-disabled-foreground",
        /** Text button: no container, blue label. */
        ghost:
          "bg-transparent text-primary disabled:text-disabled-foreground aria-expanded:before:opacity-[0.08]",
        /** Neutral text button for low-emphasis utility actions. */
        quiet:
          "bg-transparent text-muted-foreground hover:text-foreground disabled:text-disabled-foreground aria-expanded:before:opacity-[0.08]",
        /** Destructive, filled red — reserved for ending a session or
         *  irreversible actions. */
        destructive:
          "bg-end-call text-white hover:bg-end-call-hover disabled:bg-disabled disabled:text-disabled-foreground",
        link: "rounded-sm text-primary underline-offset-4 before:hidden hover:underline",
      },
      size: {
        default:
          "h-10 gap-2 px-6 has-data-[icon=inline-end]:pr-4 has-data-[icon=inline-start]:pl-4 has-[>svg:first-child]:pl-4",
        xs: "h-7 gap-1 px-3 text-xs [&_svg:not([class*='size-'])]:size-3.5",
        sm: "h-8 gap-1.5 px-4 text-[13px] has-[>svg:first-child]:pl-3 [&_svg:not([class*='size-'])]:size-4",
        lg: "h-12 gap-2 px-6 text-[15px] has-[>svg:first-child]:pl-5 [&_svg:not([class*='size-'])]:size-5",
        icon: "size-10 [&_svg:not([class*='size-'])]:size-5",
        "icon-xs": "size-7 [&_svg:not([class*='size-'])]:size-4",
        "icon-sm": "size-8 [&_svg:not([class*='size-'])]:size-[18px]",
        "icon-lg": "size-12 [&_svg:not([class*='size-'])]:size-6",
      },
    },
    compoundVariants: [
      // Text buttons hug their label more tightly than containers do.
      { variant: "ghost", size: "default", className: "px-3" },
      { variant: "quiet", size: "default", className: "px-3" },
      { variant: "ghost", size: "sm", className: "px-3" },
      { variant: "link", size: "default", className: "h-auto px-0" },
    ],
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
