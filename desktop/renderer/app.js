import {
  COMPACT_ENTER_WIDTH,
  COMPACT_EXIT_WIDTH,
  nextCompactLayout,
} from "./layout.js"
import { renderMarkdown } from "./markdown.js"

const api = window.myagent
const elements = {
  addModel: document.querySelector("#add-model-button"),
  agentConfig: document.querySelector("#agent-config"),
  approvalAllow: document.querySelector("#approval-allow-button"),
  approvalArguments: document.querySelector("#approval-arguments"),
  approvalDeny: document.querySelector("#approval-deny-button"),
  approvalDialog: document.querySelector("#approval-dialog"),
  approvalReason: document.querySelector("#approval-reason"),
  approvalTool: document.querySelector("#approval-tool"),
  conversationPage: document.querySelector("#conversation-page"),
  deleteTabMenuItem: document.querySelector("#delete-tab-menu-item"),
  errorToast: document.querySelector("#error-toast"),
  headerWorkspace: document.querySelector("#header-workspace"),
  mainAgentModelSelector: document.querySelector("#main-agent-model-selector"),
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
  modelsButton: document.querySelector("#models-button"),
  modelsPage: document.querySelector("#models-page"),
  newSession: document.querySelector("#new-session-button"),
  openFolder: document.querySelector("#open-folder-button"),
  prompt: document.querySelector("#prompt-input"),
  renameTabMenuItem: document.querySelector("#rename-tab-menu-item"),
  send: document.querySelector("#send-button"),
  settingsButton: document.querySelector("#settings-button"),
  settingsCancel: document.querySelector("#settings-cancel-button"),
  settingsDialog: document.querySelector("#settings-dialog"),
  settingsDialogError: document.querySelector("#settings-dialog-error"),
  settingsForm: document.querySelector("#settings-form"),
  maxToolRounds: document.querySelector("#max-tool-rounds-input"),
  bashTimeoutSeconds: document.querySelector("#bash-timeout-seconds-input"),
  todoReminderToolCalls: document.querySelector("#todo-reminder-tool-calls-input"),
  subagentMaxWorkers: document.querySelector("#subagent-max-workers-input"),
  subagentMaxTasks: document.querySelector("#subagent-max-tasks-input"),
  status: document.querySelector("#session-status"),
  subAgentModelSelector: document.querySelector("#sub-agent-model-selector"),
  tabContextMenu: document.querySelector("#tab-context-menu"),
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
  conversationError: "",
  settings: {},
  settingsError: "",
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
const ERROR_TOAST_DURATION_MS = 5000
let errorToastTimer = null
let tabContextSessionId = null

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

async function request(method, params = {}, { notifyError = true } = {}) {
  try {
    const result = await api.request(method, params)
    if (result?.state) applyState(result.state)
    return result
  } catch (error) {
    if (notifyError) showErrorToast(error)
    throw error
  }
}

function errorMessage(error) {
  let message = error?.message || String(error)
  message = message.replace(/^Error invoking remote method ['"]myagent:request['"]:\s*/i, "")
  while (/^Error:\s*/i.test(message)) message = message.replace(/^Error:\s*/i, "")
  return message || "请求失败"
}

function showErrorToast(error) {
  window.clearTimeout(errorToastTimer)
  elements.errorToast.textContent = errorMessage(error)
  elements.errorToast.hidden = false
  errorToastTimer = window.setTimeout(() => {
    elements.errorToast.hidden = true
    elements.errorToast.textContent = ""
    errorToastTimer = null
  }, ERROR_TOAST_DURATION_MS)
}

function closeAgentConfig() {
  elements.agentConfig.open = false
}

function openSettings() {
  const values = state.settings || {}
  for (const [name, input] of [
    ["maxToolRounds", elements.maxToolRounds],
    ["bashTimeoutSeconds", elements.bashTimeoutSeconds],
    ["todoReminderToolCalls", elements.todoReminderToolCalls],
    ["subagentMaxWorkers", elements.subagentMaxWorkers],
    ["subagentMaxTasks", elements.subagentMaxTasks],
  ]) input.value = values[name] ?? ""
  elements.settingsDialogError.textContent = state.settingsError || ""
  elements.settingsDialog.showModal()
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
  closeTabContextMenu()
  elements.tabs.replaceChildren()
  for (const session of state.sessions) {
    const tab = document.createElement("div")
    tab.className = `tab${session.id === state.activeSessionId ? " active" : ""}`
    tab.dataset.sessionId = String(session.id)
    tab.addEventListener("contextmenu", (event) => openTabContextMenu(event, session.id))
    const title = document.createElement("span")
    title.className = "tab-title"
    title.textContent = session.title
    title.title = `${session.title}（右键可修改名称或删除对话）`
    title.tabIndex = 0
    title.setAttribute(
      "aria-label",
      `${session.title}，右键可修改名称或删除对话，双击或按 F2 修改名称`,
    )
    title.addEventListener("click", async () => {
      if (session.id === state.activeSessionId) return
      storeDraft()
      await request("session.activate", { sessionId: session.id }).catch(() => undefined)
    })
    title.addEventListener("dblclick", (event) => {
      event.preventDefault()
      event.stopPropagation()
      beginTabRename(session, title)
    })
    title.addEventListener("keydown", (event) => {
      if (event.key !== "F2") return
      event.preventDefault()
      beginTabRename(session, title)
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

function openTabContextMenu(event, sessionId) {
  event.preventDefault()
  event.stopPropagation()
  tabContextSessionId = sessionId
  elements.tabContextMenu.hidden = false
  const bounds = elements.tabContextMenu.getBoundingClientRect()
  const margin = 8
  const left = Math.max(margin, Math.min(event.clientX, window.innerWidth - bounds.width - margin))
  const top = Math.max(margin, Math.min(event.clientY, window.innerHeight - bounds.height - margin))
  elements.tabContextMenu.style.left = `${left}px`
  elements.tabContextMenu.style.top = `${top}px`
  elements.renameTabMenuItem.focus({ preventScroll: true })
}

function closeTabContextMenu() {
  elements.tabContextMenu.hidden = true
  tabContextSessionId = null
}

function renameTabFromContextMenu() {
  const sessionId = tabContextSessionId
  const session = state.sessions.find((item) => item.id === sessionId)
  const title = elements.tabs.querySelector(
    `.tab[data-session-id="${sessionId}"] .tab-title`,
  )
  closeTabContextMenu()
  if (session && title) beginTabRename(session, title)
}

async function deleteTabFromContextMenu() {
  const sessionId = tabContextSessionId
  const session = state.sessions.find((item) => item.id === sessionId)
  closeTabContextMenu()
  if (!session) return
  const confirmed = window.confirm(
    `确定删除对话“${session.title}”吗？\n\n这会删除本机持久化记录，且无法撤销。`,
  )
  if (!confirmed) return
  await request("session.delete", { sessionId: session.id }).catch(() => undefined)
}

function beginTabRename(session, titleElement) {
  const input = document.createElement("input")
  input.className = "tab-title-input"
  input.type = "text"
  input.value = session.title
  input.maxLength = 80
  input.setAttribute("aria-label", "会话名称")
  titleElement.replaceWith(input)
  input.focus()
  input.select()
  let finished = false

  const finish = async (save) => {
    if (finished) return
    finished = true
    const nextTitle = input.value.trim()
    if (save && !nextTitle) {
      showErrorToast("会话名称不能为空")
    } else if (save && nextTitle !== session.title) {
      await request("session.rename", {
        sessionId: session.id,
        title: nextTitle,
      }).catch(() => undefined)
    }
    renderTabs()
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault()
      finish(true)
    } else if (event.key === "Escape") {
      event.preventDefault()
      finish(false)
    }
  })
  input.addEventListener("blur", () => finish(true))
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
  if (state.conversationError) setStatus(state.conversationError, true)

  renderAgentConfiguration(session)
  elements.prompt.disabled = state.closing
  elements.send.disabled = !session.canSend || state.closing

  if (renderedSessionId !== session.id) {
    renderedSessionId = session.id
    elements.prompt.value = drafts.get(session.id) || ""
    renderedMessagesToken = ""
  }
  renderTranscript(session)
}

function renderAgentConfiguration(session) {
  const names = state.models.map((model) => model.name)
  const mainModel = session.agentModels?.main || session.model || ""
  const subModel = session.agentModels?.sub || ""
  const mainPlaceholder = document.createElement("option")
  mainPlaceholder.value = ""
  mainPlaceholder.textContent = names.length ? "选择模型" : "尚无模型"
  elements.mainAgentModelSelector.replaceChildren(mainPlaceholder)
  const followMain = document.createElement("option")
  followMain.value = ""
  followMain.textContent = mainModel ? `跟随主 Agent（${mainModel}）` : "跟随主 Agent"
  elements.subAgentModelSelector.replaceChildren(followMain)
  for (const name of names) {
    const mainOption = document.createElement("option")
    mainOption.value = name
    mainOption.textContent = name
    elements.mainAgentModelSelector.append(mainOption)
    const subOption = mainOption.cloneNode(true)
    elements.subAgentModelSelector.append(subOption)
  }
  elements.mainAgentModelSelector.value = names.includes(mainModel) ? mainModel : ""
  elements.subAgentModelSelector.value = names.includes(subModel) ? subModel : ""
  const disabled = !session.canConfigureAgents || names.length === 0 || state.closing
  elements.mainAgentModelSelector.disabled = disabled
  elements.subAgentModelSelector.disabled = disabled
  elements.agentConfig.classList.toggle("disabled", disabled)
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
    fragment.append(createMessageCard(message.speaker, message.text))
  }
  if (hasStream) {
    fragment.append(createMessageCard("MyAgent", streamText, session.id))
  }
  elements.transcript.replaceChildren(fragment)
  elements.transcript.scrollTop = elements.transcript.scrollHeight
}

function createMessageCard(speakerName, content, streamSessionId = null) {
  const role = speakerName === "你" ? "user" : speakerName === "错误" ? "error" : "assistant"
  const article = document.createElement("article")
  article.className = `message ${role}${streamSessionId === null ? "" : " streaming"}`
  if (streamSessionId !== null) article.dataset.streamSession = String(streamSessionId)
  const speaker = document.createElement("div")
  speaker.className = "message-speaker"
  speaker.textContent = speakerName
  const text = document.createElement("div")
  text.className = `message-text${role === "assistant" ? " markdown-body" : ""}`
  if (role === "assistant") {
    renderMarkdown(text, content)
  } else {
    text.textContent = content
  }
  article.append(speaker, text)
  return article
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
  renderMarkdown(text, streamingMessages.get(sessionId))
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
  const previous = {
    state: session.state,
    canSend: session.canSend,
    canConfigureAgents: session.canConfigureAgents,
    status: session.status,
  }
  const optimisticMessage = { speaker: "你", text: prompt }
  session.messages.push(optimisticMessage)
  session.state = "busy"
  session.canSend = false
  session.canConfigureAgents = false
  session.status = `正在发送 · ${session.agentModels?.main || session.model || "MyAgent"}`
  drafts.set(session.id, "")
  elements.prompt.value = ""
  renderedMessagesToken = ""
  renderConversation()

  try {
    await request("session.submit", { sessionId: session.id, prompt })
  } catch {
    const current = state.sessions.find((item) => item.id === session.id)
    if (current === session) {
      const optimisticIndex = current.messages.indexOf(optimisticMessage)
      if (optimisticIndex >= 0) current.messages.splice(optimisticIndex, 1)
      Object.assign(current, previous)
    }
    drafts.set(session.id, prompt)
    if (state.activeSessionId === session.id) elements.prompt.value = prompt
    renderedMessagesToken = ""
    render()
  }
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
elements.renameTabMenuItem.addEventListener("click", renameTabFromContextMenu)
elements.deleteTabMenuItem.addEventListener("click", deleteTabFromContextMenu)
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
elements.mainAgentModelSelector.addEventListener("change", async () => {
  const session = activeSession()
  if (!session || !elements.mainAgentModelSelector.value) return
  await request("session.configure_agent_model", {
    sessionId: session.id,
    role: "main",
    name: elements.mainAgentModelSelector.value,
  }).catch(() => undefined)
})
elements.subAgentModelSelector.addEventListener("change", async () => {
  const session = activeSession()
  if (!session) return
  await request("session.configure_agent_model", {
    sessionId: session.id,
    role: "sub",
    name: elements.subAgentModelSelector.value,
  }).catch(() => undefined)
})
elements.settingsButton.addEventListener("click", openSettings)
elements.settingsCancel.addEventListener("click", () => elements.settingsDialog.close())
elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault()
  elements.settingsDialogError.textContent = ""
  const payload = {
    maxToolRounds: elements.maxToolRounds.valueAsNumber,
    bashTimeoutSeconds: elements.bashTimeoutSeconds.valueAsNumber,
    todoReminderToolCalls: elements.todoReminderToolCalls.valueAsNumber,
    subagentMaxWorkers: elements.subagentMaxWorkers.valueAsNumber,
    subagentMaxTasks: elements.subagentMaxTasks.valueAsNumber,
  }
  if (!Object.values(payload).every(Number.isInteger)) {
    elements.settingsDialogError.textContent = "所有运行设置都必须是整数"
    return
  }
  try {
    await request("settings.update", payload, { notifyError: false })
    elements.settingsDialog.close()
  } catch (error) {
    elements.settingsDialogError.textContent = errorMessage(error)
  }
})
elements.modelForm.addEventListener("submit", async (event) => {
  event.preventDefault()
  elements.modelDialogError.textContent = ""
  try {
    await request(
      "model.register",
      {
        name: elements.modelName.value,
        baseUrl: elements.modelBaseUrl.value,
        apiKey: elements.modelKey.value,
      },
      { notifyError: false },
    )
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
  if (event.key === "Escape" && elements.agentConfig.open) {
    event.preventDefault()
    closeAgentConfig()
  } else if (event.key === "Escape" && !elements.tabContextMenu.hidden) {
    event.preventDefault()
    closeTabContextMenu()
  } else if (event.ctrlKey && event.key.toLowerCase() === "n") {
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
document.addEventListener("pointerdown", (event) => {
  if (elements.agentConfig.open && !elements.agentConfig.contains(event.target)) {
    closeAgentConfig()
  }
  if (!elements.tabContextMenu.hidden && !elements.tabContextMenu.contains(event.target)) {
    closeTabContextMenu()
  }
})
window.addEventListener("resize", updateResponsiveLayout, { passive: true })
window.addEventListener("resize", closeTabContextMenu, { passive: true })
window.addEventListener("resize", closeAgentConfig, { passive: true })
window.addEventListener("blur", closeTabContextMenu)
window.addEventListener("blur", closeAgentConfig)

api.onEvent((message) => {
  if (message.event === "state") applyState(message.payload)
  if (message.event === "approval") enqueueApproval(message.payload)
  if (message.event === "stream-start") startStream(message.payload)
  if (message.event === "stream-delta") appendStreamDelta(message.payload)
  if (message.event === "sidecar-error") showErrorToast(message.payload?.message || "Python sidecar 异常")
})
api.onWindowState((windowState) => {
  elements.maximize.dataset.maximized = String(windowState.maximized)
  elements.maximize.ariaLabel = windowState.maximized ? "还原" : "最大化"
})

drawWelcomeLogo()
updateResponsiveLayout()
request("app.snapshot").catch(() => undefined)

export { COMPACT_ENTER_WIDTH, COMPACT_EXIT_WIDTH }
