import { createFileRoute } from "@tanstack/react-router"
import { UploadPage } from "@/components/interview-setup"
import { pageHead } from "@/lib/head"
import { requireSession } from "@/lib/route-guards"

export const Route = createFileRoute("/new")({
  beforeLoad: ({ location }) => requireSession(location),
  head: () => pageHead("New interview"),
  component: UploadPage,
})
