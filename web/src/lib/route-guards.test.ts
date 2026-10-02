/** @vitest-environment jsdom */
import { QueryClient } from "@tanstack/react-query"
import {
  createMemoryHistory,
  createRouter,
  isRedirect,
} from "@tanstack/react-router"
import { afterEach, describe, expect, it, vi } from "vitest"

import type * as Auth from "@/lib/auth"
import { safeRedirectPath } from "@/lib/auth"
import { requireSession } from "@/lib/route-guards"
import { routeTree } from "@/routeTree.gen"

const auth = vi.hoisted(() => ({
  client: null as { getSession: ReturnType<typeof vi.fn> } | null,
}))
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof Auth>()),
  get authClient() {
    return auth.client
  },
}))

function signedOut() {
  auth.client = { getSession: vi.fn().mockResolvedValue({ data: null }) }
  return auth.client
}

/** Where a thrown redirect would take the browser. */
function hrefOf(error: unknown): string {
  if (!isRedirect(error)) throw new Error("expected a redirect")
  const router = createRouter({
    routeTree,
    history: createMemoryHistory(),
    context: { queryClient: new QueryClient() },
  })
  return router.buildLocation(error.options).href
}

async function thrown(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise
  } catch (error) {
    return error
  }
  throw new Error("expected requireSession to throw")
}

afterEach(() => {
  auth.client = null
})

describe("requireSession", () => {
  it("sends a signed-out visitor to sign in, carrying where they were going", async () => {
    signedOut()
    const error = await thrown(
      requireSession({ href: "/interviews/abc?offset=20" })
    )
    expect(hrefOf(error)).toBe(
      "/auth/sign-in?redirectTo=%2Finterviews%2Fabc%3Foffset%3D20"
    )
  })

  it("lets a signed-in user through", async () => {
    auth.client = {
      getSession: vi.fn().mockResolvedValue({
        data: { session: { id: "s" }, user: { id: "u" } },
      }),
    }
    await expect(requireSession({ href: "/settings" })).resolves.toBe(undefined)
    expect(auth.client.getSession).toHaveBeenCalledTimes(1)
  })

  it("does nothing without sign-in", async () => {
    await expect(requireSession({ href: "/settings" })).resolves.toBe(undefined)
  })

  it("drops a destination that leaves the site", async () => {
    signedOut()
    const error = await thrown(requireSession({ href: "//evil.example/x" }))
    expect(hrefOf(error)).toBe("/auth/sign-in")
  })
})

describe("safeRedirectPath", () => {
  it.each([
    "https://evil.example/",
    "//evil.example/path",
    "/\\evil.example",
    "javascript:alert(1)",
    "interviews",
    "/\t/evil.example",
    "/auth/sign-out",
    "",
    42,
  ])("rejects %j", (target) => {
    expect(safeRedirectPath(target)).toBeUndefined()
  })

  it("keeps a path on this site with its query and hash", () => {
    expect(safeRedirectPath("/interviews?status=planned#top")).toBe(
      "/interviews?status=planned#top"
    )
  })
})
