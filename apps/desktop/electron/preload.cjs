const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("vistoraDesktop", {
  platform: process.platform,
  apiRequest: (input) => ipcRenderer.invoke("api:request", input),
  appVersion: () => ipcRenderer.invoke("app:version"),
  window: {
    minimize: () => ipcRenderer.send("window:minimize"),
    toggleMaximize: () => ipcRenderer.send("window:toggle-maximize"),
    close: () => ipcRenderer.send("window:close"),
  },
});
