const {chromium}=require('C:/Users/Administrator/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');const base='http://127.0.0.1:8776',out=__dirname;
const cases=[
 ['search','帮我找出这个项目登录相关代码在哪。工作区的 auth 文件夹就是这个测试项目，请列出并读取其中相关文件后回答。'],
 ['edit','修改一个简单文件并运行测试：沙箱项目 activity_edit 的 app.py 已存在。先读取，把 greeting 返回值从 hello 改成 hello FORGE，然后运行 app.py，确保测试通过。直接执行已授权的沙箱读写和 run_python，不要计划或询问。'],
 ['retry','沙箱项目 activity_fix 的 app.py 含一个测试失败。请先运行 app.py 记录实际测试失败，再读取并修正 add 的实现，然后重新运行直到测试通过。直接使用 read_code_file、write_code_file、run_python，不用 code_loop。'],
 ['approval','请对沙箱项目 activity_edit 的 app.py 调用 code_loop 验证。这项操作需要审批，请创建真实审批并等待我的确认，不要自行改用其他工具。']
];
(async()=>{const browser=await chromium.launch({channel:'msedge',headless:true});
 await Promise.all(cases.map(async([name,message])=>{const context=await browser.newContext({viewport:{width:1440,height:900}});const page=await context.newPage();const errors=[],network=[],snapshots=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{window.__publicEvents=[];const Original=window.EventSource;window.EventSource=class extends Original{constructor(url,...args){super(url,...args);for(const type of ['activity','assistant_delta','control'])this.addEventListener(type,event=>{try{window.__publicEvents.push({received:Date.now(),...JSON.parse(event.data)})}catch{}})}}});
 await page.goto(base);await page.waitForFunction(()=>window.RT&&RT.workspace);
 const task=await page.evaluate(async n=>(await RT.http.post('/api/tasks/create',{title:'Activity E2E '+n})).task,name);
 await page.evaluate(id=>RT.workspace.openExisting(id),task.id);
 await page.locator('#composerInput').fill(message);await page.evaluate(()=>RT.workspace.send());
 let rid,state,lastType='',captured=new Set(); const start=Date.now();
 while(Date.now()-start<300000){await page.waitForTimeout(700);
 const d=await(await fetch(base+'/api/tasks/'+task.id)).json();const run=d.runs&&d.runs.at(-1);if(!run)continue;rid=run.id;state=run.state;
 const evs=await page.evaluate(()=>window.__publicEvents);const last=evs.filter(e=>e.channel==='activity').at(-1);
 if(last&&!captured.has(last.type)){captured.add(last.type);snapshots.push({type:last.type,text:await page.locator('#stream').innerText()});await page.screenshot({path:path.join(out,name+'-'+last.type+'.png')});}
 if(!['submitted','running'].includes(state))break;
 }
 await page.waitForTimeout(1000);
 const wire=await page.evaluate(()=>window.__publicEvents);const events=rid?await(await fetch(base+'/api/runs/'+rid+'/events')).json():{};
 const detail=rid?await(await fetch(base+'/api/runs/'+rid)).json():{};const text=await page.locator('#stream').innerText();
 await page.screenshot({path:path.join(out,name+'-final.png')});
 let checks={terminal:state==='completed'||state==='waiting_approval',browserErrors:errors.length===0,publicOnly:wire.every(e=>e.visibility==='public'),noReasoning:!/(我先分析|我首先需要|让我先|根据刚才的工具结果|Traceback|tool_call_id|reasoning_text)/.test(text),noRawTools:!wire.some(e=>e.arguments||e.tool_name),activity:wire.some(e=>e.channel==='activity')};
 const types=(events.events||[]).map(e=>e.type);if(name==='edit'||name==='retry')checks.verified=types.includes('verification.completed');if(name==='retry')checks.failedThenPassed=types.indexOf('verification.failed')>=0&&types.indexOf('verification.failed')<types.lastIndexOf('verification.completed');if(name==='approval')checks.waiting=state==='waiting_approval'&&wire.some(e=>e.status==='waiting_for_user');
 const answerStart=wire.find(e=>e.channel==='assistant_delta');const lastAnswer=wire.filter(e=>e.channel==='assistant_delta').at(-1);checks.streaming=name==='approval'||(!!answerStart&&!!lastAnswer&&lastAnswer.received>answerStart.received);
 fs.writeFileSync(path.join(out,name+'-result.json'),JSON.stringify({name,task:task.id,rid,state,duration:Date.now()-start,checks,errors,snapshots,text,wire,events,detail},null,2));console.log(JSON.stringify({name,state,rid,checks,duration:Date.now()-start}));await context.close();
 }));await browser.close();})().catch(e=>{console.error(e);process.exit(1)});
