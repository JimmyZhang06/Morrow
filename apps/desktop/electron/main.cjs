const { app, BrowserWindow, ipcMain, net, shell } = require("electron");
const path = require("node:path");

const isDevelopment = !app.isPackaged;

function windowFromEvent(event) {
  return BrowserWindow.fromWebContents(event.sender);
}

function normalizeBaseUrl(value) {
  const parsed = new URL(value || "http://127.0.0.1:8000");
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error("API 地址只支持 HTTP 或 HTTPS");
  }
  return parsed.toString().replace(/\/$/, "");
}

async function requestBackend(input) {
  const baseUrl = normalizeBaseUrl(input?.baseUrl);
  const requestPath = String(input?.path || "/");
  if (!requestPath.startsWith("/")) {
    throw new Error("接口路径无效");
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  const headers = {
    Accept: "application/json",
    ...(input?.body === undefined ? {} : { "Content-Type": "application/json" }),
    ...(input?.token ? { Authorization: `Bearer ${input.token}` } : {}),
    ...(input?.vaultId ? { "X-Vault-ID": input.vaultId } : {}),
    ...(input?.headers || {}),
  };

  try {
    const response = await net.fetch(`${baseUrl}${requestPath}`, {
      method: input?.method || "GET",
      headers,
      body: input?.body === undefined ? undefined : JSON.stringify(input.body),
      signal: controller.signal,
    });
    const raw = await response.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = { message: raw.slice(0, 800) };
      }
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
      headers: {
        etag: response.headers.get("etag"),
        contentType: response.headers.get("content-type"),
      },
    };
  } catch (error) {
    const message = error?.name === "AbortError" ? "后端响应超时" : "无法连接后端服务";
    return { ok: false, status: 0, data: { message }, headers: {} };
  } finally {
    clearTimeout(timeout);
  }
}

function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1380,
    height: 860,
    minWidth: 980,
    minHeight: 680,
    show: false,
    frame: false,
    backgroundColor: "#f3f0e9",
    icon: path.join(__dirname, "../resources/icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: isDevelopment,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://") || url.startsWith("http://")) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });

  if (isDevelopment) {
    void mainWindow.loadURL("http://127.0.0.1:5173");
  } else {
    void mainWindow.loadFile(path.join(__dirname, "../dist/index.html"));
  }
}

ipcMain.handle("api:request", (_event, input) => requestBackend(input));
ipcMain.handle("app:version", () => app.getVersion());
ipcMain.on("window:minimize", (event) => windowFromEvent(event)?.minimize());
ipcMain.on("window:toggle-maximize", (event) => {
  const win = windowFromEvent(event);
  if (!win) return;
  win.isMaximized() ? win.unmaximize() : win.maximize();
});
ipcMain.on("window:close", (event) => windowFromEvent(event)?.close());

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
