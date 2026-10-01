import { createFileRoute } from "@tanstack/react-router"
import { UploadPage } from "@/components/interview-setup"

export const Route = createFileRoute("/")({ component: UploadPage })
