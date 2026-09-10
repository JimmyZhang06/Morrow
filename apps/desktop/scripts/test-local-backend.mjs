import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs, { mkdtemp, rm, readFile, writeFile, readdir, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { transformWithOxc } from "vite";

const require = createRequire(import.meta.url);
const { createLocalBackend } = require("../electron/local-backend.cjs");
const paginationSource = await readFile(new URL("../src/pagination.ts", import.meta.url), "utf8");
const { code: paginationJs } = await transformWithOxc(paginationSource, "pagination.ts");
const { collectPages } = await import(`data:text/javascript;base64,${Buffer.from(paginationJs).toString("base64")}`);

const testDirectory = await mkdtemp(join(tmpdir(), "vistora-local-backend-"));

try {
  const backend = createLocalBackend({ dataDirectory: testDirectory, serverVersion: "test" });
  await backend.initialize();

  assert.equal((await backend.request({ path: "/health/ready" })).ok, true);
  const capabilities = await backend.request({ path: "/health/capabilities" });
  assert.equal(capabilities.data.features.entries, true);

  const created = await backend.request({
    path: "/v1/entries",
    method: "POST",
    body: { client_id: "entry-1", content: "第一条可分发版本记录" },
  });
  assert.equal(created.status, 201);

  const page = await backend.request({ path: "/v1/entries?limit=100" });
  assert.equal(page.data.items.length, 1);
  assert.equal(page.data.items[0].content, "第一条可分发版本记录");

  const revised = await backend.request({
    path: "/v1/entries/entry-1",
    method: "PATCH",
    headers: { "If-Match": '"1"' },
    body: { content: "修订后的记录", expected_revision: 1 },
  });
  assert.equal(revised.data.revision, 2);

  const conflict = await backend.request({
    path: "/v1/entries/entry-1",
    method: "PATCH",
    headers: { "If-Match": '"1"' },
    body: { content: "过期修订", expected_revision: 1 },
  });
  assert.equal(conflict.status, 409);

  const restarted = createLocalBackend({ dataDirectory: testDirectory, serverVersion: "test" });
  await restarted.initialize();
  const persisted = await restarted.request({ path: "/v1/entries/entry-1" });
  assert.equal(persisted.data.content, "修订后的记录");

  const deleted = await restarted.request({
    path: "/v1/entries/entry-1",
    method: "DELETE",
    headers: { "If-Match": '"2"' },
  });
  assert.equal(deleted.ok, true);
  assert.equal((await restarted.request({ path: "/v1/entries" })).data.items.length, 0);

  // More than one page, including identical timestamps, survives a restart.
  for (let i = 0; i < 205; i++) {
    assert.equal((await restarted.request({ path: "/v1/entries", method: "POST",
      body: { client_id: `history-${i}`, content: `记录 ${i}`, captured_at: "2026-09-01T00:00:00Z" },
    })).status, 201);
  }
  const historyBackend = createLocalBackend({ dataDirectory: testDirectory, serverVersion: "test" });
  const history = await collectPages((cursor) => historyBackend.request({
    path: `/v1/entries?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  }));
  assert.equal(history.data.items.length, 205);
  assert.equal(new Set(history.data.items.map((entry) => entry.id)).size, 205);
  const firstPage = await historyBackend.request({ path: "/v1/entries?limit=100" });
  await historyBackend.request({ path: "/v1/entries", method: "POST", body: { content: "新增" } });
  assert.equal((await historyBackend.request({ path: `/v1/entries?cursor=${firstPage.data.next_cursor}` })).status, 409);
  assert.equal((await historyBackend.request({ path: "/v1/entries?cursor=broken" })).status, 400);

  // A failed later page must never become a successful partial history.
  const failure = { ok: false, status: 503, data: { message: "unavailable" }, headers: {} };
  const partial = await collectPages(async (cursor) => cursor ? failure :
    { ok: true, status: 200, data: { items: [1], next_cursor: "next" }, headers: {} });
  assert.equal(partial, failure);
  const looping = await collectPages(async () =>
    ({ ok: true, status: 200, data: { items: [1], next_cursor: "same" }, headers: {} }));
  assert.equal(looping.ok, false);

  // Inject disk failures before the commit point; neither memory nor disk changes.
  const diskFile = join(testDirectory, "vistora-local-data.json");
  let failAt = null;
  const failingFs = { ...fs,
    open: async (...args) => {
      const handle = await fs.open(...args);
      return {
        writeFile: async (...values) => {
          if (failAt === "write") { await handle.writeFile("partial"); throw new Error("disk full"); }
          return handle.writeFile(...values);
        },
        sync: async () => { if (failAt === "sync") throw new Error("sync failed"); return handle.sync(); },
        close: () => handle.close(),
      };
    },
    rename: async (...args) => { if (failAt === "rename") throw new Error("locked"); return fs.rename(...args); },
  };
  const faultBackend = createLocalBackend({ dataDirectory: testDirectory, serverVersion: "test", fileSystem: failingFs });
  await faultBackend.initialize();
  for (const stage of ["write", "sync", "rename"]) {
    const before = await readFile(diskFile, "utf8");
    failAt = stage;
    const result = await faultBackend.request({ path: "/v1/entries", method: "POST",
      body: { client_id: `failed-${stage}`, content: "不能假装保存成功" } });
    assert.equal(result.status, 503);
    assert.equal(await readFile(diskFile, "utf8"), before);
    assert.equal((await faultBackend.request({ path: `/v1/entries/failed-${stage}` })).status, 404);
    assert.equal((await readdir(testDirectory)).some((name) => name.endsWith(".tmp")), false);
    failAt = null;
    assert.equal((await faultBackend.request({ path: "/v1/entries", method: "POST",
      body: { client_id: `failed-${stage}`, content: "重试保存成功" } })).status, 201);
  }
  for (const method of ["PATCH", "DELETE"]) {
    failAt = "rename";
    const before = await readFile(diskFile, "utf8");
    assert.equal((await faultBackend.request({ path: "/v1/entries/failed-write", method,
      headers: { "If-Match": '"1"' }, body: { content: "未保存的修订" } })).status, 503);
    assert.equal(await readFile(diskFile, "utf8"), before);
    assert.equal((await faultBackend.request({ path: "/v1/entries/failed-write" })).data.content, "重试保存成功");
  }
  failAt = null;

  // Corruption, unsupported versions, and access errors never reset existing data.
  const recoveryDirectory = join(testDirectory, "recovery-case");
  await mkdir(recoveryDirectory);
  const recoveryFile = join(recoveryDirectory, "vistora-local-data.json");
  for (const raw of ["{broken", JSON.stringify({ schemaVersion: 2, entries: [] }),
    JSON.stringify({ schemaVersion: 1, entries: [null] })]) {
    await writeFile(recoveryFile, raw);
    const damaged = createLocalBackend({ dataDirectory: recoveryDirectory, serverVersion: "test" });
    await assert.rejects(damaged.initialize(), /已停止写入/);
    assert.equal(await readFile(recoveryFile, "utf8"), raw);
  }
  const backupNames = (await readdir(recoveryDirectory)).filter((name) => name.includes(".recovery-"));
  assert.equal(backupNames.length, 3);
  const damagedRaw = await readFile(recoveryFile, "utf8");
  const backupFailure = createLocalBackend({ dataDirectory: recoveryDirectory, serverVersion: "test",
    fileSystem: { ...fs, copyFile: async () => { throw new Error("no space"); } } });
  await assert.rejects(backupFailure.initialize(), /恢复副本也未能保存/);
  assert.equal(await readFile(recoveryFile, "utf8"), damagedRaw);
  const readFailure = createLocalBackend({ dataDirectory: recoveryDirectory, serverVersion: "test",
    fileSystem: { ...fs, readFile: async () => { throw Object.assign(new Error("denied"), { code: "EACCES" }); } } });
  await assert.rejects(readFailure.initialize(), /原文件未被覆盖/);
  assert.equal(await readFile(recoveryFile, "utf8"), damagedRaw);

  // A verified valid file can be restored explicitly, including after an interrupted write.
  const validBackup = await readFile(diskFile, "utf8");
  await writeFile(recoveryFile, validBackup);
  await writeFile(`${recoveryFile}.interrupted.tmp`, "partial");
  const restored = createLocalBackend({ dataDirectory: recoveryDirectory, serverVersion: "test" });
  assert.equal((await restored.request({ path: "/v1/entries/failed-write" })).data.content, "重试保存成功");

  console.log("Local backend contract passed");
} finally {
  const safeRoot = resolve(tmpdir());
  const safeTarget = resolve(testDirectory);
  if (safeTarget.startsWith(`${safeRoot}\\`) || safeTarget.startsWith(`${safeRoot}/`)) {
    await rm(safeTarget, { recursive: true, force: true });
  }
}
