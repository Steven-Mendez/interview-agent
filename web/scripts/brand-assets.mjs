// Generates the browser and app icons in public/ from the Interviewer Agent
// head mark (the same shapes as MascotHead in src/components/mascot.tsx).
//
//   node scripts/brand-assets.mjs
//
// Needs Google Chrome (rasterizes the SVGs pixel-exact at each size) and
// ImageMagick (`magick`, packs favicon.ico).

import { execFileSync } from "node:child_process"
import { mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"

const PUBLIC = resolve(import.meta.dirname, "../public")
const CHROME =
  process.env.CHROME ??
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

const C = {
  accent: "#0B57D0",
  soft: "#A8C7FA",
  container: "#D3E3FD",
  face: "#0F1A36",
  eye: "#EAF1FE",
  shell: "#FFFFFF",
  outline: "#C6D5EE",
}

// The head on its 48 grid. On the blue app tile the accents turn light so
// the antenna and ears stay visible.
function head48({ onBlue = false } = {}) {
  const accent = onBlue ? C.container : C.accent
  return `
  <circle cx="24" cy="8" r="4" fill="${accent}"/>
  <rect x="22.75" y="10" width="2.5" height="6" rx="1.25" fill="${onBlue ? C.container : C.face}"/>
  <rect x="2.5" y="23" width="6" height="13" rx="3" fill="${accent}"/>
  <rect x="39.5" y="23" width="6" height="13" rx="3" fill="${accent}"/>
  <rect x="6.5" y="15" width="35" height="28" rx="12" fill="${C.shell}"/>
  <rect x="11" y="19.5" width="26" height="19" rx="8" fill="${C.face}"/>
  <circle cx="18.75" cy="29" r="3" fill="${C.eye}"/>
  <circle cx="29.25" cy="29" r="3" fill="${C.eye}"/>`
}

// Favicon: a blue-traced white head on a transparent ground, drawn on a 32
// grid so it reads on light and dark browser chrome alike.
const favicon = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <circle cx="16" cy="4.25" r="3" fill="${C.accent}"/>
  <rect x="15" y="6" width="2" height="3.5" fill="${C.face}"/>
  <rect x="0.75" y="14" width="3.5" height="9" rx="1.75" fill="${C.accent}"/>
  <rect x="27.75" y="14" width="3.5" height="9" rx="1.75" fill="${C.accent}"/>
  <rect x="3.75" y="8.75" width="24.5" height="20.5" rx="7.5" fill="${C.shell}" stroke="${C.accent}" stroke-width="2"/>
  <rect x="7" y="12" width="18" height="14" rx="5" fill="${C.face}"/>
  <circle cx="12.5" cy="19" r="2.4" fill="${C.eye}"/>
  <circle cx="19.5" cy="19" r="2.4" fill="${C.eye}"/>
</svg>
`

// The 16 px cut: pixel-aligned, no ears, square eyes — at this size every
// element is one or two pixels.
const favicon16 = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <circle cx="8" cy="2" r="1.75" fill="${C.accent}"/>
  <rect x="7.5" y="3.5" width="1" height="1.5" fill="${C.face}"/>
  <rect x="1.5" y="5" width="13" height="10" rx="3.5" fill="${C.shell}" stroke="${C.accent}" stroke-width="1"/>
  <rect x="3.5" y="7" width="9" height="6" rx="2" fill="${C.face}"/>
  <rect x="5" y="9" width="2" height="2" rx="0.6" fill="${C.eye}"/>
  <rect x="9" y="9" width="2" height="2" rx="0.6" fill="${C.eye}"/>
</svg>
`

/** The app tile: the head on brand blue. `maskable` fills the square and
 *  keeps the head inside the central safe circle (40% radius). */
function appIcon({ maskable }) {
  const scale = maskable ? 5.4 : 6.6
  const bg = maskable
    ? `<rect width="512" height="512" fill="${C.accent}"/>`
    : `<rect width="512" height="512" rx="116" fill="${C.accent}"/>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  ${bg}
  <g transform="translate(256 262) scale(${scale}) translate(-24 -25.5)">${head48({ onBlue: true })}
  </g>
</svg>
`
}

// iOS rounds the corners itself: a full square.
const appleTouch = appIcon({ maskable: false }).replace('rx="116" ', "")

const work = mkdtempSync(join(tmpdir(), "brand-"))

function rasterize(svg, size, out) {
  const svgPath = join(work, `${size}-${Math.random()}.svg`)
  const htmlPath = `${svgPath}.html`
  writeFileSync(svgPath, svg)
  writeFileSync(
    htmlPath,
    `<!doctype html><style>html,body{margin:0;background:transparent}img{display:block}</style><img src="${svgPath}" width="${size}" height="${size}">`
  )
  execFileSync(
    CHROME,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--force-device-scale-factor=1",
      "--default-background-color=00000000",
      `--window-size=${size},${size}`,
      `--screenshot=${out}`,
      `file://${htmlPath}`,
    ],
    { stdio: "ignore" }
  )
  // Chrome's window has a minimum size; crop to the icon.
  execFileSync("magick", [out, "-crop", `${size}x${size}+0+0`, "+repage", out])
}

writeFileSync(join(PUBLIC, "favicon.svg"), favicon)

const ico = [16, 32, 48].map((size) => {
  const out = join(work, `favicon-${size}.png`)
  rasterize(size === 16 ? favicon16 : favicon, size, out)
  return out
})
execFileSync("magick", [...ico, join(PUBLIC, "favicon.ico")])

rasterize(appleTouch, 180, join(PUBLIC, "apple-touch-icon.png"))
for (const size of [192, 512]) {
  rasterize(
    appIcon({ maskable: false }),
    size,
    join(PUBLIC, `icon-${size}.png`)
  )
  rasterize(
    appIcon({ maskable: true }),
    size,
    join(PUBLIC, `icon-maskable-${size}.png`)
  )
}

rmSync(work, { recursive: true, force: true })
console.log("Brand assets written to", PUBLIC)
