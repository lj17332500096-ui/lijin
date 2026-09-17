const {chromium}=require('C:/Users/Administrator/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('fs'),path=require('path');
const base='http://127.0.0.1:8776',out=__dirname;
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base); await page.waitForFunction(()=>window.RT&&RT.workspace);
 await page.screenshot({path:path.join(out,'00-ready.png')});
 const task=await page.evaluate(async()=> (await RT.http.post('/api/tasks/create',{title:'Activity E2E 普通问答'})).task);
 console.log('TASK',task.id);
 await page.evaluate(id=>RT.workspace.openExisting(id),task.id);
 await page.locator('#composerInput').fill('解释一下会话管理模块通常负责什么。请直接回答，不需要工具。');
 await page.evaluate(()=>RT.workspace.send());
 let rid=null;const deadline=Date.now()+180000;let state;
 while(Date.now()<deadline){
   await page.waitForTimeout(1000);
   const d=await (await fetch(base+'/api/tasks/'+task.id)).json();
   const r=d.runs&&d.runs.at(-1);if(!r)continue;rid=r.id;state=r.state;
   if(!['submitted','running'].includes(r.state))break;
 }
 await page.waitForTimeout(800);
 const events=rid?await(await fetch(base+'/api/runs/'+rid+'/events')).json():{};
 const detail=rid?await(await fetch(base+'/api/runs/'+rid)).json():{};
 fs.writeFileSync(path.join(out,'qa-result.json'),JSON.stringify({task:task.id,rid,state,events,detail,errors,text:await page.locator('#stream').innerText()},null,2));
 await page.screenshot({path:path.join(out,'01-qa.png')});
 console.log(JSON.stringify({state,rid,errors,types:(events.events||[]).map(e=>e.type)}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
