import {
  HeadContent,
  Scripts,
  createRootRouteWithContext,
} from "@tanstack/react-router"
import { TanStackRouterDevtoolsPanel } from "@tanstack/react-router-devtools"
import { TanStackDevtools } from "@tanstack/react-devtools"
import type { QueryClient } from "@tanstack/react-query"

import { AuthProvider } from "@/components/auth-provider"
import { NotFound } from "@/components/not-found"
import { ShellProvider } from "@/components/app-shell"
import { ThemeProvider } from "@/components/theme-provider"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"

import { PRODUCT_NAME } from "@/lib/head"

import appCss from "../styles.css?url"

interface RouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: PRODUCT_NAME },
      {
        name: "description",
        content:
          "Practice job interviews by voice with an AI interviewer that plans around your resume and the role, then scores your answers with evidence.",
      },
      { name: "application-name", content: PRODUCT_NAME },
      { name: "apple-mobile-web-app-title", content: PRODUCT_NAME },
    ],
    links: [
      { rel: "stylesheet", href: appCss },
      { rel: "icon", href: "/favicon.ico", sizes: "32x32" },
      { rel: "icon", href: "/favicon.svg", type: "image/svg+xml" },
      { rel: "apple-touch-icon", href: "/apple-touch-icon.png" },
      { rel: "manifest", href: "/manifest.json" },
    ],
  }),
  // Also the root's boundary for a notFound() thrown while rendering a page.
  notFoundComponent: NotFound,
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <HeadContent />
        {/* Two theme colors, one per scheme: head() merges meta by name and
            would keep only one. */}
        <meta
          name="theme-color"
          content="#f8f9fa"
          media="(prefers-color-scheme: light)"
        />
        <meta
          name="theme-color"
          content="#202124"
          media="(prefers-color-scheme: dark)"
        />
      </head>
      <body>
        <ThemeProvider>
          <AuthProvider>
            <TooltipProvider>
              <ShellProvider>{children}</ShellProvider>
            </TooltipProvider>
          </AuthProvider>
          <Toaster />
          <TanStackDevtools
            config={{
              position: "bottom-right",
            }}
            plugins={[
              {
                name: "Tanstack Router",
                render: <TanStackRouterDevtoolsPanel />,
              },
            ]}
          />
        </ThemeProvider>
        <Scripts />
      </body>
    </html>
  )
}
