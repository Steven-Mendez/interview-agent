import * as React from "react"
import { Link, useRouter, useRouterState } from "@tanstack/react-router"
import { useQueryClient } from "@tanstack/react-query"
import {
  HistoryIcon,
  HomeIcon,
  LogInIcon,
  LogOutIcon,
  MenuIcon,
  PlusIcon,
  SettingsIcon,
} from "lucide-react"

import { BrandLink } from "@/components/brand"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { IconButton, IconLink } from "@/components/ui/icon-button"
import { LinkButton } from "@/components/ui/link-button"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { authEnabled, safeRedirectPath, signOut, useAuth } from "@/lib/auth"
import type { AuthUser } from "@/lib/auth"
import { cn } from "@/lib/utils"

// ---- Immersive mode ---------------------------------------------------------
//
// Preparation and the live interview are standalone rooms: no top bar, no
// navigation. The interview route decides that from the interview's state,
// so the shell cannot be chosen per route. Instead the shell's chrome is
// toggled around a children slot that never moves — switching between the
// room and the results must never remount the session underneath.

const ImmersiveContext = React.createContext<
  React.Dispatch<React.SetStateAction<number>>
>(() => {})

export function ShellProvider({ children }: { children: React.ReactNode }) {
  // A counter rather than a boolean: overlapping owners (one unmounting as
  // the next mounts) cannot switch the chrome back on by mistake.
  const [immersive, setImmersive] = React.useState(0)
  return (
    <ImmersiveContext.Provider value={setImmersive}>
      <AppShell immersive={immersive > 0}>{children}</AppShell>
    </ImmersiveContext.Provider>
  )
}

const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? React.useEffect : React.useLayoutEffect

/** Hide the application chrome while `active` — a layout effect, so the
 *  room never paints with the shell around it first. */
export function useImmersive(active = true) {
  const setImmersive = React.useContext(ImmersiveContext)
  useIsomorphicLayoutEffect(() => {
    if (!active) return
    setImmersive((n) => n + 1)
    return () => setImmersive((n) => n - 1)
  }, [active, setImmersive])
}

// ---- Navigation -------------------------------------------------------------

const NAV_ITEMS = [
  { to: "/", label: "Home", icon: HomeIcon, exact: true },
  { to: "/interviews", label: "History", icon: HistoryIcon, exact: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, exact: false },
] as const

function useIsActive() {
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  return (to: string, exact: boolean) =>
    exact ? pathname === to : pathname === to || pathname.startsWith(`${to}/`)
}

/** One destination as a full row: icon, label, a pill when active. Used by
 *  the wide sidebar and the narrow-window drawer. */
function NavRow({
  item,
  active,
  onClick,
}: {
  item: (typeof NAV_ITEMS)[number]
  active: boolean
  onClick?: () => void
}) {
  const Icon = item.icon
  return (
    <Link
      to={item.to}
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-12 items-center gap-4 rounded-full px-5 font-heading text-sm font-medium transition-colors duration-150 hover:bg-foreground/[0.06]",
        active
          ? "bg-primary-container text-on-primary-container hover:bg-primary-container"
          : "text-foreground"
      )}
    >
      <Icon
        className={cn("size-5", !active && "text-muted-foreground")}
        strokeWidth={active ? 2.25 : 2}
      />
      {item.label}
    </Link>
  )
}

/** Side navigation: full rows on wide windows, a compact icon rail on
 *  medium ones. Narrow windows use the drawer instead. */
function SideNav() {
  const isActive = useIsActive()
  return (
    <>
      <nav
        aria-label="Main"
        className="hidden w-64 shrink-0 flex-col pt-2 pr-3 pb-6 pl-2 lg:flex"
      >
        <ul className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavRow item={item} active={isActive(item.to, item.exact)} />
            </li>
          ))}
        </ul>
      </nav>
      <nav
        aria-label="Main"
        className="hidden w-20 shrink-0 flex-col items-center pt-2 pb-6 md:flex lg:hidden"
      >
        <ul className="flex flex-col items-center gap-3">
          {NAV_ITEMS.map((item) => {
            const active = isActive(item.to, item.exact)
            const Icon = item.icon
            return (
              <li key={item.to}>
                <Link
                  to={item.to}
                  aria-current={active ? "page" : undefined}
                  className="group/nav flex w-16 flex-col items-center gap-1 rounded-lg outline-offset-2"
                >
                  <span
                    className={cn(
                      "relative flex h-8 w-14 items-center justify-center overflow-hidden rounded-full transition-colors duration-200 ease-standard before:absolute before:inset-0 before:bg-current before:opacity-0 before:transition-opacity group-hover/nav:before:opacity-[0.08]",
                      active
                        ? "bg-primary-container text-on-primary-container"
                        : "text-muted-foreground"
                    )}
                  >
                    <Icon className="size-5" strokeWidth={active ? 2.25 : 2} />
                  </span>
                  <span
                    className={cn(
                      "font-heading text-xs",
                      active
                        ? "font-medium text-foreground"
                        : "text-muted-foreground"
                    )}
                  >
                    {item.label}
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
      </nav>
    </>
  )
}

/** The same destinations as a drawer, for narrow windows. */
function NavDrawer({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const isActive = useIsActive()
  const close = () => onOpenChange(false)
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="left" className="w-72 gap-2 px-3 py-4">
        <SheetHeader className="px-3 pt-1 pb-3">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <BrandLink />
        </SheetHeader>
        <nav aria-label="Main">
          <ul className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavRow
                  item={item}
                  active={isActive(item.to, item.exact)}
                  onClick={close}
                />
              </li>
            ))}
          </ul>
        </nav>
      </SheetContent>
    </Sheet>
  )
}

// ---- Account ------------------------------------------------------------------

function useMounted() {
  const [mounted, setMounted] = React.useState(false)
  React.useEffect(() => setMounted(true), [])
  return mounted
}

function initials(user: AuthUser): string {
  const source = user.name ?? user.email ?? ""
  const words = source.split(/[\s@._-]+/).filter(Boolean)
  return (
    words
      .slice(0, 2)
      .map((word) => word[0].toUpperCase())
      .join("") || "?"
  )
}

/** Sign in, or the signed-in account with its sign-out. Nothing in local
 *  mode, and nothing until mounted: the prerendered shell has no session. */
function AccountControls() {
  const mounted = useMounted()
  const { user, isPending } = useAuth()
  const href = useRouterState({ select: (s) => s.location.href })
  const router = useRouter()
  const queryClient = useQueryClient()

  if (!authEnabled || !mounted || isPending) return null

  if (!user) {
    return (
      <LinkButton
        to="/auth/$pathname"
        params={{ pathname: "sign-in" }}
        search={{ redirectTo: safeRedirectPath(href) }}
        variant="outline"
      >
        <LogInIcon />
        Sign in
      </LinkButton>
    )
  }

  const leave = async () => {
    // Off the private pages first, while the session still holds. From
    // signOut() on the app reports signed out — before Better Auth's own
    // session catches up — so the home page asks the API nothing; then
    // drop everything cached for this account.
    await router.navigate({ to: "/" })
    await signOut(user.id)
    queryClient.clear()
  }

  const label = user.name ?? user.email ?? "Your account"
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button variant="quiet" size="icon" aria-label={`Account: ${label}`}>
            <Avatar>
              {user.image && (
                <AvatarImage
                  src={user.image}
                  alt=""
                  referrerPolicy="no-referrer"
                />
              )}
              <AvatarFallback>{initials(user)}</AvatarFallback>
            </Avatar>
          </Button>
        }
      />
      <DropdownMenuContent align="end" className="max-w-72">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="flex flex-col">
            {user.name && (
              <span className="truncate font-medium">{user.name}</span>
            )}
            {user.email && (
              <span className="truncate text-xs text-muted-foreground">
                {user.email}
              </span>
            )}
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuItem onClick={() => void leave()}>
          <LogOutIcon />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Identity on the left; the create action, settings and the account on
 *  the right. */
function TopBar({ onMenu }: { onMenu: () => void }) {
  return (
    <header className="flex h-16 shrink-0 items-center gap-2 px-2 md:px-4">
      <IconButton
        label="Main menu"
        className="md:hidden"
        onClick={onMenu}
        tooltipSide="right"
      >
        <MenuIcon />
      </IconButton>
      <BrandLink className="md:pl-2" />
      <div className="ml-auto flex items-center gap-2">
        <LinkButton
          to="/new"
          variant="create"
          className="hidden sm:inline-flex"
        >
          <PlusIcon />
          New interview
        </LinkButton>
        <IconLink
          label="New interview"
          to="/new"
          variant="create"
          className="sm:hidden"
        >
          <PlusIcon />
        </IconLink>
        <IconLink label="Settings" to="/settings">
          <SettingsIcon />
        </IconLink>
        <AccountControls />
      </div>
    </header>
  )
}

function AppShell({
  immersive,
  children,
}: {
  immersive: boolean
  children: React.ReactNode
}) {
  const [drawerOpen, setDrawerOpen] = React.useState(false)
  // The children always sit at the same position in this tree whatever the
  // mode; only the chrome around them comes and goes.
  return (
    <div
      data-immersive={immersive}
      className="flex min-h-svh flex-col bg-background"
    >
      <a
        href="#main"
        className="sr-only z-50 rounded-full bg-primary px-4 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      {!immersive && <TopBar onMenu={() => setDrawerOpen(true)} />}
      <div className="flex min-h-0 flex-1">
        {!immersive && <SideNav />}
        <main
          id="main"
          className={cn(
            "@container/main flex min-w-0 flex-1 flex-col",
            !immersive &&
              "md:mr-4 md:mb-4 md:rounded-2xl md:bg-card md:shadow-[inset_0_0_0_1px_var(--border)] dark:md:bg-card"
          )}
        >
          {children}
        </main>
      </div>
      {!immersive && (
        <NavDrawer open={drawerOpen} onOpenChange={setDrawerOpen} />
      )}
    </div>
  )
}
