import {build} from 'vite';
import {_electron as electron, expect} from '@playwright/test';
import {mkdtemp,writeFile,mkdir,unlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
const html=`<html><body><div id="root"></div><script type="module">
import React from 'react';import {createRoot} from 'react-dom/client';import {AppSelect,DateTimePicker} from '/src/Picker.tsx';import '/src/styles.css';import '/src/ChineseTheme.css';
function Demo(){const [v,setV]=React.useState('5'),[d,setD]=React.useState('2028-01-31T09:30');return React.createElement('div',{style:{padding:32,width:400}},React.createElement('h2',null,'应用内控件'),React.createElement(AppSelect,{label:'预计用时',value:v,onChange:setV,options:[3,5,10,20].map(n=>({value:String(n),label:'约 '+n+' 分钟'}))}),React.createElement('br'),React.createElement(DateTimePicker,{label:'开始',value:d,onChange:setD}),React.createElement('p',{'data-testid':'value'},d),React.createElement('button',{type:'button',style:{position:'fixed',right:24,bottom:24}},'外部区域'));}createRoot(document.getElementById('root')).render(React.createElement(Demo));</script></body></html>`;
const tmp=await mkdtemp(join(tmpdir(),'morrow-picker-'));const fixture=resolve('.picker-test.html');
await writeFile(fixture,html);const out=join(tmp,'dist');
await build({base:'./',build:{outDir:out,emptyOutDir:true,rollupOptions:{input:fixture}}});
const main=join(tmp,'main.cjs');await writeFile(main,`const {app,BrowserWindow}=require('electron');app.whenReady().then(()=>{const w=new BrowserWindow({width:850,height:700});w.loadFile(${JSON.stringify(join(out,'.picker-test.html'))});});app.on('window-all-closed',()=>app.quit());`);
let app;
try{app=await electron.launch({executablePath:resolve('node_modules/electron/dist/electron.exe'),args:[main]});const page=await app.firstWindow();
 await page.getByRole('button',{name:'预计用时',exact:true}).press('ArrowDown');
 await expect(page.getByRole('listbox')).toBeVisible();await page.getByRole('listbox').press('ArrowDown');await page.getByRole('listbox').press('Enter');await expect(page.getByRole('button',{name:'预计用时',exact:true})).toContainText('10');
 await page.getByRole('button',{name:'开始',exact:true}).click();await page.getByRole('button',{name:'下个月',exact:true}).click();await page.getByRole('button',{name:'2028年2月29日',exact:true}).click();
 await page.getByRole('textbox',{name:'小时',exact:true}).fill('99');await expect(page.getByRole('button',{name:'确定',exact:true})).toBeDisabled();await page.getByRole('textbox',{name:'小时',exact:true}).fill('23');await page.getByRole('textbox',{name:'分钟',exact:true}).fill('45');
 await page.getByRole('button',{name:'2028年2月29日',exact:true}).hover();await expect(page.getByRole('button',{name:'2028年2月29日',exact:true})).toHaveCSS('background-color','rgb(75, 74, 67)');
 const evidence=resolve('../../.data/release-evidence/pickers');await mkdir(evidence,{recursive:true});await page.screenshot({path:join(evidence,'calendar.png')});
 await page.getByRole('button',{name:'确定',exact:true}).click();await expect(page.getByTestId('value')).toHaveText('2028-02-29T23:45');
 await page.getByRole('button',{name:'开始',exact:true}).click();await page.getByRole('button',{name:'2028年2月29日',exact:true}).press('ArrowRight');await page.getByRole('button',{name:'确定',exact:true}).click();await expect(page.getByTestId('value')).toHaveText('2028-03-01T23:45');
 await page.getByRole('button',{name:'开始',exact:true}).click();await page.getByRole('textbox',{name:'小时',exact:true}).fill('12');await page.getByRole('textbox',{name:'小时',exact:true}).press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0);await expect(page.getByTestId('value')).toHaveText('2028-03-01T23:45');await expect(page.getByRole('button',{name:'开始',exact:true})).toBeFocused();
 await page.getByRole('button',{name:'预计用时',exact:true}).click();await page.screenshot({path:join(evidence,'select.png')});await page.getByRole('button',{name:'外部区域',exact:true}).click();await expect(page.getByRole('listbox')).toHaveCount(0);await expect(page.locator('select,input[type=date],input[type=datetime-local]')).toHaveCount(0);
 console.log('Custom controls passed: keyboard select, leap day, date keyboard rollover, time validation, cancel, focus return and outside close.');
}finally{if(app)await app.close();await unlink(fixture);}
