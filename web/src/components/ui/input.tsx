import * as React from "react"
import { Input as InputPrimitive } from "@base-ui/react/input"

import { cn } from "@/lib/utils"

/** Outlined text field: 1px outline, 2px primary outline on focus, error
 *  outline when aria-invalid. Labels live outside (FieldLabel). */
export const fieldControlClasses =
  "w-full min-w-0 rounded-md border border-input/70 bg-card text-sm text-foreground transition-[border-color,box-shadow] duration-150 ease-standard outline-none placeholder:text-muted-foreground/80 hover:border-foreground/70 focus-visible:border-primary focus-visible:shadow-[inset_0_0_0_1px_var(--primary)] focus-visible:outline-none disabled:pointer-events-none disabled:cursor-not-allowed disabled:border-disabled disabled:bg-transparent disabled:text-disabled-foreground aria-invalid:border-destructive aria-invalid:focus-visible:shadow-[inset_0_0_0_1px_var(--destructive)] dark:bg-transparent"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      className={cn(
        fieldControlClasses,
        "h-11 px-3.5 file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground",
        className
      )}
      {...props}
    />
  )
}

export { Input }
