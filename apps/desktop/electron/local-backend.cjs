const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");

const LOCAL_BACKEND_URL = "vistora://local";
const CONTENT_TYPE = "application/json; charset=utf-8";

function response(status, data, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    data,
    headers: { contentType: CONTENT_TYPE, ...headers },
  };
}

function problem(status, message) {
  return response(status, { message });
}

function freshStore() {
  return { schemaVersion: 1, entries: [] };
}

function normalizeStore(value) {
  if (!value || typeof value !== "object" || value.schemaVersion !== 1
    || !Array.isArray(value.entries) || value.entries.some((entry) =>
      !entry || typeof entry.id !== "string" || typeof entry.content !== "string"
      || !Number.isInteger(entry.revision) || entry.revision < 1
      || !Number.isFinite(Date.parse(entry.captured_at)))
    || new Set(value.entries.map((entry) => entry.id)).size !== value.entries.length) {
    throw new Error("本地数据文件格式无效");
  }
  return {
    schemaVersion: 1,
    entries: value.entries,
  };
}

function normalizeHeaders(headers = {}) {
  return Object.fromEntries(
    Object.entries(headers).map(([key, value]) => [key.toLowerCase(), String(value)]),
  );
}

function expectedRevision(input) {
  const headers = normalizeHeaders(input.headers);
  const raw = headers["if-match"] || input.body?.expected_revision;
  const parsed = Number(String(raw ?? "").replaceAll('"', ""));
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function validTimestamp(value, fallback) {
  const candidate = typeof value === "string" ? value : "";
  return Number.isNaN(Date.parse(candidate)) ? fallback : candidate;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

class LocalBackend {
  constructor({ dataDirectory, serverVersion, fileSystem = fs }) {
    this.fs = fileSystem;
    this.dataDirectory = dataDirectory;
    this.dataFile = path.join(dataDirectory, "vistora-local-data.json");
    this.serverVersion = `${serverVersion}-local`;
    this.store = freshStore();
    this.ready = null;
    this.writes = Promise.resolve();
  }

  initialize() {
    if (!this.ready) this.ready = this.load();
    return this.ready;
  }

  async load() {
    await this.fs.mkdir(this.dataDirectory, { recursive: true });
    let raw;
    try {
      raw = await this.fs.readFile(this.dataFile, "utf8");
    } catch (error) {
      if (error?.code !== "ENOENT") throw new Error("无法读取本地记录，原文件未被覆盖。请检查文件权限后重启。");
      this.store = freshStore();
      await this.persist();
      return;
    }
    try {
      this.store = normalizeStore(JSON.parse(raw));
    } catch {
      const recovery = path.join(this.dataDirectory, `vistora-local-data.recovery-${crypto.randomUUID()}.json`);
      try {
        await this.fs.copyFile(this.dataFile, recovery, fs.constants.COPYFILE_EXCL);
      } catch {
        throw new Error("本地记录无法解析，恢复副本也未能保存。原文件已保留，已停止写入，请先备份数据目录。");
      }
      throw new Error("本地记录格式损坏或版本不受支持。原文件和恢复副本已保留，已停止写入，请恢复有效数据文件后重启。");
    }
  }

  enqueue(operation) {
    const run = this.writes.then(async () => {
      const previous = clone(this.store);
      try {
        return await operation();
      } catch {
        this.store = previous;
        return problem(503, "记录未能保存，原有数据未改变。请检查磁盘空间或文件权限后重试。");
      }
    });
    this.writes = run.then(() => undefined, () => undefined);
    return run;
  }

  async persist() {
    const serialized = `${JSON.stringify(this.store, null, 2)}\n`;
    const temporary = `${this.dataFile}.${crypto.randomUUID()}.tmp`;
    let handle;
    try {
      handle = await this.fs.open(temporary, "wx", 0o600);
      await handle.writeFile(serialized, "utf8");
      await handle.sync();
      await handle.close();
      handle = null;
      await this.fs.rename(temporary, this.dataFile);
    } finally {
      if (handle) await handle.close().catch(() => {});
      await this.fs.unlink(temporary).catch(() => {});
    }
  }

  async request(input = {}) {
    await this.initialize();
    await this.writes;

    const method = String(input.method || "GET").toUpperCase();
    const requestPath = String(input.path || "/");
    if (!requestPath.startsWith("/")) return problem(400, "接口路径无效");
    const url = new URL(requestPath, "http://vistora.local");
    const pathname = url.pathname;

    if (method === "GET" && pathname === "/health/live") {
      return response(200, { status: "live" });
    }
    if (method === "GET" && pathname === "/health/ready") {
      return response(200, { status: "ready" });
    }
    if (method === "GET" && pathname === "/health/capabilities") {
      return response(200, {
        api_version: "v1",
        server_version: this.serverVersion,
        features: {
          entries: true,
          entry_revisions: true,
          entry_deletion: true,
          memory_review: false,
          memory_verdicts: false,
          candidate_insights: false,
          actions: false,
          narratives: false,
          calendar_candidates: false,
          model_run_receipts: false,
        },
      });
    }

    if (pathname === "/v1/entries" && method === "GET") {
      const requestedLimit = Number(url.searchParams.get("limit") || 100);
      const limit = Number.isFinite(requestedLimit)
        ? Math.min(Math.max(Math.trunc(requestedLimit), 1), 500)
        : 100;
      const sorted = [...this.store.entries].sort((left, right) =>
        Date.parse(right.captured_at) - Date.parse(left.captured_at)
        || left.id.localeCompare(right.id));
      const fingerprint = crypto.createHash("sha256").update(JSON.stringify(sorted)).digest("hex");
      let offset = 0;
      const cursor = url.searchParams.get("cursor");
      if (cursor) {
        const match = cursor.match(/^([a-f0-9]{64}):(\d+)$/);
        if (!match || !Number.isSafeInteger(Number(match[2]))) return problem(400, "分页标记无效");
        if (match[1] !== fingerprint) return problem(409, "记录已变化，请刷新后重新加载历史");
        offset = Number(match[2]);
      }
      const items = sorted.slice(offset, offset + limit);
      const next = offset + items.length;
      return response(200, { items: clone(items), next_cursor: next < sorted.length ? `${fingerprint}:${next}` : null });
    }

    if (pathname === "/v1/entries" && method === "POST") {
      return this.enqueue(async () => {
        const body = input.body && typeof input.body === "object" ? input.body : {};
        const content = typeof body.content === "string" ? body.content.trim() : "";
        if (!content) return problem(422, "记录内容不能为空");
        const id = typeof body.client_id === "string" && body.client_id.trim()
          ? body.client_id.trim()
          : crypto.randomUUID();
        const existing = this.store.entries.find((entry) => entry.id === id);
        if (existing) {
          return response(200, {
            id: existing.id,
            revision: existing.revision,
            saved: true,
            processing: existing.processing,
          }, { etag: `"${existing.revision}"` });
        }
        const now = new Date().toISOString();
        const capturedAt = validTimestamp(body.captured_at, now);
        const entry = {
          id,
          title: null,
          content,
          captured_at: capturedAt,
          created_at: now,
          revision: 1,
          revision_id: `${id}:1`,
          source_type: body.source_type || "note",
          data_class: body.data_class || "sensitive",
          processing: { state: "ready" },
          revisions: [{
            id: `${id}:1`,
            revision: 1,
            created_at: now,
            content_mime: "text/plain; charset=utf-8",
            content,
          }],
        };
        this.store.entries.push(entry);
        await this.persist();
        return response(201, {
          id,
          revision: 1,
          saved: true,
          processing: entry.processing,
        }, { etag: '"1"' });
      });
    }

    const entryMatch = pathname.match(/^\/v1\/entries\/([^/]+)$/);
    if (entryMatch) {
      const entryId = decodeURIComponent(entryMatch[1]);
      const index = this.store.entries.findIndex((entry) => entry.id === entryId);
      if (method === "GET") {
        if (index < 0) return problem(404, "没有找到这条记录");
        const entry = this.store.entries[index];
        return response(200, clone(entry), { etag: `"${entry.revision}"` });
      }
      if (method === "PATCH") {
        return this.enqueue(async () => {
          const activeIndex = this.store.entries.findIndex((entry) => entry.id === entryId);
          if (activeIndex < 0) return problem(404, "没有找到这条记录");
          const entry = this.store.entries[activeIndex];
          const revision = expectedRevision(input);
          if (revision === null || revision !== entry.revision) {
            return problem(409, "记录版本已经变化");
          }
          const content = typeof input.body?.content === "string" ? input.body.content.trim() : "";
          if (!content) return problem(422, "记录内容不能为空");
          const nextRevision = entry.revision + 1;
          const now = new Date().toISOString();
          entry.content = content;
          entry.revision = nextRevision;
          entry.revision_id = `${entryId}:${nextRevision}`;
          entry.processing = { state: "ready" };
          entry.revisions = [...(entry.revisions || []), {
            id: `${entryId}:${nextRevision}`,
            revision: nextRevision,
            created_at: now,
            content_mime: "text/plain; charset=utf-8",
            content,
          }];
          await this.persist();
          return response(200, {
            id: entryId,
            revision: nextRevision,
            saved: true,
            processing: entry.processing,
          }, { etag: `"${nextRevision}"` });
        });
      }
      if (method === "DELETE") {
        return this.enqueue(async () => {
          const activeIndex = this.store.entries.findIndex((entry) => entry.id === entryId);
          if (activeIndex < 0) return problem(404, "没有找到这条记录");
          const entry = this.store.entries[activeIndex];
          const revision = expectedRevision(input);
          if (revision === null || revision !== entry.revision) {
            return problem(409, "记录版本已经变化");
          }
          this.store.entries.splice(activeIndex, 1);
          await this.persist();
          return response(200, {
            id: entryId,
            tombstoned: true,
            source_generation: revision + 1,
            policy_epoch: 1,
            cascade_state: "planned",
          });
        });
      }
    }

    return problem(404, "当前内置服务尚未提供这个接口");
  }
}

function createLocalBackend(options) {
  return new LocalBackend(options);
}

module.exports = { createLocalBackend, LOCAL_BACKEND_URL };
