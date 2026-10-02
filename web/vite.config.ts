import { defineConfig } from "vite"
import { devtools } from "@tanstack/devtools-vite"
import { tanstackStart } from "@tanstack/react-start/plugin/vite"
import viteReact from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

const config = defineConfig({
  resolve: { tsconfigPaths: true },
  plugins: [
    devtools({ consolePiping: { enabled: false } }),
    tailwindcss(),
    tanstackStart({
      spa: { enabled: true, prerender: { outputPath: "/index.html" } },
      // The privacy policy and the terms of service are also written out as
      // their own HTML files, so the text is there without JavaScript:
      // Google's OAuth verification fetches those URLs, and the catch-all
      // rewrite would otherwise answer with the empty shell. Their links are
      // not crawled: the pages they lead to need the browser and stay on the
      // shell.
      pages: [
        {
          path: "/privacy",
          prerender: {
            enabled: true,
            outputPath: "/privacy/index.html",
            crawlLinks: false,
          },
        },
        {
          path: "/terms",
          prerender: {
            enabled: true,
            outputPath: "/terms/index.html",
            crawlLinks: false,
          },
        },
      ],
    }),
    viteReact(),
  ],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
})

export default config
