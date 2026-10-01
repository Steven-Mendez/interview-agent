// Prefixed logger: filter the devtools console with "[app]" to see only these.
export function log(...args: unknown[]): void {
  console.log("[app]", ...args)
}
