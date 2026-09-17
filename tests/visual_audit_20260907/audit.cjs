const {chromium}=require('C:/Users/Administrator/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('fs'),path=require('path');const out=__dirname;const log={errors:[],blocked:[],shots:[]};
const metrics=()=>{
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0';};
 const els=[...document.querySelectorAll('body *')].filter(visible);
 const count=s=>els.filter(e=>e.matches(s)).length;
 const borders=els.filter(e=>{const s=getComputedStyle(e);return ['Top','Bottom','Left','Right'].some(k=>parseFloat(s['border'+k+'Width'])>0&&s['border'+k+'Style']!=='none');});
 const info=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return {tag:e.tagName,cls:e.className,id:e.id,text:e.innerText?.slice(0,100),size:s.fontSize,color:s.color,bg:s.backgroundColor,radius:s.borderRadius,shadow:s.boxShadow,w:r.width,h:r.height,x:r.x,y:r.y,scrollWidth:e.scrollWidth,clientWidth:e.clientWidth};};
 return {viewport:[innerWidth,innerHeight],theme:document.documentElement.dataset.theme,documentWidth:document.documentElement.scrollWidth,
 cards:count('.tl-sec,.mat-sec,.result-card,.fail-card,.approval-inline,.deliver-card,.act-card,.run-block'),panels:count('.brand,.topbar,.main,.drawer'),borderElements:borders.length,
 badges:count('.pill,.chip,.badge,.nav-badge'),icons:count('svg,.nav-ico,.logo,.rail-star,.rail-del'),dots:count('.rail-dot,.dot'),
 lines:els.filter(e=>{let s=getComputedStyle(e);return parseFloat(s.borderBottomWidth)>0&&parseFloat(s.borderTopWidth)===0}).length,
 textSizes:[...new Set(els.filter(e=>e.childNodes.length&&[...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim())).map(e=>getComputedStyle(e).fontSize))],
 colors:[...new Set(els.filter(e=>e.innerText?.trim()).map(e=>getComputedStyle(e).color))],
 samples:els.filter(e=>e.matches('.brand,.topbar,.main,.drawer,.tl-sec,.tl-title,.tl-head,.tl-note,.tl-time,.rail-title,.rail-meta,.bubble,.agent-markdown,.composer,.composer textarea,button,.settings-content,.approval-inline,.fail-card,.result-card')).map(info),
 overflow:els.filter(e=>e.scrollWidth>e.clientWidth+3&&e.clientWidth>0&&!['visible','auto','scroll'].includes(getComputedStyle(e).overflowX)).map(info),
 animations:document.getAnimations().map(a=>({name:a.animationName,state:a.playState,timing:a.effect.getTiming()}))};
};
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
 page.on('pageerror',e=>log.errors.push(e.message));
 await page.route('**/api/**',route=>{if(!['GET','HEAD'].includes(route.request().method())){log.blocked.push(route.request().url());return route.abort();}return route.continue();});
 async function snap(name){await page.waitForTimeout(900);await page.screenshot({path:path.join(out,name+'.png')});fs.writeFileSync(path.join(out,name+'.txt'),await page.locator('body').innerText());fs.writeFileSync(path.join(out,name+'.json'),JSON.stringify(await page.evaluate(metrics),null,2));log.shots.push(name);}
 await page.goto('http://127.0.0.1:8765/');await page.waitForTimeout(1800);await snap('02-list-light');
 await page.locator('.rail-item').nth(0).click();await snap('03-conversation-completed');
 await page.locator('#composerInput').fill('这是一条仅用于检查输入区的草稿，不发送。');await snap('04-composer-draft');
 await page.locator('#composerInput').fill('');
 await page.locator('.rail-item').nth(1).click();await snap('05-failed');
 if(await page.getByRole('button',{name:'查看问题',exact:true}).count()){await page.getByRole('button',{name:'查看问题',exact:true}).first().click();await snap('06-drawer-error');await page.locator('#drawerClose').click();}
 await page.locator('#navMaterials').click();await snap('07-materials');
 await page.locator('#navSettings').click();await snap('08-settings');
 await page.getByRole('button',{name:'外观',exact:true}).click();await snap('09-appearance');
 await page.evaluate(()=>{localStorage.setItem('forge.theme.main','dark')});await page.reload();await page.waitForTimeout(1200);await snap('10-list-dark');
 await page.locator('.rail-item').nth(0).click();await snap('11-conversation-dark');
 await page.evaluate(()=>{localStorage.setItem('forge.theme.main','light')});await page.reload();await page.waitForTimeout(900);
 await page.setViewportSize({width:1366,height:768});await snap('12-laptop-list');await page.locator('.rail-item').nth(0).click();await snap('13-laptop-conversation');
 await page.setViewportSize({width:390,height:844});await snap('14-mobile-conversation');
 await page.getByRole('button',{name:'任务',exact:true}).click();await snap('15-mobile-list');
 await page.getByRole('button',{name:'设置',exact:true}).last().click();await snap('16-mobile-settings');
 await page.setViewportSize({width:768,height:1024});await snap('17-tablet-settings');
 await page.setViewportSize({width:1440,height:900});
 page.once('dialog',async d=>{log.dialog={type:d.type(),message:d.message()};await d.dismiss();});await page.locator('#btnNewTask').click();
 fs.writeFileSync(path.join(out,'audit-log.json'),JSON.stringify(log,null,2));await browser.close();
})().catch(e=>{fs.writeFileSync(path.join(out,'audit-log.json'),JSON.stringify({...log,fatal:e.stack},null,2));console.error(e);process.exit(1)});
