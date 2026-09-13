import { _electron as electron, expect } from '@playwright/test';
import { createServer } from 'vite';
import { mkdtemp, writeFile, mkdir, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';

const temp = await mkdtemp(join(tmpdir(), 'morrow-letter-visual-'));
const fixture = resolve('.letter-visual.html');
const output = resolve('../../.data/release-evidence/letter-review', process.env.LETTER_CAPTURE || 'after');
await mkdir(output, { recursive: true });
const reply = { answer: '你现在担心的，是再次投入之后，事情依然没有起色。读到你留下的这两段记录，我注意到一个可以一起核对的细节：那时让你重新行动的，并不是先想清楚全部答案，而是把眼前的一步变小。\n\n这不意味着今天的困难和那时一样，也不意味着散一次步就能解决它。但你曾经给自己留过一点空间，然后完成了一个具体的部分。或许今天也可以先选一件二十分钟内能结束的小事，做完再决定下一步。\n\n如果这个连接与你的感受不符，也可以放下它。这封信只是把你写过的话重新递到你面前，怎样理解，仍然由你决定。', uncertainty: '这些连接只依据所选日记，未必适用于今天的情境。', citations: [{ entry_id: 'one', fragment_id: 'one', quote: '散步回来后，我终于把项目原型的第一版做完了。原来不必一次想清楚所有问题，先让它能运行起来就好。', start: 0, end: 52 }, { entry_id: 'two', fragment_id: 'two', quote: '今天没有逼自己赶完全部任务，只认真改了一个页面。虽然慢，但看到它一点点成形，心里踏实了很多。', start: 0, end: 50 }] };
reply.citations.push({...reply.citations[0], fragment_id: 'one-second', quote: '那天只完成了一小步，但它让我愿意继续。'}, {...reply.citations[0]});
await writeFile(fixture, `<html lang="zh-CN"><meta charset="utf-8"><div id="root"></div><script type="module">import React from 'react'; import {createRoot} from 'react-dom/client'; import {PastLetter} from '/src/PastLetter.tsx'; import '/src/styles.css'; createRoot(document.getElementById('root')).render(React.createElement('main',{className:'chat-page',style:{padding:32}},React.createElement('div',{className:'chat-answer',style:{maxWidth:640,margin:'auto'}},React.createElement(PastLetter,{reply:${JSON.stringify(reply)},onSource:()=>{}}))));</script></html>`);
const server = await createServer({ server: { host: '127.0.0.1', port: 0, strictPort: true } });
await server.listen();
await writeFile(join(temp, 'main.cjs'), `const {app,BrowserWindow}=require('electron'); app.whenReady().then(()=>{const w=new BrowserWindow({width:1100,height:850,show:true,webPreferences:{nodeIntegration:false,contextIsolation:true}});w.loadURL('http://127.0.0.1:${server.httpServer.address().port}/.letter-visual.html');});`);
let app;
try {
  app = await electron.launch({ executablePath: resolve('node_modules/electron/dist/electron.exe'), args: [join(temp, 'main.cjs'), '--force-device-scale-factor=1', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', `--user-data-dir=${join(temp, 'profile')}`] });
  const page = await app.firstWindow();
  await page.getByRole('button', {name:'查看回信信封'}).waitFor();
  for (const [width,height] of [[1100,850],[980,760],[920,600]]) {
    await app.evaluate(({BrowserWindow}, size) => BrowserWindow.getAllWindows()[0].setContentSize(...size), [width,height]);
    await page.getByRole('button', {name:'查看回信信封'}).click();
    const sealedBounds = await page.locator('.letter-dialog').boundingBox();
    await page.screenshot({path:join(output,`${width}-sealed.png`)});
    await page.getByRole('button',{name:'拆开回信',exact:true}).click();
    // Sample live motion; full transition still runs normally.
    await page.waitForTimeout(1050);
    await page.screenshot({path:join(output,`${width}-flap.png`)});
    await page.waitForTimeout(1150);
    await page.screenshot({path:join(output,`${width}-extract.png`)});
    await expect(page.locator('.past-letter-paper')).toBeVisible();
    await page.screenshot({path:join(output,`${width}-unfold.png`)});
    await page.waitForTimeout(750);
    expect(await page.locator('.letter-dialog').boundingBox()).toEqual(sealedBounds);
    const layout = await page.evaluate(() => {
      const dialog = document.querySelector('.letter-dialog');
      const body = document.querySelector('.past-letter-body');
      const scroll = document.querySelector('.letter-reading-scroll');
      return { overflow: dialog.scrollWidth > dialog.clientWidth + 1,
        bodyVisible: body.getBoundingClientRect().top < scroll.getBoundingClientRect().bottom,
        actionsVisible: document.querySelector('.letter-reader-actions').getBoundingClientRect().bottom <= innerHeight };
    });
    expect(layout).toEqual({ overflow: false, bodyVisible: true, actionsVisible: true });
    await page.screenshot({path:join(output,`${width}-reading.png`)});
    console.log(JSON.stringify(await page.locator('.letter-dialog').evaluate(el=>({width:innerWidth,height:innerHeight,overflow:el.scrollWidth>el.clientWidth+1,dialog:el.getBoundingClientRect().toJSON(),paper:document.querySelector('.past-letter-paper').getBoundingClientRect().toJSON()}))));
    await page.locator('.letter-reading-scroll').evaluate(el=>el.scrollTop=el.scrollHeight);
    await page.screenshot({path:join(output,`${width}-ending.png`)});
    await expect(page.locator('.letter-source-note')).toHaveCount(2);
    await expect(page.locator('.letter-source-note').first().locator('blockquote')).toHaveCount(2);
    await expect(page.locator('.letter-originals blockquote')).toHaveCount(3);
    await expect(page.locator('.letter-originals button')).toHaveCount(2);
    await page.locator('.letter-originals-details > summary').click();
    await expect(page.locator('.letter-originals blockquote').first()).not.toBeVisible();
    await page.locator('.letter-originals-details > summary').click();
    await page.getByRole('button',{name:'关闭回信',exact:true}).click();
  }
} finally { await app?.close(); await server.close(); await unlink(fixture); }
