import fs from 'node:fs';import path from 'node:path';import {fileURLToPath,pathToFileURL} from 'node:url';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'../../src/ui/node_modules/playwright-core/index.mjs');
const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(here,'..'),out=path.join(here,'screens');fs.mkdirSync(out,{recursive:true});
const browser=await chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:1600,height:900},offline:true}),page=await context.newPage();
const errors=[],network=[],checks=[],layouts=[];page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(/^https?:/.test(r.url()))network.push(r.url())});
function check(ok,name){checks.push({name,passed:!!ok});if(!ok)throw Error(name)}
async function scene(id,step=0){await page.evaluate(v=>location.hash=v,[id,step].join('/'));await page.waitForFunction(id=>!document.getElementById(id).hidden,id)}
await page.emulateMedia({reducedMotion:'reduce'});await page.goto(pathToFileURL(path.join(root,'index.html')).href+'#s1/0');await page.evaluate(()=>document.fonts.ready);
check(await page.locator('.slide[data-kind=main]').count()===7,'7 main slides');check(await page.locator('.slide[data-kind=backup]').count()===5,'5 backup slides');check(await page.locator('.slide').evaluateAll(xs=>xs.reduce((n,x)=>n+(+x.dataset.seconds),0))===300,'300 seconds of content');
check(await page.locator('#stage').getAttribute('data-reserve-seconds')==='0','No time outside the 300-second script');check(await page.locator('button,dialog').count()===0,'No on-screen buttons or modes');check(await page.locator('.source').count()===0,'No internal source footers');check(await page.locator('img[src$=".png"]').count()===0,'No bitmap UI screenshots in the deck');
for(const id of ['s1','s2','s3','s4','s5','s6','s7','b1','b2','b3','b4','b5']){
 for(const step of ['s2','s3','s5'].includes(id)?[0,1,2,3]:[0]){
  await scene(id,step);await page.evaluate(()=>document.fonts.ready);await page.waitForTimeout(100);await page.screenshot({path:path.join(out,`${id}${['s2','s3','s5'].includes(id)?'-'+step:''}.png`)});
  const issues=await page.evaluate(id=>{const el=document.getElementById(id),b=el.getBoundingClientRect(),issues=[],w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);while(w.nextNode()){const n=w.currentNode;if(!n.textContent.trim()||n.parentElement.closest('[hidden]'))continue;const r=document.createRange();r.selectNodeContents(n);for(const q of r.getClientRects())if(q.width&&q.height&&(q.left<b.left-2||q.right>b.right+2||q.top<b.top-2||q.bottom>b.bottom+2))issues.push(n.textContent.trim())}return issues},id);
  layouts.push({id,step,issues});check(issues.length===0,`${id}/${step}: all text within safe area`);
 }
}
await page.keyboard.press('Home');await page.keyboard.press('ArrowRight');check(await page.locator('#s2').isVisible(),'S2 introduces supplier candidate pipeline');
for(const id of ['s2','s3']){
 for(let i=1;i<=3;i++){await page.keyboard.press('ArrowRight');check(await page.locator(`#${id} .emphasis`).first().getAttribute('data-step')===String(i),`${id}: step ${i}`)}
 await page.reload();check(await page.locator(`#${id} .emphasis`).first().getAttribute('data-step')==='3',`${id}: refresh preserves emphasis`);await page.keyboard.press('ArrowRight');
}
for(let i=1;i<=5;i++){await page.keyboard.press('ArrowRight');check(await page.locator('#s4 .emphasis').getAttribute('data-step')===String(i),`Architecture step ${i}`)}
await page.keyboard.press('ArrowRight');for(let i=1;i<=3;i++){await page.keyboard.press('ArrowRight');check(await page.locator('#s5 .emphasis').getAttribute('data-step')===String(i),`Filter step ${i}`)}
check(await page.locator('#s2 .demo-panel').count()===0,'Old UI walkthrough removed');
await page.keyboard.press('End');await page.keyboard.press('ArrowRight');check(await page.locator('#s7').isVisible(),'Main route stops at final');await page.keyboard.press('ArrowDown');check(await page.locator('#b1').isVisible(),'Down explicitly opens Q&A');for(let i=2;i<=5;i++){await page.keyboard.press('ArrowRight');check(await page.locator('#b'+i).isVisible(),`Arrow navigation to backup ${i}`)}await page.reload();await page.keyboard.press('ArrowUp');check(await page.locator('#s7').isVisible(),'Up returns from Q&A after refresh');
await page.keyboard.press('n');await page.keyboard.press('a');check(await page.locator('dialog,button').count()===0,'Removed mode shortcuts have no UI');
await page.keyboard.press('f');await page.waitForFunction(()=>!!document.fullscreenElement);check(await page.evaluate(()=>!!document.fullscreenElement),'Keyboard fullscreen');await page.keyboard.press('f');await page.waitForFunction(()=>!document.fullscreenElement);
for(const size of [{width:1280,height:720},{width:1024,height:768},{width:2048,height:1180}]){await page.setViewportSize(size);await page.waitForTimeout(60);check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&document.documentElement.scrollHeight<=innerHeight),`${size.width}×${size.height}: fits viewport`)}
check(errors.length===0,'No JS errors');check(network.length===0,'No external requests');
fs.writeFileSync(path.join(here,'browser-report.json'),JSON.stringify({checked_at:new Date().toISOString(),browser:await browser.version(),checks,layouts,errors,network},null,2)+'\n');await browser.close();console.log(JSON.stringify({checks:checks.length,passed:true}));
