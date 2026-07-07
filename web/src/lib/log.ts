// Prefixed logger: filter the devtools console with "[app]" to see only
// these — mirrors `frontend/app.js`'s `log()` so the two frontends stay
// diffable side by side (see Fase 4's transcription-parity QA).
export function log(...args: unknown[]): void {
  console.log("[app]", ...args)
}
