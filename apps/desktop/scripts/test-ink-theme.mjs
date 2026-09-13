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
  await page.getByRole("button", {name:"开始对话",exact:true}).click();
  await expect(page.locator('.chat-toolbar')).toBeVisible();
  await page.getByRole('navigation',{name:'主导航'}).getByRole('button',{name:'记录',exact:true}).click();
  const samples = await page.evaluate(async () => {
    const nav=document.querySelector('nav[aria-label="主导航"]');
    [...nav.querySelectorAll('button')].find(b=>b.textContent.trim()==='对话').click();
    const frames=[];
    for(let i=0;i<30;i++) { await new Promise(requestAnimationFrame); frames.push({
      chat:!!document.querySelector('main .chat-page'),
      other:!!document.querySelector('main .today-page, main .records-page, main .insights-page'),
      animation:document.querySelector('.chat-page') ? getComputedStyle(document.querySelector('.chat-page')).animationName : null
    }); }
    return frames;
  });
  assert.ok(samples.every(f=>f.chat && !f.other && f.animation==='none'),JSON.stringify(samples));
  await expect(page.locator('.chat-toolbar')).toBeVisible();
  await expect(page.locator('.chat-setup')).toHaveCount(0);
  const evidence=resolve('../../.data/release-evidence/ink-theme');
  await mkdir(evidence,{recursive:true});
  await page.screenshot({path:join(evidence,'conversation.png')});
  await page.getByRole('button',{name:'设置与隐私',exact:true}).click();
  await expect(page.getByText('OpenAI 兼容接口 · Chat Completions',{exact:true})).toBeVisible();
  await expect(page.getByRole('combobox',{name:'API 协议'})).toHaveCount(0);
  await expect(page.getByText('Step Plan 专用',{exact:true})).toHaveCount(0);
  await page.screenshot({path:join(evidence,'settings.png')});
  await page.reload();
  await expect(page.locator('.settings-page')).toBeVisible();
  await expect(page.locator('.today-page')).toHaveCount(0);
  await page.getByRole('navigation',{name:'主导航'}).getByRole('button',{name:'今天',exact:true}).click();
  await page.screenshot({path:join(evidence,'today.png')});
  assert.ok(await page.evaluate(async()=>{ const url=getComputedStyle(document.querySelector('.app-shell')).backgroundImage.match(/url\(["']?(.*?)["']?\)/)?.[1]; if(!url)return false; const image=new Image(); image.src=url; await image.decode();return image.naturalWidth>1000;}));
  console.log('Ink background loaded. Single API protocol, preserved conversation, 30 stable navigation frames, reload route and visual screenshots passed.');
} finally { await app.close(); }
