const {chromium}=require('C:/Users/Administrator/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright');
const fs=require('fs'); const path=require('path');
const out=__dirname; const errors=[];
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8765/'); await page.waitForTimeout(5000);
 await page.screenshot({path:path.join(out,'01-first-desktop.png')});
 fs.writeFileSync(path.join(out,'initial-dom.txt'),await page.locator('body').innerText());
 fs.writeFileSync(path.join(out,'initial-elements.json'),JSON.stringify(await page.locator('button,a,textarea').evaluateAll(es=>es.map(e=>({tag:e.tagName,id:e.id,cls:e.className,text:e.innerText,title:e.title,aria:e.getAttribute('aria-label')}))),null,2));
 fs.writeFileSync(path.join(out,'errors.json'),JSON.stringify(errors));
 await browser.close();
})();
