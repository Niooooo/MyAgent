const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("myagent", {
  request: (method, params = {}) => ipcRenderer.invoke("myagent:request", method, params),
  pickFolder: () => ipcRenderer.invoke("myagent:pick-folder"),
  platform: () => ipcRenderer.invoke("myagent:platform"),
  windowAction: (action) => ipcRenderer.send("myagent:window", action),
  onEvent: (listener) => {
    const handler = (_event, message) => listener(message)
    ipcRenderer.on("myagent:event", handler)
    return () => ipcRenderer.removeListener("myagent:event", handler)
  },
  onWindowState: (listener) => {
    const handler = (_event, state) => listener(state)
    ipcRenderer.on("myagent:window-state", handler)
    return () => ipcRenderer.removeListener("myagent:window-state", handler)
  },
})
