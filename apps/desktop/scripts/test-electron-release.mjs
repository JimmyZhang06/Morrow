import { _electron as electron } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, mkdir, writeFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const directory = await mkdtemp(join(tmpdir(), "vistora-electron-release-"));
const version = JSON.parse(await readFile("package.json", "utf8")).version;
const profileDirectory = join(directory, "versions", version);
await mkdir(join(directory, "data"), { recursive: true });
const oldDataFile = join(directory, "data", "vistora-local-data.json");
const oldData = JSON.stringify({ schemaVersion: 1, entries: [{ id: "old-release-note", content: "旧版历史不能自动出现", revision: 1, captured_at: "2026-09-01T00:00:00Z" }] });
await writeFile(oldDataFile, oldData);
const artifactDirectory = resolve("../../.data/release-evidence");
await mkdir(artifactDirectory, { recursive: true });
const executablePath = resolve("release/win-unpacked/Morrow.exe");
let app;
async function launch() {
  app = await electron.launch({ executablePath, args: [`--user-data-dir=${directory}`], timeout: 180000 });
  const page = await app.firstWindow({ timeout: 180000 });
  await page.waitForLoadState("domcontentloaded");
  await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").waitFor({ timeout: 60000 });
  return page;
}
try {
  let page = await launch();
  console.log("Electron initialized");
  assert.equal(await page.title(), "Morrow");
  await page.getByText("Morrow", { exact: true }).first().waitFor();
  assert.equal(await page.locator(".tiny-mark img").evaluate(img => img.complete && img.naturalWidth > 0), true);
  const profilePaths = await app.evaluate(({ app }) => ({ user: app.getPath("userData"), session: app.getPath("sessionData") }));
  assert.equal(profilePaths.user, profileDirectory);
  assert.equal(profilePaths.session, profileDirectory);
  assert.ok(await stat(join(profileDirectory, "Local Storage")));
  assert.equal(await stat(join(directory, "Local Storage")).catch(() => null), null);
  assert.equal(await readFile(oldDataFile, "utf8"), oldData);
  const cleanState = await page.evaluate(async () => {
    const results = await Promise.all(["/v1/entries", "/v1/memories", "/v1/actions"].map(path =>
      window.vistoraDesktop.apiRequest({ baseUrl: "vistora://local", path })));
    return { results, status: await window.vistoraDesktop.localStatus(),
      entries: JSON.parse(localStorage.getItem("vistora.localEntries") || "[]"),
      actions: JSON.parse(localStorage.getItem("vistora.localActions.v2") || "[]"),
      draft: JSON.parse(localStorage.getItem("vistora.recordDraft") || '""') };
  });
  for (const response of cleanState.results) {
    assert.equal(response.ok, true, `Fresh profile API HTTP ${response.status}`);
    assert.deepEqual(response.data.items, []);
  }
  assert.deepEqual(cleanState.entries, []);
  assert.deepEqual(cleanState.actions, []);
  assert.equal(cleanState.draft, "");
  assert.equal(cleanState.status.enabled, false);
  assert.equal(cleanState.status.profiles.some(profile => profile.hasKey), false);
  assert.equal(await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").inputValue(), "");
  await page.screenshot({ path: join(artifactDirectory, "first-launch-empty.png"), fullPage: true });
  console.log("Fresh release profile: zero records, memories, actions or drafts; no API keys; AI disabled");
  if (!process.argv.includes("--clean-only")) {
  const content = "发布候选验证：今天完成了一次小尝试。";
  await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").fill(content);
  await page.evaluate(() => {
    window.__originalSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === "vistora.localEntries") throw new DOMException("Synthetic quota error", "QuotaExceededError");
      return window.__originalSetItem.call(this, key, value);
    };
  });
  await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").press("Control+Enter");
  await page.getByText("记录尚未保存，本机存储空间不足或不可写。原文仍留在输入框中，请复制保留后重试。", { exact: true }).waitFor();
  assert.equal(await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").inputValue(), content);
  await page.evaluate(() => { Storage.prototype.setItem = window.__originalSetItem; });
  await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").press("Control+Enter");
  await page.getByText(content, { exact: true }).first().waitFor();
  console.log("Record and quota protection passed");
  await page.getByRole("button", { name: "设置与隐私", exact: true }).click();
  await page.getByText("默认关闭 · 不配置也能记录", { exact: true }).waitFor();
  const saveModel = async (name, baseUrl, model, key) => {
    await page.getByRole("button", { name: "＋ 新增配置", exact: true }).click();
    await page.getByLabel("配置名称", { exact: true }).fill(name);
    await page.getByLabel("API 服务地址（Base URL）").fill(baseUrl);
    await page.getByLabel("模型名称", { exact: true }).fill(model);
    await page.getByLabel(/^API Key/).fill(key);
    await page.getByLabel(/我同意相关材料发送到以上 API 地址/).check();
    await page.getByRole("button", { name: "保存并切换到此模型" }).click();
    await page.getByText(/模型配置已启用/).waitFor({ timeout: 90000 });
  };
  // Reserved example domains and synthetic keys: configuration does not call a vendor.
  await saveModel("测试模型一", "https://one.example/v1", "vendor/model:latest", "synthetic-key-one");
  const firstId = (await page.evaluate(() => window.vistoraDesktop.localStatus())).activeProfileId;
  await saveModel("测试模型二", "https://two.example/v1", "second-model", "synthetic-key-two");
  await page.getByRole("button", { name: "编辑配置：测试模型一", exact: true }).click();
  assert.equal(await page.getByLabel(/^API Key/).inputValue(), "");
  await page.getByLabel(/我同意相关材料发送到以上 API 地址/).check();
  await page.getByRole("button", { name: "保存并切换到此模型" }).click();
  await page.getByText(/模型配置已启用/).waitFor({ timeout: 90000 });
  const modelStatus = await page.evaluate(() => window.vistoraDesktop.localStatus());
  assert.equal(modelStatus.activeProfileId, firstId);
  assert.equal(modelStatus.model, "vendor/model:latest");
  assert.equal(JSON.stringify(modelStatus).includes("synthetic-key"), false);
  const encrypted = await readFile(join(profileDirectory, "managed", "private-settings.bin"));
  assert.equal(encrypted.includes(Buffer.from("synthetic-key")), false);
  await app.close(); app = null;
  page = await launch();
  await page.getByRole("button", { name: "设置与隐私", exact: true }).click();
  await page.getByText(/当前使用：测试模型一/).waitFor();
  assert.equal((await page.evaluate(() => window.vistoraDesktop.localStatus())).hasKey, true);
  console.log("Model profiles: save two endpoints, switch without exposing keys, encrypted persistence and restart passed");
  await page.getByRole("button", { name: "编辑配置：测试模型二", exact: true }).click();
  await page.getByRole("button", { name: "删除配置", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page.getByText("配置及其密钥已删除。", { exact: true }).waitFor({ timeout: 90000 });
  const afterDelete = await page.evaluate(() => window.vistoraDesktop.localStatus());
  assert.equal(afterDelete.profiles.length, 1);
  assert.equal(afterDelete.enabled, true);
  assert.equal(afterDelete.activeProfileId, firstId);
  console.log("Inactive profile deletion preserves active configuration");
  await page.screenshot({ path: join(artifactDirectory, "settings.png"), fullPage: true });
  const backupFile = join(directory, "ui-backup.vistora");
  await app.evaluate(({ dialog }, filePath) => {
    dialog.showSaveDialog = async () => ({ canceled: false, filePath });
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [filePath] });
    dialog.showMessageBox = async () => ({ response: 1 });
  }, backupFile);
  await page.getByLabel("备份密码（至少 12 个字符，无法找回）").fill("ui-synthetic-backup-password");
  await page.getByRole("button", { name: "导出加密备份", exact: true }).click();
  await page.getByText(/加密备份已保存。请将文件和密码分开保管/).waitFor({ timeout: 90000 });
  console.log("UI backup passed");
  assert.equal((await readFile(backupFile, "utf8")).includes(content), false);
  await page.getByLabel("备份密码（至少 12 个字符，无法找回）").fill("ui-synthetic-backup-password");
  await Promise.all([
    page.waitForEvent("load", { timeout: 90000 }),
    page.getByRole("button", { name: "从备份恢复", exact: true }).click(),
  ]);
  await page.getByText(content, { exact: true }).first().waitFor({ timeout: 90000 });
  console.log("UI restore passed");
  const protectedSettings = await readFile(join(profileDirectory, "managed", "private-settings.bin"));
  assert.equal(protectedSettings.includes(Buffer.from("adminPassword")), false);
  await app.close(); app = null;
  page = await launch();
  await page.getByText(content, { exact: true }).first().waitFor();
  await page.screenshot({ path: join(artifactDirectory, "restarted.png"), fullPage: true });
  console.log("Packaged Electron: first launch, actual Windows secret protection, quota failure, record save, settings, backup/restore, restart passed");
  console.log(`Synthetic profile: ${directory}`);
  }
} catch (error) {
  console.error(error.message);
  throw error;
} finally {
  if (app) {
    let timer;
    await Promise.race([app.close(), new Promise((resolve) => { timer = setTimeout(() => { app.process().kill(); resolve(); }, 20000); })]);
    clearTimeout(timer);
  }
}
