import { ExternalLinkIcon } from "lucide-react"

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
    <p className="text-xs text-muted-foreground">
      {links.map(({ href, label }) => (
        <a
          key={href}
          href={href}
          target="_blank"
          rel="noreferrer"
          className="mr-3 inline-flex items-center gap-1 underline underline-offset-2"
        >
          <ExternalLinkIcon className="size-3" />
          {label}
        </a>
      ))}
      — includes the resume, offer, answers, evaluation and audio; deleted from
      LangSmith when local detail expires.
    </p>
  )
}
