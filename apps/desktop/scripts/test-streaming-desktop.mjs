import { _electron as electron, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const directory = await mkdtemp(join(tmpdir(), "morrow-stream-ui-"));
const harness = join(directory, "main.cjs");
const commands = [resolve("../../.venv/Scripts/python.exe"), resolve("../../scripts/streaming_backend_fixture.py")];
const web = resolve(process.env.MORROW_TEST_WEB || "dist/index.html");
await writeFile(harness, `const { BrowserWindow } = require("electron");
const load = BrowserWindow.prototype.loadFile;
BrowserWindow.prototype.loadFile = function(file, ...rest) { return load.call(this, String(file).endsWith('index.html') ? ${JSON.stringify(web)} : file, ...rest); };
const { ManagedRuntime } = require(${JSON.stringify(resolve("electron/managed-runtime.cjs"))});
const start = ManagedRuntime.prototype.startApi;
ManagedRuntime.prototype.startApi = function() { this.backendCommand = ${JSON.stringify(commands)}; return start.call(this); };
require(${JSON.stringify(resolve("electron/main.cjs"))});`);
const app = await electron.launch({ executablePath: resolve("node_modules/electron/dist/electron.exe"),
  args: [harness, `--user-data-dir=${join(directory, "profile")}`],
  env: { ...process.env, VISTORA_MANAGED_RUNTIME: resolve("../../.data/release-runtime") }, timeout: 180000 });
try {
  const page = await app.firstWindow({ timeout: 180000 });
  await page.getByText("记录服务已就绪", { exact: true }).waitFor({ timeout: 180000 });
  const configured = await page.evaluate(() => window.vistoraDesktop.localMaintenance({ operation: "ai",
    enabled: true, consent: true, key: "synthetic-compatible-test", model: "vendor/model:latest", profileId: "new",
    name: "流式合成测试", protocol: "compatible", baseUrl: "https://model-fixture.example/v1", jsonMode: true }));
  assert.equal(configured.ok, true, configured.error);
  await page.reload();
  await page.getByText("记录服务已就绪", { exact: true }).waitFor({ timeout: 180000 });
  await page.getByRole("navigation", { name: "主导航" }).getByRole("button", { name: "对话", exact: true }).click();
  await page.locator(".chat-consent input").check();
  await page.getByRole("button", { name: "开始对话", exact: true }).click();
  const input = page.getByRole("textbox", { name: "对话问题" });
  await input.fill("测试逐字输出");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
  await input.press("Enter");
  await expect(page.locator(".chat-stream-preview > p")).toContainText("这是一段", { timeout: 30000 });
  await expect(page.getByRole("button", { name: "停止生成", exact: true })).toBeVisible();
  await expect(page.locator(".chat-sources")).toHaveCount(0);
  const first = await page.locator(".chat-stream-preview > p").innerText();
  await expect.poll(async () => page.locator(".chat-stream-preview > p").innerText()).not.toBe(first);
  const evidence = resolve("../../.data/release-evidence/streaming");
  await mkdir(evidence, { recursive: true });
  await page.screenshot({ path: join(evidence, "stream-in-progress.png") });
  await expect(page.locator(".chat-stream-preview")).toHaveCount(0, { timeout: 30000 });
  await expect(page.locator(".chat-answer").first()).toContainText("引用只在最终校验后出现");
  await input.fill("测试停止输出");
  await input.press("Enter");
  await expect(page.locator(".chat-stream-preview")).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "停止生成", exact: true }).click();
  await expect(page.locator(".chat-stream-preview")).toHaveCount(0);
  await expect(page.locator(".chat-answer").last()).toContainText("已取消");
  await input.fill("测试连接中断");
  await input.press("Enter");
  // A fast disconnect can finish before the UI's next polling interval.
  await expect(page.locator(".chat-answer").last()).toContainText("已收到的内容已保留", { timeout: 30000 });
  await expect(page.locator(".chat-stream-preview")).toHaveCount(0);
  const count = await page.locator('.chat-turns > article').count();
  await page.reload();
  await page.getByRole('navigation', {name:'主导航'}).getByRole('button', {name:'对话',exact:true}).click();
  await page.getByRole('complementary', {name:'已保存的对话'}).getByRole('button').nth(1).click();
  await expect(page.locator('.chat-answer').last()).toContainText('这是一段', {timeout:30000});
  await expect(page.locator('.chat-answer').last()).toContainText('已收到的内容已保留');
  await page.getByRole('button', {name:'接着说',exact:true}).last().click();
  await expect(input).toHaveValue('请接着刚才未完成的回答继续，不必重复已经说过的内容。');
  await expect(page.locator('.chat-turns > article')).toHaveCount(count);
  await input.fill('测试末段缺少分隔符'); await input.press('Enter');
  await expect(page.locator('.chat-answer').last()).toContainText('引用只在最终校验后出现', {timeout:30000});
  await expect(page.locator('.chat-stream-preview')).toHaveCount(0, {timeout:30000});
  await expect(page.locator('.chat-turns > article')).toHaveCount(count + 1);
  await input.fill('测试输出截断'); await input.press('Enter');
  await expect(page.locator('.chat-turns > article')).toHaveCount(count + 2);
  await expect(page.locator('.chat-answer').last()).toContainText('已收到的内容已保留', {timeout:30000});
  await expect(page.locator('.chat-answer').last()).not.toContainText('不会自动重发');
  await expect(page.locator('.chat-answer').last()).toContainText('引用只在最终校验后出现');
  await page.getByRole('button', {name:'接着说',exact:true}).last().click();
  await input.press('Enter');
  await expect(page.locator('.chat-answer').last()).toContainText('这是接着上文的内容', {timeout:30000});
  await expect(page.locator('.chat-stream-preview')).toHaveCount(0, {timeout:30000});
  await page.screenshot({path:join(evidence,'chat-preserved-answer.png')});
  console.log('Streaming reliability passed: partial preview, stop, interruption, preserved interrupted prose after reload, EOF frame and continuation with draft context.');
} finally { await app.close(); }
