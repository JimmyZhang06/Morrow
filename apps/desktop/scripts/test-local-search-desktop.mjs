import { _electron as electron, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const directory = await mkdtemp(join(tmpdir(), "morrow-search-ui-"));
const evidence = resolve("../../.data/release-evidence/local-search");
await mkdir(evidence, { recursive: true });
const app = await electron.launch({ executablePath: resolve(process.argv.find(arg => arg.startsWith("--executable="))?.slice("--executable=".length) || "release/win-unpacked/Morrow.exe"),
  args: [`--user-data-dir=${directory}`, "--force-device-scale-factor=1.5"], timeout: 180000 });
try {
  const page = await app.firstWindow({ timeout: 180000 });
  const draft = page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……");
  await draft.waitFor({ timeout: 60000 });
  // The editor is available while first-run PostgreSQL is still initializing.
  // Wait for the UI's capability check before testing database-backed search.
  await page.getByText("记录服务已就绪", { exact: true }).waitFor({ timeout: 180000 });
  await draft.fill("今天完成了项目原型，很开心。");
  await draft.press("Control+Enter");
  await page.getByText("今天完成了项目原型，很开心。", { exact: true }).first().waitFor();
  await page.getByRole("navigation", { name: "主导航" }).getByRole("button", { name: "记录", exact: true }).click();
  const sync = page.getByRole("button", { name: /条记录只保存在本机/ });
  if (await sync.count()) await sync.click();
  await expect(page.locator(".is-synced")).toHaveCount(1, { timeout: 60000 });
  const panel = page.locator(".local-search-panel");
  await panel.locator(":scope > summary").click();
  await panel.getByRole("button", { name: "开启本地搜索" }).click();
  await panel.getByRole("button", { name: "补建索引" }).click();
  await expect(panel).toContainText("本轮补建完成。", { timeout: 60000 });
  await expect(panel).toContainText("建立索引 1 条");
  await panel.getByRole("textbox", { name: "搜索日记关键词" }).fill("项目");
  await panel.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(panel.locator(".local-search-hit")).toHaveCount(1);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(980, 760));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  assert.equal(overflow, false);
  await page.screenshot({ path: join(evidence, "search-results-150.png") });
  await panel.locator(".local-search-hit").click();
  await expect(page.getByRole("button", { name: "返回记录", exact: true })).toBeVisible();
  await expect(page.locator(".record-facts")).toContainText("已同步");
  await page.getByRole("button", { name: "返回记录", exact: true }).click();
  await panel.locator(":scope > summary").click();
  // Simulate a search hit outside the records already loaded by the renderer.
  const created = await page.evaluate(async () => {
    const id = crypto.randomUUID();
    return window.vistoraDesktop.apiRequest({ baseUrl: "vistora://local", path: "/v1/entries",
      method: "POST", headers: { "Idempotency-Key": id }, body: {
        client_id: id, content: "远方旅途里的一次散步", captured_at: "2025-01-01T08:00:00Z",
        memory_policy: "default", source_type: "note", data_class: "sensitive",
      } });
  });
  assert.equal(created.ok, true);
  await panel.getByRole("textbox", { name: "搜索日记关键词" }).fill("远方旅途");
  await panel.getByRole("button", { name: "搜索", exact: true }).click();
  await panel.locator(".local-search-hit").filter({ hasText: "远方旅途" }).click();
  await expect(page.getByRole("button", { name: "返回记录", exact: true })).toBeVisible();
  await expect(page.locator(".record-facts")).toContainText("已同步");
  await page.getByRole("button", { name: "返回记录", exact: true }).click();
  await panel.locator(":scope > summary").click();
  await panel.getByRole("button", { name: "关闭本地搜索" }).click();
  await expect(panel.locator(".local-search-hit")).toHaveCount(0);
  await expect(panel).toContainText("搜索索引已清除");
  await expect(panel.getByRole("button", { name: "开启本地搜索" })).toBeVisible();
  console.log("Desktop local search: explicit enable, backfill, query, revoke and compact 150% layout passed");
} finally { await app.close(); }
