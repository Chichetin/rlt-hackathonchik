'use strict';
(() => {
  const stage=document.getElementById('stage'),slides=[...document.querySelectorAll('.slide')];
  const main=slides.filter(s=>s.dataset.kind==='main'),backup=slides.filter(s=>s.dataset.kind==='backup');
  const motion=window.DeckMotion;
  const storage={get(k){try{return localStorage.getItem(k)}catch{return null}},set(k,v){try{localStorage.setItem(k,v)}catch{}}};
  let current=main[0],step=0,returnId=storage.get('rlt-return')||'s7',returnStep=Number(storage.get('rlt-return-step')||0);
  const maxStep=(s=current)=>s.id==='s2'?2:Math.max(0,...[...s.querySelectorAll('[data-step]')].map(n=>+n.dataset.step));
  function fit(){const scale=Math.min(innerWidth/1600,innerHeight/900);stage.style.transform=`scale(${scale})`;stage.style.left=`${(innerWidth-1600*scale)/2}px`;stage.style.top=`${(innerHeight-900*scale)/2}px`;}
  function render(){
    slides.forEach(s=>s.hidden=s!==current);
    const isMain=current.dataset.kind==='main',group=isMain?main:backup;
    document.getElementById('counter').textContent=`${isMain?'':'Q '}${group.indexOf(current)+1} / ${group.length}`;
    document.getElementById('keyboard-hint').textContent=!isMain?'← →   ·   ↑ к выступлению':current.id==='s7'?'↓ вопросы':'← →';
    document.getElementById('progress').style.width=`${(isMain?main.indexOf(current)+1:main.findIndex(s=>s.id===returnId)+1)/main.length*100}%`;
    current.querySelectorAll('[data-step]').forEach(el=>{el.classList.toggle('emphasis',+el.dataset.step===step);el.classList.toggle('step-complete',+el.dataset.step<step)});
    if(current.id==='s2'){
      ['input','result','reasons'].forEach((name,i)=>document.getElementById(`demo-${name}`).hidden=i!==step);
      current.querySelectorAll('[data-phase]').forEach(el=>{el.classList.toggle('selected',+el.dataset.phase===step);if(+el.dataset.phase===step)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current')});
    }
    const hash=`#${current.id}/${step}`;if(location.hash!==hash)history.replaceState(null,'',hash);
    storage.set('rlt-slide',`${current.id}/${step}`);document.title=`${current.dataset.title} — Митрандир`;
  }
  function show(id,wantedStep=0,remember=true){
    const target=slides.find(s=>s.id===id);if(!target)return;
    const previous=current,direction=slides.indexOf(target)<slides.indexOf(previous)?-1:1;
    if(remember&&current.dataset.kind==='main'&&target.dataset.kind==='backup'){returnId=current.id;returnStep=step;storage.set('rlt-return',returnId);storage.set('rlt-return-step',String(returnStep))}
    current=target;step=Math.max(0,Math.min(wantedStep,maxStep()));render();motion?.enter(current,previous,direction);if(step&&current.id!=='s2')motion?.accent(current,step,step);
  }
  function advance(direction){
    if((direction>0&&step<maxStep())||(direction<0&&step>0)){
      const before=step;step+=direction;render();
      if(current.id==='s2')motion?.tab(current.querySelector('.demo-panel:not([hidden])'));else motion?.accent(current,step,before);return;
    }
    const group=current.dataset.kind==='main'?main:backup,target=group[group.indexOf(current)+direction];
    if(target)show(target.id,direction<0?maxStep(target):0);
  }
  function restore(){const m=(location.hash.slice(1)||storage.get('rlt-slide')||'s1/0').match(/^([sb][1-7])(?:\/(\d+))?$/);if(m&&slides.some(s=>s.id===m[1]))show(m[1],Number(m[2]||0),false);else show('s1');}
  function returnToMain(){show(main.some(s=>s.id===returnId)?returnId:'s7',returnStep,false)}
  async function fullscreen(){try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen()}catch{document.getElementById('announcement').textContent='Используйте F11 для полного экрана.'}}
  document.addEventListener('keydown',e=>{
    if(e.ctrlKey||e.metaKey||e.altKey)return;
    const key=e.key.toLowerCase();
    const actions={arrowright:()=>advance(1),arrowleft:()=>advance(-1),arrowdown:()=>current.id==='s7'?show('b1'):advance(1),arrowup:()=>current.dataset.kind==='backup'?returnToMain():advance(-1),pagedown:()=>advance(1),pageup:()=>advance(-1),' ':()=>advance(1),home:()=>show('s1'),end:()=>show('s7'),escape:()=>{if(current.dataset.kind==='backup')returnToMain()},f:fullscreen,а:fullscreen};
    if(actions[key]){e.preventDefault();actions[key]()}
  });
  addEventListener('resize',fit);addEventListener('hashchange',restore);fit();restore();
})();
