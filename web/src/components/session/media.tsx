import * as React from "react"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"

/** The local camera feed, mirrored the way every video app mirrors it. */
export function SelfView({
  stream,
  className,
}: {
  stream: MediaStream
  className?: string
}) {
  const ref = React.useRef<HTMLVideoElement>(null)
  React.useEffect(() => {
    // srcObject is a property, not an attribute — it cannot be set in JSX.
    if (ref.current) ref.current.srcObject = stream
  }, [stream])
  return (
    <video
      ref={ref}
      autoPlay
      muted
      playsInline
      className={cn("scale-x-[-1] bg-stage object-cover", className)}
    />
  )
}

/** A device's name without the USB vendor id Chrome appends to some
 *  ("FaceTime HD Camera (467C:1317)"). */
function cleanLabel(label: string) {
  return label.replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)$/i, "")
}

/** Chrome lists the system default (and on Windows the communications
 *  device) as extra "Default - <name>" entries. Like a call app, the list
 *  shows each real device once and tags the one the system defaults to. */
export function devicesForPicker(devices: MediaDeviceInfo[]) {
  const pseudo = new Set(["default", "communications"])
  const fallback = devices.find((d) => d.deviceId === "default")
  const defaultName = fallback
    ? cleanLabel(fallback.label.replace(/^[^-]+-\s*/, ""))
    : null
  return devices
    .filter((d) => !pseudo.has(d.deviceId))
    .map((d) => ({
      id: d.deviceId,
      name: cleanLabel(d.label),
      systemDefault:
        defaultName !== null && cleanLabel(d.label) === defaultName,
    }))
}

/** The arrow of a call app's device pill: a small filled triangle. */
function DropArrow({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden className={cn("size-5", className)}>
      <path d="M7 10l5 5 5-5z" fill="currentColor" />
    </svg>
  )
}

/** A device picker under the preview: a pill with the device in use that
 *  opens upward over the video — each device once, the system default
 *  tagged, the current one checked — with an optional test action below. */
export function DeviceMenu({
  kind,
  icon: Icon,
  devices,
  value,
  onChange,
  fallbackLabel,
  action,
  footer,
  disabled,
  className,
}: {
  kind: string
  icon: React.ComponentType<{ className?: string }>
  devices: MediaDeviceInfo[]
  /** "" or "default" mean the system default. */
  value: string
  onChange: (deviceId: string) => void
  fallbackLabel: string
  /** A row under the list, e.g. "Test speakers". */
  action?: {
    label: string
    icon: React.ComponentType<{ className?: string }>
    onSelect: () => void
  }
  /** Static content under the list, e.g. the microphone's live level. */
  footer?: React.ReactNode
  disabled?: boolean
  className?: string
}) {
  const options = devicesForPicker(devices)
  const followsSystem = value === "" || value === "default"
  const selected = followsSystem
    ? options.find((o) => o.systemDefault)
    : options.find((o) => o.id === value)
  const current =
    (selected?.name || (selected ? fallbackLabel : null)) ??
    (followsSystem ? "System default" : fallbackLabel)
  const ActionIcon = action?.icon
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        disabled={disabled || options.length === 0}
        render={
          <button
            type="button"
            aria-label={`${kind}: ${current}`}
            className={cn(
              "group/device relative inline-flex h-8 max-w-full min-w-0 items-center gap-1.5 overflow-hidden rounded-full border border-border bg-transparent pr-1.5 pl-3 text-left font-heading text-sm font-medium text-muted-foreground transition-colors duration-150 outline-none before:absolute before:inset-0 before:bg-current before:opacity-0 before:transition-opacity hover:before:opacity-[0.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:text-disabled-foreground aria-expanded:border-transparent aria-expanded:bg-foreground/10",
              className
            )}
          >
            <Icon className="size-[18px] shrink-0 text-foreground" />
            <span className="min-w-0 flex-1 truncate">{current}</span>
            <DropArrow className="shrink-0 transition-transform duration-150 group-aria-expanded/device:rotate-180" />
          </button>
        }
      />
      <DropdownMenuContent
        side="top"
        sideOffset={8}
        aria-label={kind}
        className="w-max max-w-sm min-w-(--anchor-width) rounded-xl p-1"
      >
        <DropdownMenuRadioGroup
          value={selected?.id ?? ""}
          onValueChange={(next) => onChange(String(next))}
        >
          {options.map((option) => (
            <DropdownMenuRadioItem
              key={option.id}
              value={option.id}
              className="min-h-10 rounded-xl py-2 pr-3 pl-[3.25rem] data-checked:bg-foreground/10 [&_[data-slot=dropdown-menu-radio-item-indicator]]:left-4"
            >
              <span className="flex min-w-0 flex-col">
                <span className="truncate font-heading font-medium">
                  {option.name || fallbackLabel}
                </span>
                {option.systemDefault && (
                  <span className="truncate font-heading">System default</span>
                )}
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {(action || footer) && <DropdownMenuSeparator className="mx-0 my-1" />}
        {action && ActionIcon && (
          <DropdownMenuItem
            onClick={action.onSelect}
            className="min-h-10 gap-4 rounded-xl px-4 font-heading font-medium [&_svg]:text-foreground"
          >
            <ActionIcon />
            {action.label}
          </DropdownMenuItem>
        )}
        {footer}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** A short two-note chime through the chosen output — what "Test
 *  speakers" plays. `sinkId` "" is the system default. */
export async function playTestChime(sinkId: string) {
  const context = new AudioContext()
  try {
    const destination = context.createMediaStreamDestination()
    const gain = context.createGain()
    gain.connect(destination)
    const start = context.currentTime + 0.05
    ;[523.25, 783.99].forEach((frequency, i) => {
      const osc = context.createOscillator()
      osc.type = "sine"
      osc.frequency.value = frequency
      osc.connect(gain)
      const at = start + i * 0.22
      gain.gain.setValueAtTime(0.0001, at)
      gain.gain.exponentialRampToValueAtTime(0.25, at + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.4)
      osc.start(at)
      osc.stop(at + 0.42)
    })
    const audio = new Audio()
    audio.srcObject = destination.stream
    if (sinkId && "setSinkId" in audio) await audio.setSinkId(sinkId)
    await audio.play()
    await new Promise((resolve) => setTimeout(resolve, 900))
    audio.pause()
  } finally {
    void context.close()
  }
}

/** A non-interactive pill for a device the browser will not let us change. */
export function DevicePill({
  icon: Icon,
  label,
  description,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  description: string
}) {
  return (
    <span
      title={description}
      className="inline-flex h-8 min-w-0 items-center gap-1.5 rounded-full border border-border px-3 font-heading text-sm font-medium text-muted-foreground"
    >
      <Icon className="size-[18px] shrink-0" />
      <span className="truncate">{label}</span>
      <span className="sr-only">— {description}</span>
    </span>
  )
}

/** Circular participant avatar for a tile with no video. */
export function TileAvatar({
  children,
  speaking = false,
  tone = "primary",
  className,
}: {
  children: React.ReactNode
  speaking?: boolean
  tone?: "primary" | "self"
  className?: string
}) {
  return (
    <span
      className={cn(
        "flex aspect-square w-[min(28%,8rem)] min-w-14 items-center justify-center rounded-full font-heading text-[clamp(1.5rem,4vw,3rem)] font-normal transition-shadow duration-200 [&_svg]:size-[45%]",
        tone === "primary" && "bg-primary text-primary-foreground",
        tone === "self" && "bg-avatar-self text-white",
        speaking && "speaking-halo",
        className
      )}
    >
      {children}
    </span>
  )
}

/** A video tile: dark rounded surface, centered content, a name label at the
 *  bottom-left and optional badges top-right. Speaking draws an outline. */
export function ParticipantTile({
  name,
  speaking = false,
  badges,
  nameAdornment,
  tint,
  className,
  style,
  children,
  ...props
}: Omit<React.ComponentProps<"div">, "children"> & {
  name: string
  /** The participant's color, washed softly across an empty tile. */
  tint?: string
  speaking?: boolean
  badges?: React.ReactNode
  nameAdornment?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div
      data-speaking={speaking}
      style={
        tint
          ? {
              backgroundImage: `radial-gradient(circle at 50% 45%, color-mix(in srgb, ${tint} 75%, var(--tile)) 0%, color-mix(in srgb, ${tint} 30%, var(--card)) 85%)`,
              ...style,
            }
          : style
      }
      className={cn(
        "relative isolate flex items-center justify-center overflow-hidden rounded-xl bg-tile text-white",
        "after:pointer-events-none after:absolute after:inset-0 after:rounded-xl after:transition-shadow after:duration-200 data-[speaking=true]:after:shadow-[inset_0_0_0_3px_var(--primary)]",
        className
      )}
      {...props}
    >
      {children}
      {badges && (
        <div className="absolute top-2.5 right-2.5 z-10 flex items-center gap-1.5">
          {badges}
        </div>
      )}
      <div className="absolute bottom-2.5 left-3 z-10 flex max-w-[calc(100%-1.5rem)] items-center gap-1.5 text-sm font-medium [text-shadow:0_1px_2px_rgb(0_0_0/0.6)]">
        {nameAdornment}
        <span className="truncate">{name}</span>
      </div>
    </div>
  )
}

/** A small round badge for a tile corner (muted mic, speaking bars). */
export function TileBadge({
  className,
  label,
  children,
}: {
  className?: string
  label: string
  children: React.ReactNode
}) {
  return (
    <span
      role="img"
      aria-label={label}
      className={cn(
        "flex size-7 items-center justify-center rounded-full bg-black/50 text-white [&_svg]:size-4",
        className
      )}
    >
      {children}
    </span>
  )
}
