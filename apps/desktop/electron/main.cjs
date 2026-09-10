const { app, BrowserWindow, dialog, ipcMain, net, shell, safeStorage } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { createLocalBackend, LOCAL_BACKEND_URL } = require("./local-backend.cjs");
const { ManagedRuntime } = require("./managed-runtime.cjs");

const isDevelopment = !app.isPackaged && !process.env.VISTORA_MANAGED_RUNTIME;
const requestedProfile = app.commandLine.getSwitchValue("user-data-dir");
if (requestedProfile) app.setPath("userData", path.resolve(requestedProfile));
if (app.isPackaged) {
  // Each release starts empty, while later launches of that release retain new work.
  // Isolate Chromium storage too: otherwise old drafts can repopulate the new database.
  const releaseProfile = path.join(app.getPath("userData"), "versions", app.getVersion());
  fs.mkdirSync(releaseProfile, { recursive: true });
  app.setPath("userData", releaseProfile);
  app.setPath("sessionData", releaseProfile);
}
let localBackend = null;
let managedRuntime = null;
let maintenance = false;
let quitReady = false;
let pendingQuit = false;
const hasLock = app.requestSingleInstanceLock();
if (!hasLock) app.quit();
app.on("second-instance", () => {
  const win = BrowserWindow.getAllWindows()[0];
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
});

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
  const requestedBaseUrl = String(input?.baseUrl || "http://127.0.0.1:8000")
    .replace(/\/$/, "")
    .toLowerCase();
  const useEmbeddedBackend = requestedBaseUrl === LOCAL_BACKEND_URL
    ;
  if (useEmbeddedBackend) {
    if (maintenance) return { ok: false, status: 503, data: { message: "正在维护本地数据，请稍后重试" }, headers: {} };
    if (managedRuntime) return managedRuntime.request(input);
    if (!localBackend) {
      return { ok: false, status: 503, data: { message: "内置数据服务尚未就绪" }, headers: {} };
    }
    return localBackend.request(input);
  }

  const baseUrl = normalizeBaseUrl(input?.baseUrl);
  const requestPath = String(input?.path || "/");
  if (!requestPath.startsWith("/")) {
    throw new Error("接口路径无效");
  }

  const controller = new AbortController();
  const requestedTimeout = Number(input?.timeoutMs ?? 8_000);
  const timeoutMs = Number.isFinite(requestedTimeout)
    ? Math.min(Math.max(requestedTimeout, 1_000), 60_000)
    : 8_000;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
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
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url.split("#")[0] !== mainWindow.webContents.getURL().split("#")[0]) event.preventDefault();
  });

  if (isDevelopment) {
    void mainWindow.loadURL("http://127.0.0.1:5173");
  } else {
    void mainWindow.loadFile(path.join(__dirname, "../dist/index.html"));
  }
}

function requireMainFrame(event) {
  if (!event.senderFrame || event.senderFrame !== event.sender.mainFrame) throw new Error("无效调用来源");
}
ipcMain.handle("api:request", (event, input) => { requireMainFrame(event); return requestBackend(input); });
ipcMain.handle("local:status", (event) => { requireMainFrame(event); return managedRuntime?.status() || null; });
ipcMain.handle("local:maintenance", async (event, input) => {
  requireMainFrame(event);
  if (!managedRuntime || maintenance) return { ok: false, error: "本地服务暂不可用或正在处理其他操作" };
  maintenance = true;
  try {
    if (input.operation === "ai") return { ok: true, status: await managedRuntime.configureAi(input) };
    if (input.operation === "backup") {
      const selected = await dialog.showSaveDialog(windowFromEvent(event), {
        title: "保存加密备份", defaultPath: `Morrow-${new Date().toISOString().slice(0, 10)}.vistora`,
        filters: [{ name: "Morrow 加密备份", extensions: ["vistora"] }],
      });
      if (selected.canceled) return { ok: false, canceled: true };
      await managedRuntime.backup(selected.filePath, input.password, input.clientState);
      return { ok: true };
    }
    if (input.operation === "restore") {
      const selected = await dialog.showOpenDialog(windowFromEvent(event), {
        title: "选择加密备份", properties: ["openFile"], filters: [{ name: "Morrow 加密备份", extensions: ["vistora"] }],
      });
      if (selected.canceled) return { ok: false, canceled: true };
      const confirmed = await dialog.showMessageBox(windowFromEvent(event), {
        type: "warning", title: "恢复备份", message: "恢复后将显示备份中的记录和草稿。",
        detail: "原数据库会保留以便回退；在线 AI 将关闭，需要重新配置。请确认已备份当前草稿。",
        buttons: ["取消", "恢复备份"], defaultId: 0, cancelId: 0,
      });
      if (confirmed.response !== 1) return { ok: false, canceled: true };
      return { ok: true, clientState: await managedRuntime.restore(selected.filePaths[0], input.password) };
    }
    return { ok: false, error: "不支持的操作" };
  } catch (error) { return { ok: false, error: error.message || "操作未完成，已有数据已保留" }; }
  finally { maintenance = false; if (pendingQuit) app.quit(); }
});
ipcMain.handle("app:version", () => app.getVersion());
ipcMain.on("window:minimize", (event) => windowFromEvent(event)?.minimize());
ipcMain.on("window:toggle-maximize", (event) => {
  const win = windowFromEvent(event);
  if (!win) return;
  win.isMaximized() ? win.unmaximize() : win.maximize();
});
ipcMain.on("window:close", (event) => windowFromEvent(event)?.close());

app.whenReady().then(async () => {
  if (!hasLock) return;
  try {
    if (app.isPackaged || process.env.VISTORA_MANAGED_RUNTIME) {
      managedRuntime = new ManagedRuntime({
        directory: path.join(app.getPath("userData"), "managed"),
        resources: process.env.VISTORA_MANAGED_RUNTIME || process.resourcesPath,
        safeStorage,
      });
      await managedRuntime.initialize();
      if (!app.isPackaged) {
        await managedRuntime.migrateLegacy(path.join(app.getPath("userData"), "data", "vistora-local-data.json"));
      }
    } else {
      localBackend = createLocalBackend({
      dataDirectory: path.join(app.getPath("userData"), "data"),
      serverVersion: app.getVersion(),
      });
      await localBackend.initialize();
    }
  } catch (error) {
    dialog.showErrorBox(
      "Morrow 无法启动",
      `${error?.message || "无法初始化本地数据目录，已有文件不会被自动清空。"}\n\n数据目录：${path.join(app.getPath("userData"), "data")}`,
    );
    app.quit();
    return;
  }
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", (event) => {
  if (!managedRuntime || quitReady) return;
  event.preventDefault();
  if (maintenance) { pendingQuit = true; return; }
  maintenance = true;
  managedRuntime.stop().catch(() => {}).finally(() => { quitReady = true; app.quit(); });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
