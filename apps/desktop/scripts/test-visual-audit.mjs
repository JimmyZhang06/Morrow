import { _electron as electron, expect } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// A test-only main process wrapper selects the deterministic Python HTTP transport.
// Production entry points and packaged files do not contain this override.
const directory = await mkdtemp(join(tmpdir(), "morrow-visual-audit-"));
const harness = join(directory, "main.cjs");
const commands = [resolve("../../.venv/Scripts/python.exe"), resolve("../../scripts/compatible_backend_fixture.py")];
await writeFile(harness, `const { ManagedRuntime } = require(${JSON.stringify(resolve("electron/managed-runtime.cjs"))});
const original = ManagedRuntime.prototype.startApi;
ManagedRuntime.prototype.startApi = function () { this.backendCommand = ${JSON.stringify(commands)}; return original.call(this); };
require(${JSON.stringify(resolve("electron/main.cjs"))});`);
const evidence = resolve("../../.data/release-evidence/visual-audit/after");
await mkdir(evidence, { recursive: true });
const app = await electron.launch({ executablePath: resolve("node_modules/electron/dist/electron.exe"),
  args: [harness, `--user-data-dir=${join(directory, "profile")}`, "--force-device-scale-factor=1"],
  env: { ...process.env, VISTORA_MANAGED_RUNTIME: resolve("../../.data/release-runtime") }, timeout: 180000 });
try {
  const page = await app.firstWindow({timeout:180000});
  await page.getByText("记录服务已就绪",{exact:true}).waitFor({timeout:180000});
  const nav = page.getByRole("navigation", {name:"主导航"});
  for (const name of ["今天","记录","对话","认识","尝试","主线"]) {
    await nav.getByRole("button",{name,exact:true}).click();
    await page.waitForTimeout(500);
    await page.screenshot({path:join(evidence,`${name}.png`)});
  }
  await page.getByRole("button",{name:"设置与隐私",exact:true}).click();
  await page.waitForTimeout(500);
  await page.screenshot({path:join(evidence,"设置-top.png")});
  await page.locator(".stage-content").evaluate(el=>el.scrollTop=el.scrollHeight);
  await page.screenshot({path:join(evidence,"设置-bottom.png")});
  const configured = await page.evaluate(() => window.vistoraDesktop.localMaintenance({operation:"ai",enabled:true,consent:true,key:"synthetic-compatible-test",model:"vendor/model:latest",profileId:"new",name:"视觉验证模型",protocol:"compatible",baseUrl:"https://model-fixture.example/v1",jsonMode:true}));
  assert.equal(configured.ok,true);
  await page.reload();
  await page.getByText("记录服务已就绪",{exact:true}).waitFor({timeout:180000});
  await nav.getByRole("button",{name:"今天",exact:true}).click();
  const draft = page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……");
  await draft.fill("午后的散步让我把思绪慢慢理顺。回来以后，我完成了项目原型的一个小部分。");
  await draft.press("Control+Enter");
  await nav.getByRole("button",{name:"记录",exact:true}).click();
  await expect(page.locator(".record-list-item")).toHaveCount(1);
  await page.screenshot({path:join(evidence,"记录-filled.png")});
  await page.locator(".record-list-item").click();
  await page.waitForTimeout(400);
  await page.screenshot({path:join(evidence,"记录-detail.png")});
  await nav.getByRole("button",{name:"主线",exact:true}).click();
  await page.waitForTimeout(500);
  await page.screenshot({path:join(evidence,"主线-ready.png")});
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].setContentSize(980,660));
  for (const name of ["今天","记录","对话","认识","尝试","主线"]) {
    await nav.getByRole("button",{name,exact:true}).click();
    await page.waitForTimeout(400);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await page.screenshot({path:join(evidence,`${name}-compact.png`)});
  }
  console.log("Visual audit captured all main pages, record details, AI-ready narrative and compact layouts.");
} finally { await app.close(); }
