import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import {
  AlertCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ShieldCheckIcon,
  ShieldXIcon,
  UsersIcon,
} from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/ui/empty-state"
import { IconButton } from "@/components/ui/icon-button"
import { LinkButton } from "@/components/ui/link-button"
import { PageContainer, PageHeader, PageShell } from "@/components/ui/page"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  AUTH_PROVIDER_LABELS,
  formatDate,
  formatDateTime,
  interviewsUsedLabel,
} from "@/lib/accounts"
import { ApiError } from "@/lib/api"
import type { AdminUser } from "@/lib/api"
import { pageHead } from "@/lib/head"
import { ADMIN_USERS_PAGE_SIZE, adminUsersQueryOptions } from "@/lib/queries"
import { requireSession } from "@/lib/route-guards"
import { cn } from "@/lib/utils"

// The page lives in the URL, as on the history page. Who is an admin is the
// API's call: anyone else gets its 403, shown as such.
interface UsersSearch {
  offset?: number
}

export const Route = createFileRoute("/admin/users")({
  beforeLoad: ({ location }) => requireSession(location),
  head: () => pageHead("Users"),
  validateSearch: (search: Record<string, unknown>): UsersSearch => {
    const offset = Number(search.offset)
    return {
      offset:
        Number.isFinite(offset) && offset > 0 ? Math.floor(offset) : undefined,
    }
  },
  component: UsersPage,
})

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return error instanceof Error ? error.message : "Something went wrong."
}

function UsersPage() {
  const { offset = 0 } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const query = useQuery({
    ...adminUsersQueryOptions(offset),
    // Paging dims the page on screen instead of blanking it.
    placeholderData: keepPreviousData,
  })

  const forbidden =
    query.error instanceof ApiError && query.error.status === 403
  const page = query.data
  const total = page?.total ?? 0
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + ADMIN_USERS_PAGE_SIZE, total)
  const lastOffset =
    total > 0
      ? Math.floor((total - 1) / ADMIN_USERS_PAGE_SIZE) * ADMIN_USERS_PAGE_SIZE
      : 0
  const pastEnd =
    page !== undefined &&
    !query.isPlaceholderData &&
    page.items.length === 0 &&
    total > 0

  const goTo = (nextOffset: number) =>
    void navigate({
      search: { offset: nextOffset > 0 ? nextOffset : undefined },
    })

  if (forbidden) {
    return (
      <PageShell center>
        <EmptyState
          icon={<ShieldXIcon />}
          title="Admins only"
          description="The user list is for administrators. Your profile and your interviews are where they always are."
          actions={
            <LinkButton to="/profile" variant="outline">
              Your profile
            </LinkButton>
          }
        />
      </PageShell>
    )
  }

  return (
    <PageShell>
      <PageContainer variant="wide" className="flex flex-col gap-6">
        <PageHeader
          title="Users"
          description="Everyone who has signed in, most recently seen first."
          actions={
            total > 0 &&
            !pastEnd && (
              <span className="text-xs text-muted-foreground tabular-nums">
                {total} {total === 1 ? "user" : "users"}
              </span>
            )
          }
        />

        {query.isError && (
          <Alert variant="destructive">
            <AlertCircleIcon />
            <AlertDescription>{errorMessage(query.error)}</AlertDescription>
          </Alert>
        )}

        {query.isPending ? (
          <UsersSkeleton />
        ) : pastEnd ? (
          <EmptyState
            icon={<UsersIcon />}
            title="Nothing this far down"
            description={`The list ends at page ${Math.floor(lastOffset / ADMIN_USERS_PAGE_SIZE) + 1}.`}
            actions={
              <Button variant="outline" onClick={() => goTo(lastOffset)}>
                <ChevronLeftIcon />
                Go to the last page
              </Button>
            }
          />
        ) : page && page.items.length === 0 ? (
          <EmptyState
            icon={<UsersIcon />}
            title="No users yet"
            description="Accounts appear here once they have signed in."
          />
        ) : (
          page && (
            <div
              className={cn(
                "transition-opacity duration-150",
                query.isPlaceholderData && "opacity-60"
              )}
              aria-busy={query.isPlaceholderData}
            >
              {/* Wide: a quiet table. Narrow: the same rows as cards. */}
              <div className="hidden overflow-hidden rounded-xl border @4xl/main:block">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Email</TableHead>
                      <TableHead>Method</TableHead>
                      <TableHead>Role</TableHead>
                      <TableHead>Interviews</TableHead>
                      <TableHead>In history</TableHead>
                      <TableHead>Last interview</TableHead>
                      <TableHead>Member since</TableHead>
                      <TableHead>Last seen</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {page.items.map((user) => (
                      <UserTableRow key={user.id} user={user} />
                    ))}
                  </TableBody>
                </Table>
              </div>
              <ul className="flex flex-col gap-3 @4xl/main:hidden">
                {page.items.map((user) => (
                  <UserCard key={user.id} user={user} />
                ))}
              </ul>
            </div>
          )
        )}

        {total > ADMIN_USERS_PAGE_SIZE && !pastEnd && (
          <nav
            aria-label="Pagination"
            className="flex items-center justify-end gap-2"
          >
            <span className="text-sm text-muted-foreground tabular-nums">
              {from}–{to} of {total}
            </span>
            <IconButton
              label="Previous page"
              disabled={offset === 0}
              onClick={() => goTo(offset - ADMIN_USERS_PAGE_SIZE)}
            >
              <ChevronLeftIcon />
            </IconButton>
            <IconButton
              label="Next page"
              disabled={to >= total}
              onClick={() => goTo(offset + ADMIN_USERS_PAGE_SIZE)}
            >
              <ChevronRightIcon />
            </IconButton>
          </nav>
        )}
      </PageContainer>
    </PageShell>
  )
}

function RoleBadge({ user }: { user: AdminUser }) {
  return user.is_admin ? (
    <Badge>
      <ShieldCheckIcon aria-hidden />
      Admin
    </Badge>
  ) : (
    <Badge variant="secondary">Guest</Badge>
  )
}

function Moment({ iso, date = false }: { iso: string | null; date?: boolean }) {
  if (iso === null) return <>—</>
  return (
    <time dateTime={iso}>{date ? formatDate(iso) : formatDateTime(iso)}</time>
  )
}

function UserTableRow({ user }: { user: AdminUser }) {
  return (
    <TableRow>
      <TableCell className="max-w-48 truncate font-medium" title={user.id}>
        {user.name ?? user.id}
      </TableCell>
      <TableCell className="max-w-56 truncate text-muted-foreground">
        {user.email ?? "—"}
      </TableCell>
      <TableCell className="text-muted-foreground">
        {AUTH_PROVIDER_LABELS[user.auth_provider]}
      </TableCell>
      <TableCell>
        <RoleBadge user={user} />
      </TableCell>
      <TableCell className="text-muted-foreground tabular-nums">
        {interviewsUsedLabel(user.interviews_used, user.interview_limit)}
      </TableCell>
      <TableCell className="text-muted-foreground tabular-nums">
        {user.interviews_stored}
      </TableCell>
      <TableCell className="text-muted-foreground">
        <Moment iso={user.last_interview_at} />
      </TableCell>
      <TableCell className="text-muted-foreground">
        <Moment iso={user.created_at} date />
      </TableCell>
      <TableCell className="text-muted-foreground">
        <Moment iso={user.last_seen_at} />
      </TableCell>
    </TableRow>
  )
}

function UserCard({ user }: { user: AdminUser }) {
  return (
    <li className="flex flex-col gap-3 rounded-xl border bg-card p-4">
      <div className="flex items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-sm font-medium" title={user.id}>
            {user.name ?? user.id}
          </span>
          {user.email && (
            <span className="truncate text-xs text-muted-foreground">
              {user.email}
            </span>
          )}
        </div>
        <RoleBadge user={user} />
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
        <span>{AUTH_PROVIDER_LABELS[user.auth_provider]}</span>
        <span className="tabular-nums">
          {interviewsUsedLabel(user.interviews_used, user.interview_limit)}{" "}
          interviews
        </span>
        <span className="tabular-nums">
          {user.interviews_stored} in history
        </span>
        {user.last_seen_at && (
          <span>
            Last seen <Moment iso={user.last_seen_at} />
          </span>
        )}
      </div>
    </li>
  )
}

function UsersSkeleton() {
  return (
    <div className="flex flex-col divide-y overflow-hidden rounded-xl border">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="flex items-center gap-6 px-4 py-4">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-56 max-w-full" />
            <Skeleton className="h-3 w-40" />
          </div>
          <Skeleton className="h-6 w-16 rounded-full" />
          <Skeleton className="hidden h-4 w-24 @3xl/main:block" />
        </div>
      ))}
    </div>
  )
}
