import { _electron as electron } from '@playwright/test';
import { mkdtemp, mkdir, writeFile, readFile, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
const evidence = resolve('../../.data/experience-repair');
await mkdir(evidence,{recursive:true});
const directory = await mkdtemp(join(tmpdir(),'morrow-repair-'));
await mkdir(join(directory,'versions','0.6.0-beta.6'),{recursive:true});
const localState=JSON.parse(await readFile(join(process.env.APPDATA,'vistora-desktop','versions','0.6.0-beta.6','Local State'),'utf8'));
await writeFile(join(directory,'versions','0.6.0-beta.6','Local State'),JSON.stringify({os_crypt:localState.os_crypt}));
const executablePath=resolve('release-experience-repair/win-unpacked/Morrow.exe');
let app, page;
async function launch(){
 app=await electron.launch({executablePath,args:[`--user-data-dir=${directory}`],timeout:180000});
 page=await app.firstWindow({timeout:180000});
 await page.getByText('记录服务已就绪',{exact:true}).waitFor({timeout:180000});
}
await launch();
const profiles=await app.evaluate(async ({safeStorage,app})=>{
 const fs=process.mainModule.require('node:fs');const path=process.mainModule.require('node:path');
 const root=path.join(process.env.APPDATA,'vistora-desktop');
 const sources=[path.join(root,'versions','0.6.0-beta.6','managed','private-settings.bin'),path.join(root,'versions','0.6.0-beta.5','managed','private-settings.bin'),path.join(root,'managed','private-settings.bin')];
 let selected; const meta=[];
 for(const file of sources){if(!fs.existsSync(file))continue;try{const c=JSON.parse(safeStorage.decryptString(fs.readFileSync(file)));const ps=c.aiProfiles||[c.ai];meta.push({file,enabled:c.ai.enabled,profiles:ps.map(p=>({name:p.name,model:p.model,baseUrl:p.baseUrl,hasKey:!!p.key}))});if(!selected&&c.ai.enabled&&c.ai.key&&!String(c.ai.baseUrl).includes('.example'))selected=c;}catch{meta.push({file,locked:true});}}
 if(selected){const dst=path.join(app.getPath('userData'),'managed','private-settings.bin');const c=JSON.parse(safeStorage.decryptString(fs.readFileSync(dst)));c.ai=selected.ai;c.aiProfiles=selected.aiProfiles;fs.writeFileSync(dst,safeStorage.encryptString(JSON.stringify(c)));}
 return {meta,configured:!!selected};
});
await writeFile(join(evidence,'configuration.json'),JSON.stringify(profiles,null,2));
await app.close();await launch();
const api=async(path,method='GET',body,headers)=>page.evaluate(input=>window.vistoraDesktop.apiRequest(input),{baseUrl:'vistora://local',path,method,body,headers});
const nav=async name=>page.getByRole('navigation',{name:'主导航'}).getByRole('button',{name,exact:true}).click();
const shot=async name=>page.screenshot({path:join(evidence,name+'.png'),fullPage:true});
const save=async(name,data)=>writeFile(join(evidence,name+'.json'),JSON.stringify(data,null,2));
await save('session',{directory,profiles,status:await page.evaluate(()=>window.vistoraDesktop.localStatus())});
console.log('READY '+JSON.stringify(profiles));
while(true){
 let task;try{task=JSON.parse(await readFile(join(evidence,'command.json'),'utf8'));await unlink(join(evidence,'command.json'));}catch{await new Promise(r=>setTimeout(r,500));continue;}
 if(task.code==='STOP'){await app.close();break;}
 try{const result=await eval(`(async()=>{${task.code}\n})()`);await save('result-'+task.id,{ok:true,result});console.log('DONE '+task.id);}
 catch(e){await save('result-'+task.id,{ok:false,error:String(e),stack:e.stack});console.log('FAIL '+task.id+' '+String(e));}
}
