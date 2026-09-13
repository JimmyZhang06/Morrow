import { _electron as electron, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// A test-only main process wrapper selects the deterministic Python HTTP transport.
// Production entry points and packaged files do not contain this override.
const directory = await mkdtemp(join(tmpdir(), "morrow-care-ui-"));
const harness = join(directory, "main.cjs");
const commands = [resolve("../../.venv/Scripts/python.exe"), resolve("../../scripts/compatible_backend_fixture.py")];
await writeFile(harness, `const { ManagedRuntime } = require(${JSON.stringify(resolve("electron/managed-runtime.cjs"))});
const original = ManagedRuntime.prototype.startApi;
ManagedRuntime.prototype.startApi = function () { this.backendCommand = ${JSON.stringify(commands)}; return original.call(this); };
require(${JSON.stringify(resolve("electron/main.cjs"))});`);
const evidence = resolve("../../.data/release-evidence/care-letters");
await mkdir(evidence, { recursive: true });
const app = await electron.launch({ executablePath: resolve("node_modules/electron/dist/electron.exe"),
  args: [harness, `--user-data-dir=${join(directory, "profile")}`, "--force-device-scale-factor=1"],
  env: { ...process.env, VISTORA_MANAGED_RUNTIME: resolve("../../.data/release-runtime") }, timeout: 180000 });
try {
  const page = await app.firstWindow({ timeout: 180000 });
  await page.getByText("记录服务已就绪", { exact: true }).waitFor({ timeout: 180000 });
  const configured = await page.evaluate(() => window.vistoraDesktop.localMaintenance({ operation: "ai",
    enabled: true, consent: true, key: "synthetic-compatible-test", model: "vendor/model:latest", profileId: "new",
    name: "合成验证模型", protocol: "compatible", baseUrl: "https://model-fixture.example/v1", jsonMode: true }));
  assert.equal(configured.ok, true, JSON.stringify(configured));
  await page.reload();
  await page.getByText("记录服务已就绪", { exact: true }).waitFor({ timeout: 180000 });
  await page.evaluate(() => { Date.prototype.getHours = () => 12; });
  await page.getByRole("button", {name:"关怀来信", exact:true}).click();
  await expect(page.getByLabel("接受关怀来信", {exact:true})).not.toBeChecked();
  await page.getByLabel("接受关怀来信", {exact:true}).check();
  await expect(page.getByLabel("来信如何出现", {exact:true})).toBeVisible();
  await page.getByLabel("来信如何出现", {exact:true}).selectOption("inbox");
  await expect(page.getByLabel("来信如何出现", {exact:true})).toBeEnabled();
  await page.getByRole("button", {name:"关闭来信",exact:true}).click();
  const sent = await page.evaluate(async () => {
    const api = input => window.vistoraDesktop.apiRequest({baseUrl:"vistora://local", ...input});
    const chat = await api({path:"/v1/conversations",method:"POST",body:{entry_ids:[],allow_history:true}});
    if (!chat.ok) return chat;
    return api({path:`/v1/conversations/${chat.data.id}/turns`,method:"POST",body:{question:"关怀验证：最近连续几天都很累，有些事情压得我喘不过气。",request_id:crypto.randomUUID()}});
  });
  assert.equal(sent.ok,true,JSON.stringify(sent));
  await expect.poll(async () => (await page.evaluate(() => window.vistoraDesktop.apiRequest({baseUrl:"vistora://local",path:"/v1/conversations/care/state"}))).data?.letter, {timeout:60000}).toBeTruthy();
  await expect(page.getByRole("button", {name:"来信，有一封待读信"})).toBeVisible({timeout:25000});
  await expect(page.locator(".care-note")).toHaveCount(0);
  await expect(page.locator(".care-dialog")).not.toBeVisible();
  await page.getByRole("button", {name:"来信，有一封待读信"}).click();
  await expect(page.locator(".care-paper")).toContainText("今天不必把所有事情都处理好");
  await expect(page.locator(".care-dialog")).toHaveCSS("opacity", "1");
  await page.screenshot({path:join(evidence,"letter-reader.png")});
  await page.getByLabel("来信如何出现", {exact:true}).selectOption("gentle");
  await expect(page.getByLabel("来信如何出现", {exact:true})).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(page.locator(".care-note")).toBeVisible();
  await expect(page.locator(".care-note")).toHaveCSS("opacity", "1");
  await page.screenshot({path:join(evidence,"gentle-arrival.png")});
  const bounds = await page.locator(".care-note").boundingBox();
  assert.ok(bounds.height < 95 && bounds.width < 350);
  await page.getByRole("button", {name:"稍后再看"}).click();
  await expect(page.locator(".care-note")).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", {name:"来信，有一封待读信"}).click({timeout:60000});
  await expect(page.locator(".care-paper")).toContainText("今天不必把所有事情都处理好");
  await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].setContentSize(920,600));
  await expect(page.locator(".care-dialog")).toHaveCSS("opacity", "1");
  await page.screenshot({path:join(evidence,"compact-reader.png")});
  assert.ok(await page.locator(".care-dialog").evaluate(el => el.scrollWidth <= el.clientWidth + 1));
  await page.getByRole("button", {name:"暂时不想收，暂停七天"}).click();
  await expect(page.getByRole("heading", {name:"给你留一些安静的时间"})).toBeVisible();
  await page.getByRole("button", {name:"恢复来信",exact:true}).click();
  await expect(page.getByRole("heading", {name:"信箱暂时是空的"})).toBeVisible();
  await page.getByLabel("接受关怀来信", {exact:true}).uncheck();
  await expect(page.getByRole("heading", {name:"偶尔，有一封给你的信"})).toBeVisible();
  console.log("Care desktop passed: opt-in, model-generated encrypted letter, inbox-only, gentle arrival, no auto dialog, later, reload, compact layout, pause/resume and opt-out.");
} catch (error) { const p = await app.firstWindow(); console.log(await p.locator("body").innerText()); throw error; } finally { await app.close(); }
