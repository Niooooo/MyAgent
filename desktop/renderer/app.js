import {
  COMPACT_ENTER_WIDTH,
  COMPACT_EXIT_WIDTH,
  nextCompactLayout,
} from "./layout.js"

const api = window.myagent
const elements = {
  addModel: document.querySelector("#add-model-button"),
  approvalAllow: document.querySelector("#approval-allow-button"),
  approvalArguments: document.querySelector("#approval-arguments"),
  approvalDeny: document.querySelector("#approval-deny-button"),
  approvalDialog: document.querySelector("#approval-dialog"),
  approvalReason: document.querySelector("#approval-reason"),
  approvalTool: document.querySelector("#approval-tool"),
  conversationPage: document.querySelector("#conversation-page"),
  headerWorkspace: document.querySelector("#header-workspace"),
  kindSelector: document.querySelector("#kind-selector"),
  maximize: document.querySelector("#maximize-button"),
  modelBack: document.querySelector("#model-back-button"),
  modelCancel: document.querySelector("#model-cancel-button"),
  modelDialog: document.querySelector("#model-dialog"),
  modelDialogError: document.querySelector("#model-dialog-error"),
  modelForm: document.querySelector("#model-form"),
  modelBaseUrl: document.querySelector("#model-base-url-input"),
  modelKey: document.querySelector("#model-key-input"),
  modelList: document.querySelector("#model-list"),
  modelName: document.querySelector("#model-name-input"),
  modelPageStatus: document.querySelector("#model-page-status"),
  modelSelector: document.querySelector("#model-selector"),
  modelsButton: document.querySelector("#models-button"),
  modelsPage: document.querySelector("#models-page"),
  newSession: document.querySelector("#new-session-button"),
  openFolder: document.querySelector("#open-folder-button"),
  prompt: document.querySelector("#prompt-input"),
  send: document.querySelector("#send-button"),
  status: document.querySelector("#session-status"),
  tabs: document.querySelector("#tab-bar"),
  transcript: document.querySelector("#transcript"),
  welcome: document.querySelector("#welcome-panel"),
  welcomeLogo: document.querySelector("#welcome-logo"),
  workspace: document.querySelector("#workspace-value"),
}

let state = {
  activeSessionId: 0,
  sessions: [],
  models: [],
  modelError: "",
  lastError: "",
  closing: false,
}
let page = "conversation"
let compact = null
let renderedSessionId = null
let renderedMessagesToken = ""
let activeApproval = null
const approvalQueue = []
const drafts = new Map()
const streamingMessages = new Map()

function activeSession() {
  return state.sessions.find((session) => session.id === state.activeSessionId)
}

function applyState(next) {
  if (!next || !Array.isArray(next.sessions) || !Array.isArray(next.models)) return
  let clearedStream = false
  for (const session of next.sessions) {
    if (session.state !== "busy" && streamingMessages.delete(session.id)) clearedStream = true
  }
  if (clearedStream) renderedMessagesToken = ""
  state = next
  render()
}

async function request(method, params = {}) {
  try {
    const result = await api.request(method, params)
    if (result?.state) applyState(result.state)
    return result
  } catch (error) {
    setStatus(error?.message || String(error), true)
    throw error
  }
}

function setStatus(message, error = false) {
  elements.status.textContent = message
  elements.status.classList.toggle("error", error)
}

function storeDraft() {
  const session = activeSession()
  if (session) drafts.set(session.id, elements.prompt.value)
}

function render() {
  renderTabs()
  renderConversation()
  renderModels()
  renderPage()
}

function renderTabs() {
  elements.tabs.replaceChildren()
  for (const session of state.sessions) {
    const tab = document.createElement("div")
    tab.className = `tab${session.id === state.activeSessionId ? " active" : ""}`
    const title = document.createElement("span")
    title.className = "tab-title"
    title.textContent = session.title
    title.title = session.title
    title.addEventListener("click", async () => {
      if (session.id === state.activeSessionId) return
      storeDraft()
      await request("session.activate", { sessionId: session.id }).catch(() => undefined)
    })
    const close = document.createElement("button")
    close.className = "tab-close"
    close.type = "button"
    close.textContent = "×"
    close.ariaLabel = `关闭 ${session.title}`
    close.addEventListener("click", async () => {
      await request("session.close", { sessionId: session.id }).catch(() => undefined)
    })
    tab.append(title, close)
    elements.tabs.append(tab)
  }
}

function renderConversation() {
  const session = activeSession()
  if (!session) {
    elements.workspace.textContent = ""
    elements.headerWorkspace.textContent = "LOCAL"
    elements.prompt.disabled = true
    elements.send.disabled = true
    setStatus("Python sidecar 未提供可用会话", true)
    return
  }
  elements.workspace.textContent = session.workspace
  elements.headerWorkspace.textContent = `LOCAL · ${session.workspaceName}`
  elements.status.textContent = session.status || "就绪"
  elements.status.classList.toggle("busy", session.state === "busy")
  elements.status.classList.toggle("error", session.status?.includes("失败") || false)

  renderModelSelector(session)
  elements.kindSelector.value = session.kind
  elements.kindSelector.disabled = !session.canChangeKind || state.closing
  elements.prompt.disabled = state.closing
  elements.send.disabled = !session.canSend || state.closing

  if (renderedSessionId !== session.id) {
    renderedSessionId = session.id
    elements.prompt.value = drafts.get(session.id) || ""
    renderedMessagesToken = ""
  }
  renderTranscript(session)
}

function renderModelSelector(session) {
  const names = state.models.map((model) => model.name)
  const desired = session.model || ""
  elements.modelSelector.replaceChildren()
  const placeholder = document.createElement("option")
  placeholder.value = ""
  placeholder.textContent = names.length ? "选择模型" : "尚无模型"
  elements.modelSelector.append(placeholder)
  for (const name of names) {
    const option = document.createElement("option")
    option.value = name
    option.textContent = name
    elements.modelSelector.append(option)
  }
  elements.modelSelector.value = names.includes(desired) ? desired : ""
  elements.modelSelector.disabled = session.state !== "idle" || names.length === 0 || state.closing
}

function renderTranscript(session) {
  const last = session.messages.at(-1)
  const streamText = streamingMessages.get(session.id)
  const hasStream = streamText !== undefined
  const token = `${session.id}:${session.messages.length}:${last?.speaker || ""}:${last?.text || ""}:stream:${hasStream ? streamText.length : -1}`
  if (token === renderedMessagesToken) return
  renderedMessagesToken = token
  if (session.messages.length === 0 && !hasStream) {
    elements.transcript.replaceChildren()
    elements.transcript.hidden = true
    elements.welcome.hidden = false
    return
  }
  elements.welcome.hidden = true
  elements.transcript.hidden = false
  const fragment = document.createDocumentFragment()
  for (const message of session.messages) {
    const article = document.createElement("article")
    article.className = `message${message.speaker === "错误" ? " error" : ""}`
    const speaker = document.createElement("div")
    speaker.className = "message-speaker"
    speaker.textContent = message.speaker
    const text = document.createElement("p")
    text.className = "message-text"
    text.textContent = message.text
    article.append(speaker, text)
    fragment.append(article)
  }
  if (hasStream) {
    const article = document.createElement("article")
    article.className = "message streaming"
    article.dataset.streamSession = String(session.id)
    const speaker = document.createElement("div")
    speaker.className = "message-speaker"
    speaker.textContent = "MyAgent"
    const text = document.createElement("p")
    text.className = "message-text"
    text.textContent = streamText
    article.append(speaker, text)
    fragment.append(article)
  }
  elements.transcript.replaceChildren(fragment)
  elements.transcript.scrollTop = elements.transcript.scrollHeight
}

function startStream(payload) {
  const sessionId = payload?.sessionId
  if (!Number.isInteger(sessionId)) return
  streamingMessages.set(sessionId, "")
  if (sessionId === state.activeSessionId) {
    renderedMessagesToken = ""
    const session = activeSession()
    if (session) renderTranscript(session)
  }
}

function appendStreamDelta(payload) {
  const sessionId = payload?.sessionId
  const delta = payload?.delta
  if (!Number.isInteger(sessionId) || typeof delta !== "string" || !delta) return
  if (!streamingMessages.has(sessionId)) startStream({ sessionId })
  streamingMessages.set(sessionId, `${streamingMessages.get(sessionId) || ""}${delta}`)
  if (sessionId !== state.activeSessionId) return
  const article = elements.transcript.querySelector(".message.streaming")
  const text = article?.querySelector(".message-text")
  if (!article || article.dataset.streamSession !== String(sessionId) || !text) {
    renderedMessagesToken = ""
    const session = activeSession()
    if (session) renderTranscript(session)
    return
  }
  text.append(document.createTextNode(delta))
  elements.transcript.scrollTop = elements.transcript.scrollHeight
}

function renderModels() {
  elements.modelList.replaceChildren()
  if (state.models.length === 0) {
    const empty = document.createElement("div")
    empty.className = "model-empty"
    empty.textContent = "还没有可用模型"
    elements.modelList.append(empty)
  } else {
    for (const model of state.models) {
      const card = document.createElement("article")
      card.className = "model-card"
      const copy = document.createElement("div")
      const name = document.createElement("strong")
      name.textContent = model.name
      const endpoint = document.createElement("small")
      endpoint.textContent = model.baseUrl || "使用全局或 OpenAI 默认地址"
      endpoint.title = endpoint.textContent
      const date = document.createElement("small")
      date.textContent = `凭据已保存 · ${model.createdAt.replace("T", " ").replace("Z", "")}`
      copy.append(name, endpoint, date)
      const remove = document.createElement("button")
      remove.type = "button"
      remove.className = "delete-button"
      remove.textContent = "删除"
      remove.addEventListener("click", async () => {
        if (!window.confirm(`确定删除模型“${model.name}”吗？`)) return
        await request("model.delete", { name: model.name }).catch(() => undefined)
      })
      card.append(copy, remove)
      elements.modelList.append(card)
    }
  }
  elements.modelPageStatus.textContent = state.modelError || state.lastError || `${state.models.length} 个已注册模型`
}

function renderPage() {
  const models = page === "models"
  elements.conversationPage.hidden = models
  elements.modelsPage.hidden = !models
  elements.modelsButton.classList.toggle("active", models)
}

function showConversation() {
  page = "conversation"
  renderPage()
  if (!elements.prompt.disabled) elements.prompt.focus()
}

function showModels() {
  storeDraft()
  page = "models"
  renderPage()
}

async function newSession(cwd) {
  storeDraft()
  await request("session.new", cwd ? { cwd } : {}).catch(() => undefined)
  showConversation()
}

async function openFolder() {
  const folder = await api.pickFolder()
  if (folder) await newSession(folder)
}

async function send() {
  const session = activeSession()
  const prompt = elements.prompt.value.trim()
  if (!session || !prompt || elements.send.disabled) return
  await request("session.submit", { sessionId: session.id, prompt }).catch(() => undefined)
  drafts.set(session.id, "")
  elements.prompt.value = ""
}

function openModelDialog() {
  elements.modelDialogError.textContent = ""
  elements.modelForm.reset()
  elements.modelDialog.showModal()
  elements.modelName.focus()
}

function enqueueApproval(payload) {
  approvalQueue.push(payload)
  showNextApproval()
}

function showNextApproval() {
  if (activeApproval || approvalQueue.length === 0) return
  activeApproval = approvalQueue.shift()
  elements.approvalTool.textContent = `工具：${activeApproval.toolName}`
  elements.approvalReason.textContent = activeApproval.reason
  elements.approvalArguments.textContent = activeApproval.arguments
  elements.approvalDialog.showModal()
}

async function decideApproval(approved) {
  const approval = activeApproval
  if (!approval) return
  activeApproval = null
  elements.approvalDialog.close()
  await request("approval.decide", {
    approvalId: approval.approvalId,
    approved,
  }).catch(() => undefined)
  showNextApproval()
}

function updateResponsiveLayout() {
  const next = nextCompactLayout(compact, window.innerWidth)
  if (next === compact) return
  compact = next
  document.body.classList.toggle("compact", compact)
}

function drawWelcomeLogo() {
  const canvas = elements.welcomeLogo
  const context = canvas.getContext("2d")
  const scale = 2
  context.scale(scale, scale)
  context.fillStyle = "#275dce"
  context.beginPath()
  context.roundRect(110, 22, 120, 120, 26)
  context.fill()
  context.strokeStyle = "#f7f9ff"
  context.lineWidth = 13
  context.lineCap = "round"
  context.lineJoin = "round"
  context.beginPath()
  context.moveTo(136, 116)
  context.lineTo(136, 64)
  context.lineTo(170, 101)
  context.lineTo(204, 64)
  context.lineTo(204, 116)
  context.stroke()
  context.fillStyle = "#f1f5f3"
  context.font = '700 24px "Microsoft YaHei UI", "Segoe UI", sans-serif'
  context.textAlign = "center"
  context.textBaseline = "middle"
  context.fillText("MyAgent", 170, 183)
}

elements.newSession.addEventListener("click", () => newSession())
elements.openFolder.addEventListener("click", openFolder)
elements.modelsButton.addEventListener("click", showModels)
elements.modelBack.addEventListener("click", showConversation)
elements.addModel.addEventListener("click", openModelDialog)
elements.modelCancel.addEventListener("click", () => elements.modelDialog.close())
elements.send.addEventListener("click", send)
elements.prompt.addEventListener("input", () => {
  const session = activeSession()
  if (session) drafts.set(session.id, elements.prompt.value)
})
elements.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.ctrlKey) {
    event.preventDefault()
    send()
  }
})
elements.modelSelector.addEventListener("change", async () => {
  const session = activeSession()
  if (!session || !elements.modelSelector.value) return
  await request("session.select_model", {
    sessionId: session.id,
    name: elements.modelSelector.value,
  }).catch(() => undefined)
})
elements.kindSelector.addEventListener("change", async () => {
  const session = activeSession()
  if (!session) return
  await request("session.set_kind", {
    sessionId: session.id,
    kind: elements.kindSelector.value,
  }).catch(() => undefined)
})
elements.modelForm.addEventListener("submit", async (event) => {
  event.preventDefault()
  elements.modelDialogError.textContent = ""
  try {
    await request("model.register", {
      name: elements.modelName.value,
      baseUrl: elements.modelBaseUrl.value,
      apiKey: elements.modelKey.value,
    })
  } catch (error) {
    elements.modelDialogError.textContent = error?.message || String(error)
    return
  }
  elements.modelKey.value = ""
  elements.modelDialog.close()
})
elements.approvalAllow.addEventListener("click", () => decideApproval(true))
elements.approvalDeny.addEventListener("click", () => decideApproval(false))
elements.approvalDialog.addEventListener("cancel", (event) => {
  event.preventDefault()
  decideApproval(false)
})
document.querySelectorAll("[data-window-action]").forEach((button) => {
  button.addEventListener("click", () => api.windowAction(button.dataset.windowAction))
})
document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key.toLowerCase() === "n") {
    event.preventDefault()
    newSession()
  } else if (event.ctrlKey && event.key.toLowerCase() === "o") {
    event.preventDefault()
    openFolder()
  } else if (event.key === "Escape" && page === "models") {
    event.preventDefault()
    showConversation()
  }
})
window.addEventListener("resize", updateResponsiveLayout, { passive: true })

api.onEvent((message) => {
  if (message.event === "state") applyState(message.payload)
  if (message.event === "approval") enqueueApproval(message.payload)
  if (message.event === "stream-start") startStream(message.payload)
  if (message.event === "stream-delta") appendStreamDelta(message.payload)
  if (message.event === "sidecar-error") setStatus(message.payload?.message || "Python sidecar 异常", true)
})
api.onWindowState((windowState) => {
  elements.maximize.dataset.maximized = String(windowState.maximized)
  elements.maximize.ariaLabel = windowState.maximized ? "还原" : "最大化"
})

drawWelcomeLogo()
updateResponsiveLayout()
request("app.snapshot").catch(() => undefined)

export { COMPACT_ENTER_WIDTH, COMPACT_EXIT_WIDTH }
