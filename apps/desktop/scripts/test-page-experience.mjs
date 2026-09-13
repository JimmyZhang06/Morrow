import { _electron as electron, expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

// A fresh database/profile and deterministic model transport; no personal records or real API use.
const directory = await mkdtemp(join(tmpdir(), 'morrow-page-regression-'));
const harness = join(directory, 'main.cjs');
const commands = [resolve('../../.venv/Scripts/python.exe'), resolve('../../scripts/compatible_backend_fixture.py')];
await writeFile(harness, `const {ManagedRuntime}=require(${JSON.stringify(resolve('electron/managed-runtime.cjs'))});const start=ManagedRuntime.prototype.startApi;ManagedRuntime.prototype.startApi=function(){this.backendCommand=${JSON.stringify(commands)};return start.call(this);};require(${JSON.stringify(resolve('electron/main.cjs'))});`);
const evidence = resolve('../../.data/release-evidence/page-regression/after');
await mkdir(evidence,{recursive:true});
const app = await electron.launch({executablePath:resolve('node_modules/electron/dist/electron.exe'),args:[harness,`--user-data-dir=${join(directory,'profile')}`,'--force-device-scale-factor=1'],env:{...process.env,VISTORA_MANAGED_RUNTIME:resolve('../../.data/release-runtime')},timeout:180000});
let page;
try {
  page=await app.firstWindow({timeout:180000});
  await page.getByText('记录服务已就绪',{exact:true}).waitFor({timeout:180000});
  const nav=page.getByRole('navigation',{name:'主导航'});
  const go=name=>nav.getByRole('button',{name:new RegExp(`^${name}(?:\\s*\\d+)?$`)}).click();
  const shot=async name=>{await page.screenshot({path:join(evidence,`${name}.png`)});};
  for(const name of ['今天','记录','对话','认识','尝试','主线']) {await go(name);await page.waitForTimeout(200);await shot(`${name}-empty`);}
  await go('认识');await page.getByRole('button',{name:'选择一条记录',exact:false}).click();await expect(page.locator('.records-page')).toBeVisible();
  await go('尝试');await page.getByRole('button',{name:/选择.*认识|去.*认识/}).last().click();await expect(page.locator('.insights-page')).toBeVisible();
  const configured=await page.evaluate(()=>window.vistoraDesktop.localMaintenance({operation:'ai',enabled:true,consent:true,key:'synthetic-compatible-test',model:'vendor/model:latest',profileId:'new',name:'视觉回归合成模型',protocol:'compatible',baseUrl:'https://model-fixture.example/v1',jsonMode:true}));
  assert.equal(configured.ok,true);
  await page.reload();await page.getByText('记录服务已就绪',{exact:true}).waitFor({timeout:180000});await go('今天');
  const draft=page.getByPlaceholder('写下一句话、一个感受，或刚刚发生的片段……');
  await draft.fill('今天开会前，我写下了三个要点，表达时轻松了一些。');
  await page.getByRole('button',{name:'一个停顿的瞬间',exact:true}).click();
  await expect(draft).toHaveValue(/今天开会前.*\n\n今天有一个瞬间/);
  await draft.fill('今天开会前，我写下了三个要点，表达时轻松了一些。');await draft.press('Control+Enter');
  await go('记录');await expect(page.locator('.record-list-item')).toHaveCount(1);
  await page.locator('.record-list-item').click();await expect(page.getByRole('button',{name:'发现一个线索',exact:true})).toBeEnabled({timeout:60000});
  await page.getByRole('button',{name:'发现一个线索',exact:true}).click();
  await expect(page.getByRole('button',{name:'查看认识',exact:true})).toBeVisible({timeout:60000});await go('认识');
  await expect(page.locator('.insight-list-item')).toHaveCount(1,{timeout:60000});await shot('认识-pending');
  await page.locator('.insight-list-item').click();await expect(page.getByRole('button',{name:'符合我的感受',exact:true})).toBeEnabled();await shot('认识-review');
  await page.getByRole('button',{name:'符合我的感受',exact:true}).click();
  await expect(page.getByText('这是你当前认可的理解',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'让 AI 设计一次可撤销的小尝试',exact:false}).click();
  await expect(page.locator('.action-card')).toHaveCount(1,{timeout:60000});await shot('尝试-filled');
  await go('主线');await expect(page.getByRole('button',{name:'整理第一版主线',exact:true})).toBeEnabled({timeout:15000});
  await page.getByRole('button',{name:'整理第一版主线',exact:true}).click();
  await expect(page.getByRole('heading',{name:'在表达前整理自己的想法',exact:true})).toBeVisible({timeout:60000});await shot('主线-filled');
  await page.getByRole('button',{name:'重新整理',exact:true}).click();await expect(page.getByRole('button',{name:'主线版本',exact:true})).toBeVisible({timeout:60000});
  await page.getByRole('button',{name:'主线版本',exact:true}).click();await expect(page.getByRole('option')).toHaveCount(2);await page.getByRole('option').last().click();
  await page.getByRole('button',{name:'查看材料',exact:false}).click();await expect(page.locator('.insight-tabs button.active')).toContainText('已认可');await expect(page.locator('.insight-list-item')).toHaveCount(1);await go('主线');
  await page.getByRole('button',{name:'写一章回忆录',exact:true}).click();
  await expect(page.locator('.thread-chapter-body > summary')).toContainText('给表达留一点准备的时间',{timeout:60000});await page.locator('.thread-chapter-body > summary').click();await shot('主线-chapter');
  await page.locator('.thread-calendar > summary').click();await page.getByRole('button',{name:'开始',exact:true}).click();await expect(page.getByRole('dialog',{name:'开始日期与时间',exact:true})).toBeVisible();await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'保存为待确认事项',exact:true}).click();await expect(page.locator('.thread-calendar-item')).toHaveCount(1);await page.locator('.thread-calendar-item').getByRole('button',{name:'确认',exact:true}).click();await expect(page.getByRole('button',{name:'下载 .ics',exact:true})).toBeVisible();await shot('主线-calendar');
  await page.locator('.thread-calendar-item').getByRole('button',{name:'撤销',exact:true}).click();await expect(page.locator('.thread-calendar-item')).toContainText('已撤销');
  await go('对话');await page.getByRole('textbox',{name:'这次想聊什么',exact:true}).fill('我想回看今天的表达。');
  await page.locator('.chat-consent input').check();await page.getByRole('button',{name:'开始对话',exact:true}).click();
  const question=page.getByRole('textbox',{name:'对话问题',exact:true});await expect(question).toHaveValue('我想回看今天的表达。');await expect(page.getByRole('button',{name:'发送',exact:true})).toBeEnabled();await question.press('Enter');
  await expect(page.locator('.chat-answer')).toContainText('从选定记录可以看到一个值得继续核对的线索。',{timeout:60000});await shot('对话-filled');
  await page.getByRole('button',{name:'设置与隐私',exact:true}).click();await shot('设置-filled');
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].setContentSize(980,660));
  for(const name of ['今天','记录','对话','认识','尝试','主线']) {await go(name);await page.waitForTimeout(200);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await shot(`${name}-compact`);}
  await writeFile(join(evidence,'result.json'),JSON.stringify({passed:true,model:'deterministic synthetic fixture',checks:['7 pages, empty and filled','draft append','empty-state navigation','record to insight review','generated action','narrative generation, version picker and approved material navigation','chapter and calendar confirmation/revocation','chat initial draft and reply','980x660 layouts']},null,2));
  console.log('Page experience regression passed: record, review, action, narrative, chat and compact layouts.');
} catch(error) {if(page)await page.screenshot({path:join(evidence,'failure.png')}).catch(()=>{});throw error;} finally {await app.close();}
