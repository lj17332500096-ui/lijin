/* Loads production / and /rt assets. Scenario APIs are intercepted fixtures;
   streaming uses the production EventSource parser against a local SSE fixture server. */
const {chromium}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const assert=require('node:assert/strict');
const out=__dirname, checks=[],errors=[],requests=[],network=[];
let browser, server;
const ok=(name,value)=>{assert.ok(value,name);checks.push(name);console.log('PASS '+name);};
const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  let connections=[];
  server=http.createServer((req,res)=>{
    res.writeHead(200,{'Content-Type':'text/event-stream','Access-Control-Allow-Origin':'*','Cache-Control':'no-cache','Connection':'keep-alive'});
    res.write(': fixture SSE connected\n\n'); connections.push(res);
    req.on('close',()=>{connections=connections.filter(r=>r!==res);});
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const sseURL=`http://127.0.0.1:${server.address().port}/events`;
  const emit=(name,payload)=>connections.forEach(r=>r.write(`event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`));
  browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:900}});
  await page.addInitScript(()=>{
    window.motionEvidence=[];
    ['animationstart','animationend','transitionrun','transitionend'].forEach(type=>document.addEventListener(type,e=>{
      if((type.startsWith('animation')||['grid-template-rows','grid-template-columns'].includes(e.propertyName))&&window.motionEvidence.length<1000)window.motionEvidence.push({event:type,name:e.animationName||e.propertyName,target:e.target.className,time:performance.now()});
    },true));
  });
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',async r=>{
    if(r.url().includes('/rt/')||r.request().resourceType()==='document') {
      try {network.push({url:r.url(),status:r.status(),sha256:crypto.createHash('sha256').update(await r.body()).digest('hex')});}catch{}
    }
  });
  let fixture=false,runState='completed',hasRun=false,message='',answer='';
  const run=()=>({id:'brand-run',state:runState,goal:message,created_at:'2026-09-08T03:00:00Z',tool_calls:[
    {id:1,tool_name:'read_workspace_file',arguments:{path:'行业报告.md'},status:'completed'},
    {id:2,tool_name:'search_documents',arguments:{query:'行业趋势'},status:'completed'}],events:[],artifacts:[]});
  const task=()=>({id:'brand-task',title:'行业报告分析 · 状态验收样例',session_id:'brand-fixture',updated_at:new Date().toISOString(),sources:[{id:'source-fixture',display_name:'行业报告.md'}],
    stat:{runs:hasRun?1:0,messages:hasRun?(answer?2:1):0,latest_run:hasRun?run():null},runs:hasRun?[run()]:[],
    messages:hasRun?[{id:1,run_id:'brand-run',role:'user',content:message},...(answer?[{id:2,run_id:'brand-run',role:'assistant',content:answer}]:[])]:[]});
  const approval={id:'approval-fixture',task_id:'brand-run',tool:'write_project_file',tool_name:'write_project_file',arguments:{path:'摘要.md'},state:'pending'};
  await page.route('**/api/**',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    if(!fixture) {if(req.method()==='GET')return route.continue();throw new Error('Unexpected real write '+p);}
    requests.push({path:p,method:req.method(),body:req.postData()});
    const json=data=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    if(p==='/api/tasks')return json({tasks:[task()],total:1});
    if(p==='/api/tasks/brand-task')return json(task());
    if(p==='/api/runs/brand-run')return json(run());
    if(p==='/api/runs/brand-run/stream')return route.fulfill({status:307,headers:{location:sseURL}});
    if(p==='/api/tasks/brand-task/runs'){
      message=req.postDataJSON().message;answer='';hasRun=true;runState='running';
      await delay(350);return json({ok:true,run_id:'brand-run'});
    }
    if(p==='/api/runs/brand-run/cancel'){runState='cancelled';return json({ok:true});}
    if(p==='/api/runs/brand-run/resume'){runState='running';return json({ok:true});}
    if(p==='/api/approvals')return json({approvals:[approval],total:1});
    if(p==='/api/approval'){await delay(120);return json({ok:true});}
    if(p==='/api/projects/brand-task')return json(task());
    if(p.endsWith('/attachments/upload'))return json({ok:true,attachment:{id:'att-fixture',attachment_scope:'message_only'}});
    if(p.endsWith('/attachments/refs'))return json({attachments:[{id:'ref-fixture',display_name:'行业报告.md',attachment_scope:'project_source'}]});
    if(req.method()!=='GET')throw new Error('Unhandled fixture mutation '+p);
    return route.continue();
  });
  const shot=async name=>{await delay(280);await page.screenshot({path:path.join(out,name+'.png')});};
  await page.goto('http://127.0.0.1:8765');await page.waitForSelector('.home-dashboard');await delay(300);
  ok('production URL and version 224',page.url()==='http://127.0.0.1:8765/'&&await page.locator('script[src*="brand-motion.js?v=224"]').count()===1);
  ok('N mark loaded from SVG',await page.locator('.home-signature .logo').evaluate(e=>getComputedStyle(e).backgroundImage.includes('brand-mark.svg')));
  const dark=await page.evaluate(()=>({bg:getComputedStyle(document.body).backgroundColor,accent:getComputedStyle(document.documentElement).getPropertyValue('--color-accent').trim()}));
  ok('brand dark background',dark.bg==='rgb(11, 18, 32)');
  await shot('01-welcome-dark');await shot('10-sidebar-open');
  await page.evaluate(()=>document.documentElement.setAttribute('data-theme','light'));
  ok('light theme retains identical brand blue',await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--color-accent').trim())===dark.accent);
  await shot('02-welcome-light');
  await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
  await page.locator('#composerInput').focus();await shot('03-input-focus');
  ok('focus has token-based restrained shadow',await page.locator('.composer').evaluate(e=>getComputedStyle(e).boxShadow!=='none'));
  await page.locator('#sidebarCollapse').click();await delay(300);
  ok('desktop collapsed after finite transition',await page.locator('#taskSidebar').evaluate(e=>getComputedStyle(e).visibility==='hidden'));
  ok('collapsed toolbar keeps N mark',await page.locator('#brandLogo').isVisible());await shot('11-sidebar-collapsed');
  await page.locator('#sidebarToggle').click();await delay(300);
  const responsive=[];
  for(const [width,height] of [[1440,900],[1366,768],[1024,768],[390,844]]){
    await page.setViewportSize({width,height});await delay(300);
    const bounds=await page.locator('.composer').boundingBox();
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    ok(`responsive no overflow ${width}`,!overflow);ok(`composer visible ${width}`,bounds.x>=0&&bounds.x+bounds.width<=width&&bounds.y+bounds.height<=height);
    responsive.push({width,height,overflow,composer:bounds});await shot(width===390?'12-mobile-390':'responsive-'+width);
  }
  await page.locator('#sidebarToggle').click();await delay(300);ok('mobile drawer opens',await page.locator('#sidebarToggle').getAttribute('aria-expanded')==='true');
  await shot('mobile-sidebar');await page.locator('#sidebarCollapse').click();await delay(300);ok('mobile drawer closes',await page.locator('#sidebarToggle').getAttribute('aria-expanded')==='false');
  await page.setViewportSize({width:1440,height:900});await delay(300);
  fixture=true;
  await page.evaluate(()=>{
    const note=document.createElement('div');note.id='fixture-label';note.textContent='状态验收样例 · 模拟 API / 本地 SSE';
    note.style.cssText='position:fixed;right:60px;top:18px;z-index:200;color:var(--text-muted);font-size:11px;pointer-events:none';document.body.appendChild(note);
  });
  await page.evaluate(()=>RT.workspace.openExisting('brand-task'));
  await page.locator('#composerInput').fill('请分析这份行业报告，提炼关键趋势，并用 Markdown 列出三条建议。');
  const composerBefore=await page.locator('.composer').boundingBox();
  await page.locator('#sendBtn').click();
  ok('sending phase while POST pending',await page.locator('#sendBtn').getAttribute('data-state')==='sending');await page.screenshot({path:path.join(out,'sending-pending.png')});
  await page.waitForFunction(()=>document.querySelector('#sendBtn').dataset.state==='running');
  for(let i=0;i<30&&!connections.length;i++)await delay(50);
  ok('native EventSource connected to local fixture',connections.length>0);
  emit('run.started',{run_id:'brand-run'});emit('tool',{name:'read_workspace_file'});
  await shot('04-user-message-sent');
  ok('user message displayed once',await page.locator('.msg-row.user').count()===1);
  ok('running offers real stop action',await page.locator('#sendBtn').getAttribute('aria-label')==='停止当前任务');
  await shot('05-agent-running');
  // Observe continuous deltas through native SSE, not direct DOM insertion.
  await page.evaluate(()=>{window.frameTimes=[];window.frameMeasure=true;let prev=performance.now();function frame(t){if(!window.frameMeasure)return;window.frameTimes.push(t-prev);prev=t;requestAnimationFrame(frame);}requestAnimationFrame(frame);});
  const parts=['## 关键趋势\n\n','| 方向 | 建议 |\n| --- | --- |\n| 用户需求 | 聚焦实际任务 |\n\n','- 简化操作路径。\n- 让结果清晰可见。\n- 保留确认与停止入口。\n\n'];
  for(const text of parts){answer+=text;emit('reply_delta',{text});await delay(90);}
  ok('streaming Markdown renders table',await page.locator('.md-table').count()===1);
  const initialLength=await page.locator('.live-markdown').innerText().then(t=>t.length);
  for(let i=0;i<70;i++){const text='补充观察：让每一次交互保持清晰、稳定，结果可验证。\n\n';answer+=text;emit('reply_delta',{text});await delay(20);}
  await delay(100);
  ok('continuous deltas update before stream closes',(await page.locator('.live-markdown').innerText()).length>initialLength+100);
  const perf=await page.evaluate(()=>{window.frameMeasure=false;const values=window.frameTimes.filter(n=>n>0).sort((a,b)=>a-b);return {frames:values.length,meanMs:values.reduce((a,b)=>a+b,0)/values.length,p95Ms:values[Math.floor(values.length*.95)],over50ms:values.filter(n=>n>50).length};});
  ok('streaming frame budget under 40ms p95',perf.p95Ms<40);
  const composerDuring=await page.locator('.composer').boundingBox();
  ok('streaming does not move composer',Math.abs(composerDuring.y-composerBefore.y)<2);
  // Completion: keep a concise answer for screenshot and confirm folded summary persists.
  answer=parts.join('');emit('reply',{content:answer});runState='completed';emit('run.completed',{run_id:'brand-run'});
  await page.waitForSelector('.run-proc.ok');await delay(400);
  ok('completion produces collapsed summary',await page.locator('.rp-head').getAttribute('aria-expanded')==='false');
  ok('final answer keeps Markdown table',await page.locator('.md-table').count()===1);
  ok('cursor removed at completion',await page.locator('.live-markdown').count()===0);
  await shot('07-execution-completed-collapsed');
  await page.locator('.rp-head').click();
  ok('fold has height/opacity transition',await page.locator('.rp-body').evaluate(e=>getComputedStyle(e).transitionProperty.includes('grid-template-rows')));
  await shot('06-execution-expanded');ok('fold accessible expanded state',await page.locator('.rp-head').getAttribute('aria-expanded')==='true');
  await page.locator('.rp-head').click();await delay(280);
  ok('fold content inert when collapsed',await page.locator('.rp-body').evaluate(e=>e.inert));
  // A second real controller submission, followed by approval and resume HTTP actions.
  await page.locator('#composerInput').fill('请保存摘要，修改前让我确认。');await page.locator('#sendBtn').click();
  await page.waitForFunction(()=>document.querySelector('#sendBtn').dataset.state==='running');await delay(150);
  runState='waiting_approval';emit('approval',{task_id:'brand-run',approvals:[approval]});
  await page.waitForSelector('.approval-inline');await shot('08-approval');
  await page.locator('.approval-inline').last().getByRole('button',{name:'继续',exact:true}).click();await delay(250);
  ok('approval posts approved then resumes',requests.some(r=>r.path==='/api/approval'&&r.body.includes('approved'))&&requests.some(r=>r.path==='/api/runs/brand-run/resume'));
  ok('approval visibly becomes confirmed',(await page.locator('.approval-inline.is-approved').innerText()).includes('已确认'));await shot('approval-confirmed');
  runState='failed';emit('run.failed',{run_id:'brand-run'});await page.waitForSelector('.fail-card');await shot('09-error');
  ok('error keeps retry and details actions',await page.locator('.fail-card button').count()===2);
  await page.locator('.fail-card').getByRole('button',{name:'查看问题',exact:true}).click();await delay(250);
  ok('drawer opens with accessible state',await page.locator('#detailDrawer').getAttribute('aria-hidden')==='false');await shot('details-drawer');
  await page.locator('#drawerClose').click();await delay(250);
  ok('drawer hides from keyboard navigation',await page.locator('#detailDrawer').evaluate(e=>e.inert));
  await page.locator('#composerInput').fill('测试停止当前执行。');await page.locator('#sendBtn').click();await page.waitForFunction(()=>document.querySelector('#sendBtn').dataset.state==='running');
  await page.locator('#sendBtn').click();await delay(500);
  ok('stop button sends actual cancel endpoint',requests.some(r=>r.path==='/api/runs/brand-run/cancel'));
  ok('stopped state visible',(await page.locator('#stream').innerText()).includes('已停止'));
  await page.locator('#btnComposerFile').click();const chooser=page.waitForEvent('filechooser');await page.locator('[data-kind="upload"]').click();
  await (await chooser).setFiles({name:'报告.txt',mimeType:'text/plain',buffer:Buffer.from('fixture')});await delay(150);
  ok('attachment still uploads through production client',(await page.locator('#composerChips').innerText()).includes('报告.txt'));
  await page.locator('#btnComposerFile').click();await page.locator('[data-kind="source"]').click();await page.locator('#composerFileMenu').getByRole('button',{name:'[ ] 行业报告.md',exact:true}).click();await page.locator('#composerFileMenu').getByRole('button',{name:/添加所选来源/}).click();await delay(150);
  ok('source reference attaches',requests.some(r=>r.path.endsWith('/attachments/refs')));
  await page.evaluate(()=>RT.workspace.goHome());await page.waitForSelector('.home-dashboard');ok('task switch restores home',await page.locator('.home-dashboard').isVisible());
  await page.setViewportSize({width:390,height:844});await delay(300);await page.locator('#composerInput').focus();
  await page.evaluate(()=>{Object.defineProperty(visualViewport,'height',{configurable:true,get:()=>440});visualViewport.dispatchEvent(new Event('resize'));});await delay(200);
  ok('simulated keyboard resizes shell',await page.locator('.app').evaluate(e=>Math.abs(e.getBoundingClientRect().height-440)<2));
  ok('simulated keyboard leaves composer above keyboard',await page.locator('.composer').evaluate(e=>e.getBoundingClientRect().bottom<=440));await shot('mobile-keyboard-simulation');
  await page.evaluate(()=>{delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'));});
  await page.emulateMedia({reducedMotion:'reduce'});await page.setViewportSize({width:1440,height:900});await delay(300);
  ok('reduced motion disables nonessential transitions',await page.locator('.composer').evaluate(e=>getComputedStyle(e).transitionDuration==='0s'));
  await page.locator('.home-shortcut').first().hover();ok('reduced motion disables hover movement',await page.locator('.home-shortcut').first().evaluate(e=>getComputedStyle(e).transform==='none'));
  await shot('reduced-motion');ok('browser has no uncaught errors',errors.length===0);
  const motionEvidence=await page.evaluate(()=>window.motionEvidence);
  ok('browser ran new-message animation',motionEvidence.some(e=>e.event==='animationstart'&&e.name==='message-enter'));
  ok('browser completed message animation',motionEvidence.some(e=>e.event==='animationend'&&e.name==='message-enter'));
  ok('browser ran actual execution fold transition',motionEvidence.some(e=>e.event==='transitionrun'&&e.name==='grid-template-rows'));
  fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({productionURL:page.url(),fixtureDescription:'Actual production HTML/CSS/JS and HTTP client; deterministic API responses plus native EventSource local fixture, no real provider/tool mutation.',checks,errors,requests,network,responsive,performance:perf,motionEvidence},null,2));
  console.log('BRAND_SCENARIOS_PASS '+checks.length);
})().catch(e=>{console.error(e);fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({error:String(e),checks,errors,requests},null,2));process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server){server.closeAllConnections();server.close();}});
