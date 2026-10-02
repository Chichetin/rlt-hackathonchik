'use strict';
/* Finite slide transitions; continuous current only while the architecture is visible. */
(() => {
  const stage = document.getElementById('stage');
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  const NS = 'http://www.w3.org/2000/svg';
  const running = new Set();
  let disabled = false, ghost = null, active = null;
  const ease = 'cubic-bezier(.22,1,.36,1)';
  const canMove = () => !disabled && !media.matches && !document.hidden && typeof Element.prototype.animate === 'function';
  function animate(el, frames, options = {}) {
    if (!el || !canMove()) return;
    const a = el.animate(frames, { duration: 640, easing: ease, fill: 'both', ...options });
    running.add(a);
    a.finished.then(() => { running.delete(a); a.cancel(); }, () => running.delete(a));
    return a;
  }
  function settle() {
    for (const a of running) a.cancel();
    running.clear();
    if (ghost) { ghost.remove(); ghost = null; }
    document.querySelectorAll('.circuit-pulse').forEach(el=>el.remove());
  }
  function svg(tag, attrs = {}) {
    const el = document.createElementNS(NS, tag);
    for (const [k,v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    return el;
  }
  // A low-contrast dotted silk surface, echoing the homepage. No renderer/dependency/RAF loop.
  const ambient = document.createElement('div');
  ambient.className = 'ambient'; ambient.setAttribute('aria-hidden', 'true');
  const wave = svg('svg', { viewBox:'0 0 1600 900', class:'silk-field', preserveAspectRatio:'xMidYMid slice' });
  const field = svg('g', { class:'silk-points' });
  for (let col=0; col<64; col++) for (let row=0; row<48; row++) {
    const x = 520 + col*18, y = 20 + row*18;
    const ridge = Math.sin(col*.12 + Math.sin(row*.095)*2.8);
    const second = Math.cos(col*.064-row*.045);
    const light = Math.pow(Math.max(0,(ridge+1)/2),3);
    const edge = Math.min(1,col/15,(64-col)/12,row/8,(48-row)/8);
    field.append(svg('circle',{cx:x,cy:y+ridge*11,r:Math.max(.55,1.1+light*2.15+second*.22),fill:'#d7d8e4',opacity:Math.max(0,edge*(.055+light*.35)).toFixed(3)}));
  }
  wave.append(field);
  ambient.append(wave);
  const jewel = document.createElement('div');
  jewel.className = 'ambient-jewel';
  jewel.innerHTML = '<div class="jewel-glass"><div class="jewel-rim"></div><div class="jewel-light"></div><svg viewBox="0 0 64 64" fill="none"><path d="M18 44V21l14 17 14-17v23M14 44h9m18 0h9" stroke="#e9ebf2" stroke-width="1.3" stroke-linejoin="round"/></svg></div>';
  ambient.append(jewel); stage.prepend(ambient);
  const heroJewel = jewel.cloneNode(true);
  heroJewel.className = 'hero-jewel'; heroJewel.setAttribute('aria-hidden','true');
  document.querySelector('.hero-result').append(heroJewel);
  // The active route is an editable SVG overlay on the HTML architecture.
  const architecture = document.querySelector('.architecture');
  const route = svg('svg',{class:'request-route','aria-hidden':'true',viewBox:'0 0 1456 500',preserveAspectRatio:'none'});
  const rail = svg('path',{class:'route-rail',fill:'none'});
  const glow = svg('path',{class:'route-active',fill:'none'});
  const packet = svg('circle',{class:'route-packet',r:5,cx:0,cy:0});
  route.append(rail,glow,packet); architecture.append(route);
  function wirePath(a,b,bus) {
    const sign=b.x>=a.x?1:-1, bend=12;
    return `M ${a.x} ${a.y} V ${bus-bend} Q ${a.x} ${bus} ${a.x+sign*bend} ${bus} H ${b.x-sign*bend} Q ${b.x} ${bus} ${b.x} ${bus-bend} V ${b.y}`;
  }
  // One travelling current, following the actual SVG conductor. No random metric changes.
  function pulseCircuit(host,d,{delay=0,duration=1050,bits=false,loop=false,phase=0}={}) {
    if(!canMove())return;
    const group=svg('g',{class:`circuit-pulse${loop?' current-loop':''}`,'aria-hidden':'true'});
    const options={duration,delay,easing:'linear',iterations:loop?Infinity:1};
    const flow=(el,frames,extra={})=>{const a=animate(el,frames,{...options,...extra});if(a&&loop)a.currentTime=phase;return a;};
    const halo=svg('path',{d,fill:'none',class:'circuit-halo'}), core=svg('path',{d,fill:'none',class:'circuit-core'});
    group.append(halo,core);host.append(group);
    const length=core.getTotalLength(),tail=Math.min(85,length*.32);
    for(const line of [halo,core]) flow(line,[{strokeDasharray:`${tail} ${length+tail}`,strokeDashoffset:tail},{strokeDasharray:`${tail} ${length+tail}`,strokeDashoffset:-length}],{duration,delay,easing:'linear'});
    const frames=[];
    for(let i=0;i<=32;i++){
      const point=core.getPointAtLength(length*i/32);
      frames.push({transform:`translate(${point.x}px,${point.y}px)`,opacity:i===0||i===32?0:1,offset:i/32});
    }
    let last;
    for(let i=0;i<3;i++){
      const spark=svg('circle',{class:'circuit-spark',r:i===0?3.2:1.6,cx:0,cy:0});group.append(spark);
      last=flow(spark,frames,{delay:delay+i*60});
    }
    if(bits){
      const bit=svg('text',{class:'circuit-bit',x:0,y:-12,'text-anchor':'middle'});bit.textContent='01';group.append(bit);
      last=flow(bit,frames,{delay:delay+170});
    }
    last?.finished.then(()=>group.remove(),()=>group.remove());
  }
  function routeGeometry(slide, step, previousStep=step) {
    if (slide.id !== 's4') return;
    const bounds = architecture.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const width=architecture.offsetWidth, height=architecture.offsetHeight;
    route.setAttribute('viewBox',`0 0 ${width} ${height}`);
    const nodes=[...slide.querySelectorAll('.query-flow .node')];
    const points=nodes.map(el=>{
      const r=el.getBoundingClientRect();
      return {x:(r.left+r.width/2-bounds.left)*width/bounds.width,y:(r.bottom-bounds.top)*height/bounds.height};
    });
    const bus=Math.max(...points.map(p=>p.y))+25;
    route.dataset.currentPath=wirePath(points[0],points.at(-1),bus);
    rail.setAttribute('d',route.dataset.currentPath+points.slice(1,-1).map(p=>` M ${p.x} ${bus} V ${p.y}`).join(''));
    const idx=Math.max(0,Math.min(points.length-1,step-1));
    glow.setAttribute('d',idx?wirePath(points[0],points[idx],bus):'');
    packet.setAttribute('cx',points[idx].x);packet.setAttribute('cy',points[idx].y);
    route.classList.toggle('is-active',step>0);
    if (step>0 && step!==previousStep && canMove()) {
      const before=Math.max(0,Math.min(points.length-1,previousStep-1));
      if(before!==idx){
        const d=wirePath(points[before],points[idx],bus);
        pulseCircuit(route,d,{bits:true});
        const probe=svg('path',{d});route.append(probe);const length=probe.getTotalLength(),frames=[];
        for(let n=0;n<=32;n++){
          const p=probe.getPointAtLength(length*n/32);
          frames.push({transform:`translate(${p.x-points[idx].x}px,${p.y-points[idx].y}px)`,offset:n/32});
        }
        probe.remove();animate(packet,frames,{duration:1050,easing:'linear'});
      }else{
        animate(packet,[{opacity:.25,transform:'scale(.5)'},{opacity:1,transform:'scale(1)'}],{duration:450});
      }
    }
  }
  function startCurrent(slide) {
    if(!slide||slide.id!=='s4'||slide.hidden||document.hidden||!canMove()||slide.querySelector('.current-loop'))return;
    const diagram=slide.querySelector('.architecture-links');
    diagram.querySelectorAll('g > path').forEach((path,i)=>pulseCircuit(diagram,path.getAttribute('d'),{duration:2600,loop:true,phase:i*550}));
    if(route.dataset.currentPath)for(const phase of [0,2400])pulseCircuit(route,route.dataset.currentPath,{duration:4800,bits:true,loop:true,phase});
  }
  function reveal(el, index=0, direction=1) {
    return animate(el,[{opacity:0,transform:`translate3d(${direction*20}px, 24px, 0)`,filter:'blur(3px)'},{opacity:1,transform:'translate3d(0,0,0)',filter:'blur(0px)'}],{delay:Math.min(index*55,220),duration:700});
  }
  const objects = {
    s1:'.brand-title, .hero .lead, .hero-result',
    s3:'.candidate-branch, .new-example, .corpus-strip',
    s2:'.scenario-rail, .demo-panel:not([hidden])',
    s4:'.lane-label, .prep .node, .query-flow .node, .architecture-foot',
    s5:'.evidence-card, .factor, .score-total, .takeaway',
    s6:'.chart, .eval-context, .eval-boundary',
    s7:'.final-route > div, .next-step, .qa-links',
    b1:'.formula-main, .backup-grid .node, .backup-note',
    b2:'.timeline > div, .eval-detail > *',
    b3:'.stack-catalog article, .stack-status > p',
    b4:'.data-branches .node, .takeaway',
    b5:'.backup-grid .node, .backup-note'
  };
  function enter(slide, previous, direction=1) {
    settle(); active=slide;
    stage.dataset.scene=slide.id;
    routeGeometry(slide,0);
    if (!canMove()) return;
    if (previous && previous !== slide) {
      ghost=previous.cloneNode(true); ghost.hidden=false; ghost.inert=true;
      ghost.classList.add('motion-ghost'); ghost.setAttribute('aria-hidden','true');
      ghost.removeAttribute('id'); ghost.querySelectorAll('[id]').forEach(el=>el.removeAttribute('id'));
      ghost.querySelectorAll('.speaker-note').forEach(el=>el.remove());
      stage.append(ghost);
      const leaving=ghost;
      const a=animate(leaving,[{opacity:.55,transform:'perspective(1800px) translateX(0) rotateY(0deg)'},{opacity:0,transform:`perspective(1800px) translateX(${-direction*90}px) rotateY(${-direction*3}deg)`}],{duration:260,easing:'cubic-bezier(.4,0,1,1)'});
      a?.finished.then(()=>{leaving.remove();if(ghost===leaving)ghost=null;},()=>leaving.remove());
    }
    const title=slide.querySelector('h1');
    animate(title,[{opacity:0,transform:`translate3d(${direction*35}px,28px,0)`,clipPath:'inset(0 0 100% 0)'},{opacity:1,transform:'translate3d(0,0,0)',clipPath:'inset(-15% -5% -15% -5%)'}],{duration:760});
    slide.querySelectorAll(objects[slide.id] || '.node').forEach((el,i)=>reveal(el,i+1,direction));
    animate(wave,[{transform:`translate3d(${direction*35}px,14px,0) scale(1.055)`},{transform:'translate3d(0,0,0) scale(1)'}],{duration:2100});
    if (slide.id==='s1'||slide.id==='s7') {
      const mark = slide.id==='s1' ? heroJewel : jewel;
      animate(mark,[{opacity:0,transform:'translate3d(25px,20px,0) rotate(-7deg)'},{opacity:1,transform:'translate3d(0,0,0) rotate(0deg)'}],{duration:1300});
      animate(mark.querySelector('.jewel-light'),[{transform:'translateX(-75%)',opacity:0},{opacity:.8,offset:.4},{transform:'translateX(85%)',opacity:0}],{duration:2200,delay:150});
      animate(mark.querySelector('.jewel-rim'),[{'--rim-angle':'205deg'},{'--rim-angle':'380deg'}],{duration:2200});
    }
    if(slide.id==='s4')startCurrent(slide);
    if(slide.id==='s2')slide.querySelectorAll('.relation-lines path').forEach((el,i)=>{const length=el.getTotalLength();animate(el,[{strokeDasharray:`${length}`,strokeDashoffset:length},{strokeDasharray:`${length}`,strokeDashoffset:0}],{duration:1000,delay:250+i*110});});
    if(slide.id==='s5')slide.querySelectorAll('.meter i').forEach((el,i)=>animate(el,[{transform:'scaleX(0)'},{transform:'scaleX(1)'}],{duration:850,delay:180+i*90}));
    if(slide.id==='s6') {
      const line=slide.querySelector('.hill-line');
      if(line){
        const length=line.getTotalLength();
        animate(line,[{strokeDasharray:`${length}`,strokeDashoffset:length},{strokeDasharray:`${length}`,strokeDashoffset:0}],{duration:1250,delay:120});
        animate(slide.querySelector('.hill-area'),[{clipPath:'inset(0 100% 0 0)'},{clipPath:'inset(0 0 0 0)'}],{duration:1250,delay:120});
        animate(slide.querySelector('.hill-gain'),[{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'translateY(0)'}],{duration:400,delay:900});
      }
    }
  }
  function accent(slide, step, previousStep) {
    if(active!==slide)active=slide;
    routeGeometry(slide,step,previousStep);
    const target=slide.querySelector(`[data-step="${step}"]`);
    if (!target || !canMove() || step===previousStep) return;
    animate(target,[{boxShadow:'0 0 0 1px #91baff00, 0 0 0 0 #91baff00'},{boxShadow:'0 0 0 1px #b7ceff88, 0 0 40px 0 #91baff22',offset:.3},{boxShadow:'0 0 0 1px #91baff33, 0 0 0 0 #91baff00'}],{duration:850});
    if(slide.id==='s5') {
      animate(target.querySelector('.meter i'),[{transform:'scaleX(.05)'},{transform:'scaleX(1)'}],{duration:800});
      animate(slide.querySelector('.score-total'),[{transform:'translateX(10px)',opacity:.65},{transform:'translateX(0)',opacity:1}],{duration:600});
    }
  }
  function tab(panel) {
    settle();if(!canMove())return;
    reveal(panel,0);
    panel.querySelectorAll('.scenario-numbers > div, .proof-heading, blockquote').forEach((el,i)=>reveal(el,i+1));
  }
  function setDisabled(value) { disabled=value; document.body.classList.toggle('motion-off',disabled||media.matches);if(!canMove())settle();else startCurrent(active); }
  media.addEventListener('change',()=>setDisabled(disabled));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)settle();else startCurrent(active);});
  window.DeckMotion={enter,accent,tab,setDisabled,settle,enabled:canMove};
  setDisabled(false);
})();
