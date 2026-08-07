import { EventEmitter, once } from "node:events"
import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import process from "node:process"
import { deflateSync } from "node:zlib"
import { fileURLToPath } from "node:url"

import { app, BrowserWindow, dialog, ipcMain, nativeImage } from "electron"

const desktopRoot = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(desktopRoot, "..")
const rendererRoot = path.join(desktopRoot, "renderer")
const APP_ID = "Niooooo.MyAgent.Desktop"
const BACKGROUND = "#0b0e10"
const smokeTest = process.argv.includes("--smoke-test")
const IPC_METHODS = new Set([
  "app.snapshot",
  "app.shutdown",
  "session.new",
  "session.activate",
  "session.close",
  "session.select_model",
  "session.set_kind",
  "session.submit",
  "model.register",
  "model.delete",
  "model.refresh",
  "approval.decide",
])

app.setName("MyAgent")
app.setAppUserModelId(APP_ID)

class SidecarBridge extends EventEmitter {
  constructor() {
    super()
    this.process = undefined
    this.pending = new Map()
    this.sequence = 0
    this.buffer = ""
    this.exited = false
  }

  start() {
    if (this.process) return
    const executable = process.env.MYAGENT_PYTHON || (process.platform === "win32" ? "python.exe" : "python3")
    const pythonPath = [path.join(projectRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)
    const child = spawn(executable, ["-X", "utf8", "-u", "-m", "myagent.desktop_sidecar"], {
      cwd: projectRoot,
      env: {
        ...process.env,
        PYTHONPATH: pythonPath,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
      },
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    })
    this.process = child
    child.stdout.setEncoding("utf8")
    child.stderr.setEncoding("utf8")
    child.stdout.on("data", (chunk) => this.#read(chunk))
    child.stderr.on("data", (chunk) => console.error(`[MyAgent sidecar] ${chunk}`.trimEnd()))
    child.on("error", (error) => this.#fail(error))
    child.on("exit", (code, signal) => {
      this.exited = true
      const detail = signal ? `signal ${signal}` : `code ${code}`
      this.#fail(new Error(`Python sidecar exited with ${detail}`))
      this.emit("exit", { code, signal })
    })
  }

  request(method, params = {}) {
    if (!IPC_METHODS.has(method)) return Promise.reject(new Error(`Blocked desktop method: ${method}`))
    if (!this.process || this.exited || !this.process.stdin.writable) {
      return Promise.reject(new Error("Python sidecar is unavailable"))
    }
    const id = `desktop-${++this.sequence}`
    const message = `${JSON.stringify({ id, method, params })}\n`
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.process.stdin.write(message, "utf8", (error) => {
        if (!error) return
        this.pending.delete(id)
        reject(error)
      })
    })
  }

  async shutdown() {
    if (!this.process || this.exited) return
    try {
      await this.request("app.shutdown")
    } catch (error) {
      console.error("[MyAgent] sidecar shutdown request failed", error)
    }
    this.process.stdin.end()
    if (!this.exited) await once(this, "exit")
  }

  #read(chunk) {
    this.buffer += chunk
    while (true) {
      const end = this.buffer.indexOf("\n")
      if (end < 0) return
      const line = this.buffer.slice(0, end).trim()
      this.buffer = this.buffer.slice(end + 1)
      if (!line) continue
      let message
      try {
        message = JSON.parse(line)
      } catch {
        this.emit("event", { event: "sidecar-error", payload: { message: "Python sidecar returned invalid JSON" } })
        continue
      }
      if (message.event) {
        this.emit("event", message)
        continue
      }
      const pending = this.pending.get(message.id)
      if (!pending) continue
      this.pending.delete(message.id)
      if (message.error) {
        pending.reject(new Error(message.error.message || message.error.code || "Sidecar request failed"))
      } else {
        pending.resolve(message.result)
      }
    }
  }

  #fail(error) {
    for (const pending of this.pending.values()) pending.reject(error)
    this.pending.clear()
  }
}

const sidecar = new SidecarBridge()
let mainWindow
let quitAllowed = false
let shutdownStarted = false
let smokeExitCode = 0

function crc32(buffer) {
  let crc = 0xffffffff
  for (const byte of buffer) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1))
  }
  return (crc ^ 0xffffffff) >>> 0
}

function pngChunk(name, data) {
  const type = Buffer.from(name, "ascii")
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const checksum = Buffer.alloc(4)
  checksum.writeUInt32BE(crc32(Buffer.concat([type, data])))
  return Buffer.concat([length, type, data, checksum])
}

function createMyAgentIcon(size = 64) {
  const pixels = Buffer.alloc(size * size * 4)
  const setPixel = (x, y, color) => {
    if (x < 0 || x >= size || y < 0 || y >= size) return
    const offset = (y * size + x) * 4
    pixels[offset] = color[0]
    pixels[offset + 1] = color[1]
    pixels[offset + 2] = color[2]
    pixels[offset + 3] = color[3]
  }
  const cobalt = [39, 93, 206, 255]
  const light = [247, 249, 255, 255]
  const inset = 5
  const radius = 13
  for (let y = inset; y < size - inset; y += 1) {
    for (let x = inset; x < size - inset; x += 1) {
      const dx = Math.max(inset + radius - x, 0, x - (size - inset - radius - 1))
      const dy = Math.max(inset + radius - y, 0, y - (size - inset - radius - 1))
      if (dx * dx + dy * dy <= radius * radius) setPixel(x, y, cobalt)
    }
  }
  const segments = [
    [18, 45, 18, 20],
    [18, 20, 32, 36],
    [32, 36, 46, 20],
    [46, 20, 46, 45],
  ]
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      if (
        segments.some(([x1, y1, x2, y2]) => {
          const vx = x2 - x1
          const vy = y2 - y1
          const lengthSquared = vx * vx + vy * vy
          const t = Math.max(0, Math.min(1, ((x - x1) * vx + (y - y1) * vy) / lengthSquared))
          const px = x1 + t * vx
          const py = y1 + t * vy
          return (x - px) ** 2 + (y - py) ** 2 <= 9
        })
      ) {
        setPixel(x, y, light)
      }
    }
  }
  const raw = Buffer.alloc((size * 4 + 1) * size)
  for (let y = 0; y < size; y += 1) {
    const output = y * (size * 4 + 1)
    raw[output] = 0
    pixels.copy(raw, output + 1, y * size * 4, (y + 1) * size * 4)
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0)
  header.writeUInt32BE(size, 4)
  header[8] = 8
  header[9] = 6
  const png = Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(raw)),
    pngChunk("IEND", Buffer.alloc(0)),
  ])
  return nativeImage.createFromBuffer(png)
}

function createWindow() {
  const icon = createMyAgentIcon()
  mainWindow = new BrowserWindow({
    width: 960,
    height: 640,
    minWidth: 720,
    minHeight: 520,
    show: false,
    frame: false,
    backgroundColor: BACKGROUND,
    title: "MyAgent 工作台",
    icon,
    webPreferences: {
      preload: path.join(desktopRoot, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  mainWindow.loadFile(path.join(rendererRoot, "index.html"))
  mainWindow.once("ready-to-show", () => {
    if (!smokeTest) mainWindow?.show()
  })
  if (smokeTest) {
    mainWindow.webContents.once("did-finish-load", async () => {
      try {
        const result = await mainWindow.webContents.executeJavaScript(
          'window.myagent.request("app.snapshot")',
        )
        if (!Array.isArray(result?.state?.sessions)) throw new Error("Sidecar snapshot has no sessions")
        const bridgeShape = await mainWindow.webContents.executeJavaScript(
          '({ request: typeof window.myagent?.request, windowAction: typeof window.myagent?.windowAction })',
        )
        if (bridgeShape.request !== "function" || bridgeShape.windowAction !== "function") {
          throw new Error("Renderer preload bridge is incomplete")
        }
        await mainWindow.webContents.executeJavaScript(
          'document.querySelector("[data-window-action=maximize]").click()',
        )
        for (let attempt = 0; attempt < 20 && !mainWindow.isMaximized(); attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        if (!mainWindow.isMaximized()) throw new Error("Renderer maximize control did not reach the main window")
        mainWindow.unmaximize()
        let status = ""
        for (let attempt = 0; attempt < 20; attempt += 1) {
          status = await mainWindow.webContents.executeJavaScript(
            'document.querySelector("#session-status")?.textContent || ""',
          )
          if (!status.includes("正在连接桌面后端")) break
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        if (status.includes("正在连接桌面后端")) throw new Error("Renderer did not apply the sidecar snapshot")
        const renderedText = await mainWindow.webContents.executeJavaScript(
          '({ tab: document.querySelector(".tab-title")?.textContent || "", status: document.querySelector("#session-status")?.textContent || "" })',
        )
        if (!renderedText.tab.includes("对话") || JSON.stringify(renderedText).includes("�")) {
          throw new Error("Renderer received mojibake from the Python sidecar")
        }
        const busyState = JSON.parse(JSON.stringify(result.state))
        const busySession = busyState.sessions.find(
          (session) => session.id === busyState.activeSessionId,
        )
        busySession.state = "busy"
        busySession.canSend = false
        mainWindow.webContents.send("myagent:event", { event: "state", payload: busyState })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const busyControls = await mainWindow.webContents.executeJavaScript(
          '({ promptDisabled: document.querySelector("#prompt-input")?.disabled, sendDisabled: document.querySelector("#send-button")?.disabled })',
        )
        if (busyControls.promptDisabled || !busyControls.sendDisabled) {
          throw new Error("Busy turn must keep the prompt editable and lock only send")
        }
        mainWindow.webContents.send("myagent:event", {
          event: "stream-start",
          payload: { sessionId: busyState.activeSessionId },
        })
        mainWindow.webContents.send("myagent:event", {
          event: "stream-delta",
          payload: { sessionId: busyState.activeSessionId, delta: "流式输出" },
        })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const streamingText = await mainWindow.webContents.executeJavaScript(
          'document.querySelector(".message.streaming .message-text")?.textContent || ""',
        )
        if (streamingText !== "流式输出") {
          throw new Error("Renderer did not incrementally render sidecar output")
        }
        mainWindow.webContents.send("myagent:event", { event: "state", payload: result.state })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const streamWasCleared = await mainWindow.webContents.executeJavaScript(
          '!document.querySelector(".message.streaming")',
        )
        if (!streamWasCleared) throw new Error("Final state did not replace the streaming message")
        const modelDialog = await mainWindow.webContents.executeJavaScript(`(() => {
          document.querySelector("#add-model-button")?.click()
          const dialog = document.querySelector("#model-dialog")
          const input = document.querySelector("#model-base-url-input")
          const result = {
            open: Boolean(dialog?.open),
            inputType: input?.getAttribute("type") || "",
            label: input?.closest("label")?.textContent || "",
          }
          dialog?.close()
          return result
        })()`)
        if (!modelDialog.open || modelDialog.inputType !== "url" || !modelDialog.label.includes("Base URL")) {
          throw new Error("Model dialog does not expose the Base URL setting")
        }
        console.log("MyAgent Electron smoke test passed")
      } catch (error) {
        console.error("[MyAgent] Electron smoke test failed", error)
        smokeExitCode = 1
      } finally {
        app.quit()
      }
    })
  }
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }))
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url !== mainWindow?.webContents.getURL()) event.preventDefault()
  })
  mainWindow.on("maximize", () => mainWindow?.webContents.send("myagent:window-state", { maximized: true }))
  mainWindow.on("unmaximize", () => mainWindow?.webContents.send("myagent:window-state", { maximized: false }))
  mainWindow.on("closed", () => {
    mainWindow = undefined
  })
}

ipcMain.handle("myagent:request", (_event, method, params) => sidecar.request(method, params))
ipcMain.handle("myagent:pick-folder", async () => {
  const result = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory", "createDirectory"] })
  return result.canceled ? null : result.filePaths[0] || null
})
ipcMain.handle("myagent:platform", () => process.platform)
ipcMain.on("myagent:window", (_event, action) => {
  if (!mainWindow) return
  if (action === "minimize") mainWindow.minimize()
  if (action === "maximize") mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize()
  if (action === "close") mainWindow.close()
})

sidecar.on("event", (message) => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("myagent:event", message)
})

app.whenReady().then(() => {
  sidecar.start()
  createWindow()
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on("window-all-closed", () => app.quit())
app.on("before-quit", (event) => {
  if (quitAllowed) return
  event.preventDefault()
  if (shutdownStarted) return
  shutdownStarted = true
  for (const window of BrowserWindow.getAllWindows()) window.hide()
  sidecar.shutdown().finally(() => {
    quitAllowed = true
    if (smokeTest) app.exit(smokeExitCode)
    else app.quit()
  })
})

if (!existsSync(path.join(rendererRoot, "index.html"))) {
  console.error("[MyAgent] renderer assets are missing")
}
