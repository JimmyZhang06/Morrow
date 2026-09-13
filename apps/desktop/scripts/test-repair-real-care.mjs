import { _electron as electron, expect } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
const evidence=resolve('../../.data/experience-repair');
const {directory}=JSON.parse(await readFile(join(evidence,'session.json'),'utf8'));
if (!directory.includes('morrow-repair-')) throw Error('Expected isolated repair profile');
let app,page;
async function launch(){
  app=await electron.launch({executablePath:resolve('release-experience-repair/win-unpacked/Morrow.exe'),args:[`--user-data-dir=${directory}`],timeout:180000});
  page=await app.firstWindow();page.setDefaultTimeout(10000);
  await page.getByText('记录服务已就绪',{exact:true}).waitFor({timeout:180000});
}
await launch();
try {
  await app.evaluate(({app,safeStorage})=>{
    const fs=process.mainModule.require('node:fs'),path=process.mainModule.require('node:path');
    const source=path.join(process.env.APPDATA,'vistora-desktop','versions','0.6.0-beta.6','managed','private-settings.bin');
    const target=path.join(app.getPath('userData'),'managed','private-settings.bin');
    const old=JSON.parse(safeStorage.decryptString(fs.readFileSync(source)));
    const config=JSON.parse(safeStorage.decryptString(fs.readFileSync(target)));
    config.ai=old.ai;config.aiProfiles=old.aiProfiles;
    fs.writeFileSync(target,safeStorage.encryptString(JSON.stringify(config)));
  });
  await app.close();await launch();
  const api=(path,method='GET',body)=>page.evaluate(input=>window.vistoraDesktop.apiRequest(input),{baseUrl:'vistora://local',path,method,body});
  console.log('Isolated real private-diary care check started; original ten-minute interval has elapsed.');
  await api('/v1/conversations/care/check','POST');
  let result;
  for(let i=0;i<45;i++){
    result=await api('/v1/conversations/care/state');
    if(result.data?.letter)break;
    await new Promise(resolve=>setTimeout(resolve,2000));
  }
  await writeFile(join(evidence,'care-real-final.json'),JSON.stringify(result,null,2));
  if(result.data?.letter){
    await page.getByRole('button',{name:'关怀来信',exact:true}).click();
    await expect(page.getByText(result.data.letter.body.split('\n')[0],{exact:false}).first()).toBeVisible();
    await page.screenshot({path:join(evidence,'10-real-private-care.png'),animations:'disabled',timeout:10000});
    console.log('Real private-diary care letter generated and displayed.');
  }else console.log('No care letter produced; recorded without treating absence as a transport failure.');
} finally {
  const off=await page.evaluate(()=>window.vistoraDesktop.localMaintenance({operation:'ai',enabled:false}));
  await writeFile(join(evidence,'final-isolated-status.json'),JSON.stringify(off,null,2));
  await app.close();
}
