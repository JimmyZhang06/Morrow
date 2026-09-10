const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("vistoraDesktop", {
  platform: process.platform,
  apiRequest: (input) => ipcRenderer.invoke("api:request", input),
  appVersion: () => ipcRenderer.invoke("app:version"),
  localStatus: () => ipcRenderer.invoke("local:status"),
  localMaintenance: (input) => ipcRenderer.invoke("local:maintenance", input),
  window: {
    minimize: () => ipcRenderer.send("window:minimize"),
    toggleMaximize: () => ipcRenderer.send("window:toggle-maximize"),
    close: () => ipcRenderer.send("window:close"),
  },
});
