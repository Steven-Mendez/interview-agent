import { createFileRoute } from "@tanstack/react-router"
import { UploadPage } from "@/components/interview-setup"
import { pageHead } from "@/lib/head"

export const Route = createFileRoute("/new")({
  head: () => pageHead("New interview"),
  component: UploadPage,
})
