import { existsSync, readFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const root = path.dirname(fileURLToPath(import.meta.url))
const required = [
  "main.mjs",
  "preload.cjs",
  "renderer/index.html",
  "renderer/app.js",
  "renderer/styles.css",
]
for (const relative of required) {
  if (!existsSync(path.join(root, relative))) throw new Error(`Missing desktop asset: ${relative}`)
}
const html = readFileSync(path.join(root, "renderer/index.html"), "utf8")
const app = readFileSync(path.join(root, "renderer/app.js"), "utf8")
const layout = readFileSync(path.join(root, "renderer/layout.js"), "utf8")
if (!/<section id="welcome-panel"[^>]*>\s*<canvas[^>]*><\/canvas>\s*<\/section>/s.test(html)) {
  throw new Error("Welcome panel must contain only the logo canvas")
}
if (!layout.includes("COMPACT_ENTER_WIDTH = 880") || !layout.includes("COMPACT_EXIT_WIDTH = 920")) {
  throw new Error("Responsive hysteresis constants are missing")
}
if (!app.includes("drawWelcomeLogo()")) throw new Error("Static logo draw is missing")
const electronBinary = path.join(root, "node_modules", "electron", "dist", process.platform === "win32" ? "electron.exe" : "electron")
if (!existsSync(electronBinary)) throw new Error("Electron is not installed; run npm install in desktop")
console.log("MyAgent Electron desktop check passed")
