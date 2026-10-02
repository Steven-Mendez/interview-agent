import { ExternalLinkIcon } from "lucide-react"

import { AnchorButton } from "@/components/ui/link-button"

/** Opens the interview's LangSmith trace and its voice sessions. The API only
 *  sends URLs while the traces exist, so there is never a dead link. */
export function TraceLink({
  url,
  voiceUrls = [],
}: {
  url?: string | null
  voiceUrls?: string[]
}) {
  if (!url && voiceUrls.length === 0) return null
  const links = [
    ...(url ? [{ href: url, label: "View trace in LangSmith" }] : []),
    ...voiceUrls.map((href, index) => ({
      href,
      label:
        voiceUrls.length > 1
          ? `Voice session ${index + 1} (audio)`
          : "Voice session (audio)",
    })),
  ]
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {links.map(({ href, label }) => (
          <AnchorButton
            key={href}
            variant="outline"
            size="sm"
            href={href}
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLinkIcon />
            {label}
          </AnchorButton>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        Includes the resume, offer, answers, evaluation and audio; deleted from
        LangSmith when local detail expires.
      </p>
    </div>
  )
}
