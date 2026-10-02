import * as React from "react"

import { cn } from "@/lib/utils"
import { fieldControlClasses } from "@/components/ui/input"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        fieldControlClasses,
        "flex field-sizing-content min-h-20 px-3.5 py-3 leading-relaxed",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
