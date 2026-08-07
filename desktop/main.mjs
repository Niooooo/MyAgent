import { EventEmitter, once } from "node:events"
import { spawn } from "node:child_process"
import { existsSync, writeFileSync } from "node:fs"
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
const smokeScreenshotPath = process.env.MYAGENT_SMOKE_SCREENSHOT || ""
const smokeSettingsScreenshotPath = process.env.MYAGENT_SMOKE_SETTINGS_SCREENSHOT || ""
const smokeIconPath = process.env.MYAGENT_SMOKE_ICON || ""
const IPC_METHODS = new Set([
  "app.snapshot",
  "app.shutdown",
  "session.new",
  "session.activate",
  "session.close",
  "session.delete",
  "session.rename",
  "session.select_model",
  "session.configure_agent_model",
  "session.submit",
  "model.register",
  "model.delete",
  "model.refresh",
  "settings.update",
  "approval.decide",
])

app.setName("MyAgent")
app.setAppUserModelId(APP_ID)
if (smokeTest) app.disableHardwareAcceleration()

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
    [35, 33, 46, 40],
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
  if (smokeTest && smokeIconPath) writeFileSync(smokeIconPath, icon.toPNG())
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
      backgroundThrottling: !smokeTest,
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
        if (!renderedText.tab || JSON.stringify(renderedText).includes("�")) {
          throw new Error("Renderer received mojibake from the Python sidecar")
        }
        if (smokeSettingsScreenshotPath) {
          await mainWindow.webContents.executeJavaScript(
            'document.querySelector("#settings-button").click()',
          )
          await new Promise((resolve) => setTimeout(resolve, 50))
          writeFileSync(
            smokeSettingsScreenshotPath,
            (await mainWindow.webContents.capturePage()).toPNG(),
          )
          await mainWindow.webContents.executeJavaScript(
            'document.querySelector("#settings-dialog").close()',
          )
        }
        const settingsAndAgentConfig = await mainWindow.webContents.executeJavaScript(`(() => {
          document.querySelector("#settings-button")?.click()
          const settingsDialog = document.querySelector("#settings-dialog")
          const settingsOpened = settingsDialog.open
          const settingsBounds = settingsDialog.getBoundingClientRect()
          const settingsFormBounds = settingsDialog.querySelector("form").getBoundingClientRect()
          const settingsFits =
            settingsDialog.scrollWidth <= settingsDialog.clientWidth &&
            settingsDialog.scrollHeight <= settingsDialog.clientHeight &&
            settingsFormBounds.right <= settingsBounds.right + 1 &&
            settingsFormBounds.bottom <= settingsBounds.bottom + 1
          const settingValues = [...settingsDialog.querySelectorAll('input[type="number"]')]
            .map((input) => input.value)
          settingsDialog.close()
          const details = document.querySelector("#agent-config")
          const select = document.querySelector("#main-agent-model-selector")
          details.open = true
          select.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }))
          const internalStayedOpen = details.open
          let changeFired = false
          select.addEventListener("change", () => { changeFired = true }, { once: true })
          select.dispatchEvent(new Event("change", { bubbles: true }))
          const selectStayedOpen = details.open
          document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }))
          const externalClosed = !details.open
          details.open = true
          document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))
          const escapeClosed = !details.open
          details.open = true
          window.dispatchEvent(new Event("blur"))
          const blurClosed = !details.open
          details.open = true
          window.dispatchEvent(new Event("resize"))
          const resizeClosed = !details.open
          return {
            settingsOpened,
            settingsFits,
            settingValues,
            internalStayedOpen,
            selectStayedOpen,
            changeFired,
            externalClosed,
            escapeClosed,
            blurClosed,
            resizeClosed,
          }
        })()`)
        if (
          !settingsAndAgentConfig.settingsOpened ||
          !settingsAndAgentConfig.settingsFits ||
          settingsAndAgentConfig.settingValues.length !== 5 ||
          settingsAndAgentConfig.settingValues.some((value) => value === "") ||
          !settingsAndAgentConfig.internalStayedOpen ||
          !settingsAndAgentConfig.selectStayedOpen ||
          !settingsAndAgentConfig.changeFired ||
          !settingsAndAgentConfig.externalClosed ||
          !settingsAndAgentConfig.escapeClosed ||
          !settingsAndAgentConfig.blurClosed ||
          !settingsAndAgentConfig.resizeClosed
        ) {
          throw new Error("Settings dialog or Agent configuration dismissal behavior failed")
        }
        const renameStarted = await mainWindow.webContents.executeJavaScript(`(() => {
          const title = document.querySelector(".tab-title")
          const rect = title?.getBoundingClientRect()
          title?.dispatchEvent(new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: rect?.left || 20,
            clientY: rect?.bottom || 20,
          }))
          const menu = document.querySelector("#tab-context-menu")
          const menuItem = document.querySelector("#rename-tab-menu-item")
          if (menu?.hidden || menuItem?.textContent !== "修改名称") return false
          menuItem.click()
          const input = document.querySelector(".tab-title-input")
          if (!input) return false
          input.value = "冒烟会话"
          input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }))
          return true
        })()`)
        if (!renameStarted) throw new Error("Conversation rename context menu did not open the editor")
        let renamedTitle = ""
        for (let attempt = 0; attempt < 20; attempt += 1) {
          renamedTitle = await mainWindow.webContents.executeJavaScript(
            'document.querySelector(".tab-title")?.textContent || ""',
          )
          if (renamedTitle === "冒烟会话") break
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        if (renamedTitle !== "冒烟会话") throw new Error("Conversation title rename did not persist")
        const secondConversation = await sidecar.request("session.new", {})
        mainWindow.webContents.send("myagent:event", {
          event: "state",
          payload: secondConversation.state,
        })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const historyOpenedTab = await mainWindow.webContents.executeJavaScript(`(() => {
          const inactiveClose = document.querySelector(".tab:not(.active) .tab-close")
          const historyItemsBefore = document.querySelectorAll(".history-item").length
          inactiveClose?.click()
          const tabsAfterClose = document.querySelectorAll(".tab").length
          const historyItem = [...document.querySelectorAll(".history-item")]
            .find((item) => item.querySelector(".history-item-title")?.textContent === "冒烟会话")
          historyItem?.click()
          return { historyItemsBefore, tabsAfterClose, foundHistory: Boolean(historyItem) }
        })()`)
        let reopenedTitle = ""
        let reopenedTabs = 0
        for (let attempt = 0; attempt < 20; attempt += 1) {
          const reopened = await mainWindow.webContents.executeJavaScript(`({
            title: document.querySelector(".tab.active .tab-title")?.textContent || "",
            tabs: document.querySelectorAll(".tab").length,
          })`)
          reopenedTitle = reopened.title
          reopenedTabs = reopened.tabs
          if (reopenedTitle === "冒烟会话" && reopenedTabs === 2) break
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        if (
          historyOpenedTab.historyItemsBefore !== 2 ||
          historyOpenedTab.tabsAfterClose !== 1 ||
          !historyOpenedTab.foundHistory ||
          reopenedTitle !== "冒烟会话" ||
          reopenedTabs !== 2
        ) {
          throw new Error("Workspace history did not reopen the conversation in a tab")
        }
        await mainWindow.webContents.executeJavaScript(`(() => {
          const second = [...document.querySelectorAll(".tab-title")]
            .find((title) => title.textContent !== "冒烟会话")
          second?.click()
        })()`)
        for (let attempt = 0; attempt < 20; attempt += 1) {
          const activeTitle = await mainWindow.webContents.executeJavaScript(
            'document.querySelector(".tab.active .tab-title")?.textContent || ""',
          )
          if (activeTitle && activeTitle !== "冒烟会话") break
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        const sidebarCollapse = await mainWindow.webContents.executeJavaScript(`(() => {
          const sidebar = document.querySelector("#history-sidebar")
          const toggle = document.querySelector("#history-sidebar-toggle")
          const expandedWidth = sidebar.getBoundingClientRect().width
          toggle.click()
          const collapsed = document.body.classList.contains("sidebar-collapsed")
          const collapsedWidth = sidebar.getBoundingClientRect().width
          const collapsedAria = toggle.getAttribute("aria-expanded")
          toggle.click()
          return {
            collapsed,
            collapsedAria,
            collapsedWidth,
            expandedWidth,
            restored: !document.body.classList.contains("sidebar-collapsed"),
          }
        })()`)
        if (
          !sidebarCollapse.collapsed ||
          sidebarCollapse.collapsedAria !== "false" ||
          sidebarCollapse.collapsedWidth >= sidebarCollapse.expandedWidth ||
          !sidebarCollapse.restored
        ) {
          throw new Error("History sidebar collapse and restore behavior failed")
        }
        if (smokeScreenshotPath) {
          await mainWindow.webContents.executeJavaScript(
            'document.querySelector("#agent-config").open = true',
          )
          await new Promise((resolve) => setTimeout(resolve, 50))
          writeFileSync(smokeScreenshotPath, (await mainWindow.webContents.capturePage()).toPNG())
          await mainWindow.webContents.executeJavaScript(
            'document.querySelector("#agent-config").open = false',
          )
        }
        const menuBehavior = await mainWindow.webContents.executeJavaScript(`(() => {
          const open = () => {
            const title = document.querySelector(".tab.active .tab-title")
            const rect = title?.getBoundingClientRect()
            title?.dispatchEvent(new MouseEvent("contextmenu", {
              bubbles: true,
              cancelable: true,
              clientX: rect?.left || 20,
              clientY: rect?.bottom || 20,
            }))
          }
          const menu = document.querySelector("#tab-context-menu")
          const deleteItem = document.querySelector("#delete-tab-menu-item")
          open()
          const hasActions = !menu.hidden &&
            document.querySelector("#rename-tab-menu-item")?.textContent === "修改名称" &&
            deleteItem?.textContent === "删除对话"
          document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }))
          const externalClosed = menu.hidden
          open()
          document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))
          const escapeClosed = menu.hidden
          open()
          window.dispatchEvent(new Event("blur"))
          const blurClosed = menu.hidden
          open()
          window.dispatchEvent(new Event("resize"))
          const resizeClosed = menu.hidden
          open()
          window.confirm = () => false
          deleteItem.click()
          return { hasActions, externalClosed, escapeClosed, blurClosed, resizeClosed }
        })()`)
        if (Object.values(menuBehavior).some((value) => !value)) {
          throw new Error("Conversation context menu actions or dismissal behavior failed")
        }
        await new Promise((resolve) => setTimeout(resolve, 25))
        const afterCancel = await sidecar.request("app.snapshot")
        if (afterCancel.state.sessions.length !== 2) {
          throw new Error("Cancelled conversation deletion changed persisted state")
        }
        await mainWindow.webContents.executeJavaScript(`(() => {
          const title = document.querySelector(".tab.active .tab-title")
          title?.dispatchEvent(new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: 20,
            clientY: 20,
          }))
          window.confirm = () => true
          document.querySelector("#delete-tab-menu-item")?.click()
        })()`)
        let afterDelete
        for (let attempt = 0; attempt < 20; attempt += 1) {
          afterDelete = await sidecar.request("app.snapshot")
          if (afterDelete.state.sessions.length === 1) break
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        const remainingTitle = afterDelete?.state?.sessions?.[0]?.title
        if (afterDelete?.state?.sessions?.length !== 1 || remainingTitle !== "冒烟会话") {
          throw new Error("Confirmed conversation deletion removed the wrong tab")
        }
        await mainWindow.webContents.executeJavaScript(
          'document.querySelector(".tab-close")?.click()',
        )
        await new Promise((resolve) => setTimeout(resolve, 25))
        const toast = await mainWindow.webContents.executeJavaScript(
          '({ hidden: document.querySelector("#error-toast")?.hidden, text: document.querySelector("#error-toast")?.textContent || "" })',
        )
        if (toast.hidden || !toast.text.includes("至少保留一个打开的对话标签") || toast.text.includes("remote method")) {
          throw new Error("Desktop request errors are not shown as a clean toast")
        }
        await new Promise((resolve) => setTimeout(resolve, 5200))
        const toastDismissed = await mainWindow.webContents.executeJavaScript(
          'document.querySelector("#error-toast")?.hidden === true && document.querySelector("#error-toast")?.textContent === ""',
        )
        if (!toastDismissed) throw new Error("Desktop request error toast did not dismiss after five seconds")
        const busyState = JSON.parse(JSON.stringify(result.state))
        const busySession = busyState.sessions.find(
          (session) => session.id === busyState.activeSessionId,
        )
        busyState.models = [
          { name: "smoke-model", baseUrl: null, createdAt: "2026-01-01T00:00:00Z" },
          { name: "child-model", baseUrl: null, createdAt: "2026-01-01T00:00:00Z" },
        ]
        busySession.state = "busy"
        busySession.model = "smoke-model"
        busySession.agentModels = { main: "smoke-model", sub: "child-model" }
        busySession.canSend = false
        busySession.canConfigureAgents = false
        mainWindow.webContents.send("myagent:event", { event: "state", payload: busyState })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const busyControls = await mainWindow.webContents.executeJavaScript(
          '({ promptDisabled: document.querySelector("#prompt-input")?.disabled, sendDisabled: document.querySelector("#send-button")?.disabled, mainModelDisabled: document.querySelector("#main-agent-model-selector")?.disabled, subModelDisabled: document.querySelector("#sub-agent-model-selector")?.disabled })',
        )
        if (
          busyControls.promptDisabled ||
          !busyControls.sendDisabled ||
          !busyControls.mainModelDisabled ||
          !busyControls.subModelDisabled
        ) {
          throw new Error("Busy turn must keep the prompt editable and lock Agent configuration")
        }
        mainWindow.webContents.send("myagent:event", {
          event: "stream-start",
          payload: { sessionId: busyState.activeSessionId },
        })
        mainWindow.webContents.send("myagent:event", {
          event: "stream-delta",
          payload: { sessionId: busyState.activeSessionId, delta: "**流式输出**" },
        })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const streamingMarkdown = await mainWindow.webContents.executeJavaScript(
          'document.querySelector(".message.streaming strong")?.textContent || ""',
        )
        if (streamingMarkdown !== "流式输出") {
          throw new Error("Renderer did not incrementally render Markdown output")
        }
        const completedState = JSON.parse(JSON.stringify(busyState))
        const completedSession = completedState.sessions.find(
          (session) => session.id === completedState.activeSessionId,
        )
        completedSession.state = "idle"
        completedSession.canSend = true
        completedSession.canConfigureAgents = true
        completedSession.messages = [
          { speaker: "你", text: "**用户原文**" },
          {
            speaker: "MyAgent",
            text: "**模型粗体**\n\n```python\nprint('ok')\n```\n\n<img src=x onerror=alert(1)>",
          },
        ]
        mainWindow.webContents.send("myagent:event", { event: "state", payload: completedState })
        await new Promise((resolve) => setTimeout(resolve, 25))
        const messageLayout = await mainWindow.webContents.executeJavaScript(
          `(() => {
            const cards = [...document.querySelectorAll(".message")]
            const unsafeImage = document.querySelector(".message.assistant img")
            return {
              cardClasses: cards.map((card) => card.className),
              assistantBold: document.querySelector(".message.assistant strong")?.textContent || "",
              code: document.querySelector(".message.assistant pre code")?.textContent || "",
              userRenderedAsMarkdown: Boolean(document.querySelector(".message.user strong")),
              unsafeHandler: unsafeImage?.getAttribute("onerror") || "",
              streamPresent: Boolean(document.querySelector(".message.streaming")),
              mainModel: document.querySelector("#main-agent-model-selector")?.value || "",
              subModel: document.querySelector("#sub-agent-model-selector")?.value || "",
              agentModelsDisabled: Boolean(
                document.querySelector("#main-agent-model-selector")?.disabled ||
                document.querySelector("#sub-agent-model-selector")?.disabled
              ),
              legacyAgentControls: Boolean(
                document.querySelector("#model-selector") || document.querySelector("#kind-selector")
              ),
              userLeft: document.querySelector(".message.user")?.getBoundingClientRect().left || 0,
              assistantLeft: document.querySelector(".message.assistant")?.getBoundingClientRect().left || 0,
            }
          })()`,
        )
        if (
          messageLayout.cardClasses.length !== 2 ||
          !messageLayout.cardClasses[0].includes("user") ||
          !messageLayout.cardClasses[1].includes("assistant") ||
          messageLayout.assistantBold !== "模型粗体" ||
          !messageLayout.code.includes("print('ok')") ||
          messageLayout.userRenderedAsMarkdown ||
          messageLayout.unsafeHandler ||
          messageLayout.streamPresent ||
          messageLayout.mainModel !== "smoke-model" ||
          messageLayout.subModel !== "child-model" ||
          messageLayout.agentModelsDisabled ||
          messageLayout.legacyAgentControls ||
          messageLayout.userLeft <= messageLayout.assistantLeft
        ) {
          throw new Error("Message cards, Markdown safety, or Agent configuration failed")
        }
        const optimisticMessages = await mainWindow.webContents.executeJavaScript(`(() => {
          const input = document.querySelector("#prompt-input")
          input.value = "立即显示的用户消息"
          input.dispatchEvent(new Event("input", { bubbles: true }))
          document.querySelector("#send-button")?.click()
          return {
            user: [...document.querySelectorAll(".message.user")].at(-1)?.querySelector(".message-text")?.textContent || "",
            agent: Boolean(document.querySelector(".message.assistant.streaming")),
            agentStatus: document.querySelector(".message.assistant.streaming .message-text")?.textContent || "",
          }
        })()`)
        if (
          optimisticMessages.user !== "立即显示的用户消息" ||
          !optimisticMessages.agent ||
          optimisticMessages.agentStatus !== "正在思考"
        ) {
          throw new Error("User and Agent message cards were not rendered before the sidecar response")
        }
        let optimisticRollback
        for (let attempt = 0; attempt < 20; attempt += 1) {
          optimisticRollback = await mainWindow.webContents.executeJavaScript(`(() => ({
            prompt: document.querySelector("#prompt-input")?.value || "",
            messageStillPresent: [...document.querySelectorAll(".message.user .message-text")]
              .some((node) => node.textContent === "立即显示的用户消息"),
            agentPlaceholderPresent: Boolean(document.querySelector(".message.assistant.streaming")),
          }))()`)
          if (
            optimisticRollback.prompt === "立即显示的用户消息" &&
            !optimisticRollback.messageStillPresent &&
            !optimisticRollback.agentPlaceholderPresent
          ) break
          await new Promise((resolve) => setTimeout(resolve, 25))
        }
        if (
          optimisticRollback.prompt !== "立即显示的用户消息" ||
          optimisticRollback.messageStillPresent ||
          optimisticRollback.agentPlaceholderPresent
        ) {
          throw new Error("Rejected optimistic messages did not restore the prompt")
        }
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
