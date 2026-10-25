# Web app

The interview agent's frontend: TanStack Start (built as a SPA), React, TypeScript and shadcn/ui. `pnpm install && pnpm dev` serves it on http://localhost:3000 and proxies `/api` to the API on :8000. The build variables are in `.env.example`; local setup and the Vercel deploy are in the [root README](../README.md).

## Adding components

To add components to your app, run the following command:

```bash
pnpm shadcn add button
```

This will place the ui components in `src/components/ui`.

## Using components

To use the components in your app, import them as follows:

```tsx
import { Button } from "@/components/ui/button";
```
