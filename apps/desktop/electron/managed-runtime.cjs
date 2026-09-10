const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const net = require("node:net");
const { spawn } = require("node:child_process");
const { profiles, publicProfile, configureProfiles } = require("./model-profiles.cjs");

async function atomicWrite(file, bytes) {
  const temp = `${file}.${crypto.randomUUID()}.tmp`;
  let handle;
  try {
    handle = await fs.open(temp, "wx", 0o600);
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close(); handle = null;
    await fs.rename(temp, file);
  } finally {
    if (handle) await handle.close().catch(() => {});
    await fs.unlink(temp).catch(() => {});
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function run(executable, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { windowsHide: true, stdio: "ignore", ...options });
    const timer = setTimeout(() => { child.kill(); reject(new Error("本地服务操作超时")); }, 120000);
    child.once("error", () => { clearTimeout(timer); reject(new Error("无法启动内置运行组件")); });
    child.once("exit", (code) => { clearTimeout(timer); code === 0 ? resolve() : reject(new Error("本地数据库操作失败，已有数据已保留")); });
  });
}

class ManagedRuntime {
  constructor({ directory, resources, safeStorage, backendCommand }) {
    this.directory = directory;
    this.resources = resources;
    this.safeStorage = safeStorage;
    this.backendCommand = backendCommand || [path.join(resources, "backend", "vistora-backend.exe")];
    this.configFile = path.join(directory, "private-settings.bin");
    this.databaseDirectory = path.join(directory, "postgres-data");
    this.process = null;
    this.config = null;
    this.port = null;
    this.stopping = false;
  }

  pg(name) { return path.join(this.resources, "pgsql", "bin", `${name}.exe`); }
  environment() {
    return { ...process.env, PGPASSWORD: this.config.adminPassword, PGHOST: "127.0.0.1",
      PGPORT: String(this.config.databasePort), PGUSER: "vistora_owner", PGDATABASE: this.config.databaseName || "postgres" };
  }

  async saveConfig(config) {
    if (!this.safeStorage.isEncryptionAvailable()) throw new Error("Windows 密钥保护不可用，无法保存设置");
    await atomicWrite(this.configFile, this.safeStorage.encryptString(JSON.stringify(config)));
    this.config = config;
  }

  async initialize() {
    await fs.mkdir(this.directory, { recursive: true });
    try {
      const bytes = await fs.readFile(this.configFile);
      this.config = JSON.parse(this.safeStorage.decryptString(bytes));
      if (this.config.version !== 1) throw new Error("unsupported");
    } catch (error) {
      if (error.code !== "ENOENT") throw new Error("无法解锁本机设置。请使用原 Windows 账户或恢复加密备份，原数据已保留。");
      const existingDatabase = await fs.stat(this.databaseDirectory).catch(() => null);
      if (existingDatabase) throw new Error("本地数据库存在但密钥设置缺失，已停止初始化以保护数据");
      const secret = () => crypto.randomBytes(32).toString("hex");
      await this.saveConfig({ version: 1, adminPassword: secret(), runtimePassword: secret(),
        token: secret(), contentKey: secret(), sourceHmac: secret(), modelHmac: secret(),
        principalId: crypto.randomUUID(), vaultId: crypto.randomUUID(),
        databasePort: await freePort(), ai: { enabled: false, model: "step-3.7-flash", key: "" } });
    }
    await this.start();
  }

  async start() {
    const versionFile = path.join(this.databaseDirectory, "PG_VERSION");
    if (!await fs.stat(versionFile).catch(() => null)) {
      // Initialize in a staging directory. Interrupted initialization never touches a live cluster.
      const staged = path.join(this.directory, `postgres-init-${crypto.randomUUID()}`);
      const passwordFile = path.join(this.directory, `init-${crypto.randomUUID()}.secret`);
      await atomicWrite(passwordFile, this.config.adminPassword);
      try {
        await run(this.pg("initdb"), ["-D", staged, "-U", "vistora_owner", "--encoding=UTF8", "--locale=C",
          "--auth=scram-sha-256", `--pwfile=${passwordFile}`]);
        await fs.rename(staged, this.databaseDirectory);
      } finally { await fs.unlink(passwordFile).catch(() => {}); }
    }
    const version = (await fs.readFile(versionFile, "utf8")).trim();
    if (version !== "17") throw new Error("数据库主版本不兼容，请保留数据并使用匹配版本恢复");
    // pg_ctl status permits recovery after an app crash without starting a second server.
    let running = true;
    try { await run(this.pg("pg_ctl"), ["status", "-D", this.databaseDirectory]); } catch { running = false; }
    if (!running) {
      await run(this.pg("pg_ctl"), ["start", "-D", this.databaseDirectory, "-w", "-t", "60",
        "-l", path.join(this.directory, "postgres.log"), "-o",
        `-h 127.0.0.1 -p ${this.config.databasePort} -c log_statement=none -c log_min_error_statement=panic -c log_min_messages=panic`]);
    }
    await this.startApi();
  }

  async startApi() {
    this.port = await freePort();
    const c = this.config;
    const dsn = (user, password) => `postgresql+asyncpg://${user}:${password}@127.0.0.1:${c.databasePort}/${c.databaseName || "postgres"}`;
    const environment = {
      APP_ENV: "desktop", APP_DATABASE_URL: dsn("vistora_runtime", c.runtimePassword),
      LOCAL_ADMIN_DATABASE_URL: dsn("vistora_owner", c.adminPassword),
      LOCAL_RUNTIME_DATABASE_ROLE: "vistora_runtime", LOCAL_RUNTIME_DATABASE_PASSWORD: c.runtimePassword,
      APP_LOCAL_AUTH_ENABLED: "true", APP_LOCAL_AUTH_PRINCIPAL_ID: c.principalId, APP_LOCAL_AUTH_TOKEN: c.token,
      LOCAL_VAULT_ID: c.vaultId, APP_SOURCE_API_ENABLED: "true", APP_MEMORY_API_ENABLED: "true",
      APP_SOURCE_API_HMAC_KEY: c.sourceHmac, APP_LOCAL_SOURCE_CONTENT_KEY: c.contentKey,
      APP_MODEL_RUN_HMAC_KEY: c.modelHmac, APP_LOG_LEVEL: "WARNING",
      APP_MODEL_PROVIDER: c.ai.enabled ? (c.ai.protocol === "compatible" ? "desktop-compatible" : "stepfun-step-plan") : "disabled",
      APP_CANDIDATE_ASYNC_ENABLED: c.ai.enabled ? "true" : "false",
      ...(c.ai.activationId ? { LOCAL_AI_ACTIVATION_ID: c.ai.activationId } : {}),
      ...(c.ai.enabled && c.ai.protocol === "compatible" ? {
        APP_COMPATIBLE_API_KEY: c.ai.key, APP_COMPATIBLE_MODEL: c.ai.model,
        APP_COMPATIBLE_BASE_URL: c.ai.baseUrl, APP_COMPATIBLE_JSON_MODE: String(c.ai.jsonMode),
      } : c.ai.enabled ? { APP_STEPFUN_API_KEY: c.ai.key, APP_STEPFUN_MODEL: c.ai.model,
        APP_STEPFUN_BASE_URL: c.ai.baseUrl || "https://api.stepfun.com/step_plan/v1",
        APP_STEPFUN_TIMEOUT_SECONDS: "60" } : {}),
    };
    const child = spawn(this.backendCommand[0], this.backendCommand.slice(1), {
      cwd: this.directory, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    });
    this.process = child;
    let failed = false;
    let diagnostic = "";
    child.stdout.on("data", (chunk) => {
      const match = String(chunk).match(/"failure_code"\s*:\s*"([a-z0-9_]{1,64})"/);
      if (match) this.lastProviderFailure = match[1];
    });
    child.stderr.on("data", (chunk) => {
      const match = String(chunk).match(/DESKTOP_BACKEND_START_FAILED(?::[A-Za-z]+)?/);
      if (match) diagnostic = match[0];
      const provider = String(chunk).match(/"failure_code"\s*:\s*"([a-z0-9_]{1,64})"/);
      if (provider) this.lastProviderFailure = provider[1];
    });
    child.once("error", () => { failed = true; });
    child.stdin.on("error", () => {});
    child.stdin.write(`${JSON.stringify({ version: 1, environment, aiConsent: c.ai.enabled, apiPort: this.port })}\n`);
    for (let attempt = 0; attempt < 180; attempt++) {
      if (failed || child.exitCode !== null) throw new Error(`完整数据服务启动失败，已有数据已保留 ${diagnostic}`);
      try {
        const response = await fetch(`http://127.0.0.1:${this.port}/health/ready`, { signal: AbortSignal.timeout(1000) });
        if (response.ok) return;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    await this.stopApi();
    throw new Error("完整数据服务启动超时，请重启应用重试");
  }

  async request(input) {
    if (!this.process || this.process.exitCode !== null || this.stopping) {
      return { ok: false, status: 503, data: { message: "本地服务暂不可用，请重启应用" }, headers: {} };
    }
    const requestPath = String(input.path || "/");
    if (!requestPath.startsWith("/") || requestPath.startsWith("//")) throw new Error("无效接口路径");
    try {
      const response = await fetch(`http://127.0.0.1:${this.port}${requestPath}`, {
        method: input.method || "GET", signal: AbortSignal.timeout(Math.min(input.timeoutMs || 8000, 60000)),
        headers: { ...input.headers, "Content-Type": "application/json",
          Authorization: `Bearer ${this.config.token}`, "X-Vault-ID": this.config.vaultId },
        body: input.body === undefined ? undefined : JSON.stringify(input.body), redirect: "error",
      });
      const contentType = response.headers.get("content-type");
      const text = await response.text();
      return { ok: response.ok, status: response.status,
        data: contentType?.includes("json") ? JSON.parse(text) : text,
        headers: { etag: response.headers.get("etag"), contentType } };
    } catch {
      return { ok: false, status: 503, data: { message: "本地服务响应超时或连接中断，请刷新确认状态后重试" }, headers: {} };
    }
  }

  status() {
    const issues = {
      subscription_inactive: "Step Plan 订阅未激活，请在供应商账户中启用订阅后再整理记录。",
      http_400: "模型服务拒绝了请求，请检查订阅、模型名称和账户权限。",
      http_401: "API Key 无效或已失效，请重新配置。",
      http_403: "模型账户没有此服务的访问权限。",
      http_429: "模型服务限流或额度不足，请检查账户后稍后再试。",
      timeout: "在线模型响应超时，记录仍在本机。请先检查原任务状态，避免重复调用。",
      transport_connect_failed: "无法连接在线模型，请检查网络。",
    };
    return { enabled: this.config.ai.enabled, model: this.config.ai.model, hasKey: Boolean(this.config.ai.key),
      activeProfileId: this.config.ai.profileId || "legacy-stepfun",
      profiles: profiles(this.config).map(publicProfile),
      issue: this.lastProviderFailure ? issues[this.lastProviderFailure] || "模型请求未完成，请检查账户及网络后重试。" : null };
  }

  async migrateLegacy(file) {
    if (this.config.legacyImported) return;
    const raw = await fs.readFile(file, "utf8").catch((error) => { if (error.code === "ENOENT") return null; throw error; });
    if (raw === null) return;
    const legacy = JSON.parse(raw);
    if (legacy.schemaVersion !== 1 || !Array.isArray(legacy.entries)) throw new Error("旧版记录格式无效，原文件已保留");
    for (const entry of legacy.entries) {
      const revisions = entry.revisions?.length ? entry.revisions : [{ content: entry.content }];
      const created = await this.request({ path: "/v1/entries", method: "POST",
        headers: { "Idempotency-Key": `legacy:${entry.id}` }, body: {
          client_id: entry.id, content: revisions[0].content, captured_at: entry.captured_at,
          memory_policy: "default", source_type: "note", data_class: "sensitive",
        } });
      if (!created.ok) throw new Error("旧记录迁移未完成，原文件已保留，重启后会安全重试");
      const id = created.data.id;
      for (let i = 1; i < revisions.length; i++) {
        const revised = await this.request({ path: `/v1/entries/${id}`, method: "PATCH",
          headers: { "Idempotency-Key": `legacy:${entry.id}:revision:${i + 1}`, "If-Match": `"${i}"` },
          body: { content: revisions[i].content, expected_revision: i } });
        if (!revised.ok) throw new Error("旧记录修订迁移未完成，原文件已保留");
      }
      const verified = await this.request({ path: `/v1/entries/${id}` });
      if (!verified.ok || verified.data.content !== entry.content) throw new Error("旧记录迁移核对失败，原文件已保留");
    }
    await this.saveConfig({ ...this.config, legacyImported: true });
  }

  async backup(destination, password, clientState) {
    const { sealBackup } = require("./private-backup.cjs");
    const dump = path.join(this.directory, `backup-${crypto.randomUUID()}.dump`);
    try {
      await this.stopApi();
      await run(this.pg("pg_dump"), ["--format=custom", "--file", dump], { env: this.environment() });
      const bytes = await fs.readFile(dump);
      const c = this.config;
      const payload = { version: 1, createdAt: new Date().toISOString(), database: bytes.toString("base64"),
        identity: { principalId: c.principalId, vaultId: c.vaultId, contentKey: c.contentKey,
          sourceHmac: c.sourceHmac, modelHmac: c.modelHmac, legacyImported: c.legacyImported }, clientState };
      await atomicWrite(destination, sealBackup(payload, password));
    } finally {
      await fs.unlink(dump).catch(() => {});
      await this.startApi();
    }
  }

  async restore(source, password) {
    const { openBackup } = require("./private-backup.cjs");
    const stat = await fs.stat(source);
    if (stat.size > 256 * 1024 * 1024) throw new Error("备份超过当前试用版支持的 256 MB，请保留文件并联系维护者");
    const payload = openBackup(await fs.readFile(source), password);
    const name = `vistora_restore_${crypto.randomBytes(12).toString("hex")}`;
    const dump = path.join(this.directory, `${name}.dump`);
    const previous = this.config;
    this.lastProviderFailure = null;
    await atomicWrite(dump, Buffer.from(payload.database, "base64"));
    await this.stopApi();
    try {
      await run(this.pg("createdb"), [name], { env: this.environment() });
      await run(this.pg("pg_restore"), ["--exit-on-error", "--no-owner", "--dbname", name, dump], { env: this.environment() });
      await this.saveConfig({ ...previous, ...payload.identity, databaseName: name,
        aiProfiles: profiles(previous).map(p => ({ ...p, key: "" })),
        ai: { enabled: false, key: "", model: "step-3.7-flash" } });
      await this.startApi();
      return payload.clientState;
    } catch {
      await this.stopApi();
      await this.saveConfig(previous);
      await this.startApi();
      throw new Error("备份未恢复成功，已返回原数据库，原数据未被覆盖");
    } finally { await fs.unlink(dump).catch(() => {}); }
  }

  async configureAi(input) {
    const next = configureProfiles(this.config, input);
    const previous = this.config;
    if (input.deleteProfileId && next.ai === previous.ai) {
      await this.saveConfig(next);
      return this.status();
    }
    await this.stopApi();
    try {
      await this.saveConfig(next);
      await this.startApi();
      this.lastProviderFailure = null;
    } catch {
      await this.stopApi();
      await this.saveConfig(previous);
      await this.startApi();
      throw new Error("AI 配置未生效，已恢复原设置");
    }
    return this.status();
  }

  async stopApi() {
    const child = this.process;
    this.process = null;
    if (!child || child.exitCode !== null || !child.pid) return;
    await new Promise((resolve) => {
      const timer = setTimeout(() => child.kill(), 15000);
      child.once("exit", () => { clearTimeout(timer); resolve(); });
      child.stdin.end();
    });
  }

  async stop() {
    this.stopping = true;
    await this.stopApi();
    await run(this.pg("pg_ctl"), ["stop", "-D", this.databaseDirectory, "-m", "fast", "-w", "-t", "30"]);
  }
}

module.exports = { ManagedRuntime, atomicWrite, run };
