/* FinGuard guided HR tour. Static, synthetic, no API or trading side effects. */
(() => {
  'use strict';
  const steps = [
    { title:'从一个研究问题开始', detail:'用户希望比较美股科技公司。接下来播放的是预先整理的研究场景，不会实时请求模型。', ms:5600, page:'workspace', run:'pending', focus:'.hero', trace:'progress' },
    { title:'Agent 调用多项金融工具', detail:'研究任务被拆成行情、公司事件与价格历史三条只读工具分支，过程和耗时都能查看。', ms:7000, page:'workspace', run:'pending', focus:'#workflowPanel', trace:'progress' },
    { title:'研究过程可以被审计', detail:'切换到技术 Trace：工具调用、回执和证据引用逐步记录，而不只给用户一个黑箱结论。', ms:6700, page:'workspace', run:'pending', focus:'#feed', trace:'trace' },
    { title:'结论背后有证据', detail:'每份研究都保存 Signal、Thesis、Risk、Decision 以及证据编号。这里是合成报告示例。', ms:6900, page:'library', focus:'.report', run:null },
    { title:'关键行动必须由人批准', detail:'当 Agent 提出模拟买入时，任务暂停等待人工审批。演示不会擅自批准，也不会产生订单。', ms:7200, page:'workspace', run:'pending', focus:'#decisionPanel', trace:'progress' },
    { title:'风控可以拒绝交易', detail:'这是另一笔 AAPL 示例任务：价格偏离超过预设阈值，确定性规则阻断模拟订单。', ms:7000, page:'workspace', run:'blocked', focus:'#decisionPanel', trace:'progress' },
    { title:'最后回到金融研究全景', detail:'金融看板连接行情与虚拟账户视图；完整 Python Agent、模拟账本及测试代码可在 GitHub 查阅。', ms:6300, page:'dashboard', focus:'.dashboard-top', run:null }
  ];
  const totalMs=steps.reduce((n,s)=>n+s.ms,0);
  const root=document.createElement('section');
  root.className='fg-tour-panel';
  root.id='fg-auto-tour';
  root.setAttribute('role','region');
  root.setAttribute('aria-label','FinGuard 自动产品演示');
  root.innerHTML=`<div class="fg-tour-top"><span class="fg-tour-kicker">FIN GUARD · GUIDED TOUR</span><span class="fg-tour-counter" id="fg-tour-counter">1 / 7 · 约 47 秒</span></div>
  <h2 id="fg-tour-title"></h2><p id="fg-tour-detail"></p>
  <div class="fg-tour-track" role="progressbar" aria-label="演示进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="fg-tour-track"><div class="fg-tour-fill" id="fg-tour-fill"></div></div>
  <div class="fg-tour-footer"><span class="fg-tour-disclaimer">STATIC UI · SYNTHETIC DATA · NO LIVE TRADING</span><div class="fg-tour-actions">
  <button type="button" id="fg-tour-toggle" class="fg-tour-primary">暂停</button>
  <button type="button" id="fg-tour-next">下一幕 →</button>
  <button type="button" id="fg-tour-close">跳过 ×</button></div></div>`;
  const reopen=document.createElement('button');
  reopen.type='button';reopen.id='fg-tour-reopen';reopen.className='fg-tour-reopen';
  reopen.textContent='▶ 重播产品演示';
  document.body.append(root,reopen);
  const title=document.getElementById('fg-tour-title');
  const detail=document.getElementById('fg-tour-detail');
  const counter=document.getElementById('fg-tour-counter');
  const track=document.getElementById('fg-tour-track');
  const fill=document.getElementById('fg-tour-fill');
  const toggle=document.getElementById('fg-tour-toggle');
  const next=document.getElementById('fg-tour-next');
  const close=document.getElementById('fg-tour-close');
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const KEY='finguard-hr-tour-visited-v1';
  let index=0, spent=0, elapsed=0, remaining=steps[0].ms, lastTick=0;
  let playing=false, finished=false, interval=null, focus=null, scrollTimer=null;
  const userSession=()=>{try{return sessionStorage.getItem(KEY)==='1'}catch{return false}};
  const markSession=()=>{try{sessionStorage.setItem(KEY,'1')}catch{}};
  const setFocus=el=>{if(focus)focus.classList.remove('fg-tour-spotlight');focus=el;if(focus)focus.classList.add('fg-tour-spotlight')};
  const scrollFocus=el=>{
    if(!el || root.hidden || !playing)return;
    const margin=window.innerWidth<=650?250:196;
    const rect=el.getBoundingClientRect();
    const top=rect.top+window.scrollY;
    const aim=Math.max(0,top-Math.max(40,(window.innerHeight-margin-Math.min(rect.height,310))/2));
    window.scrollTo({top:aim,behavior:reduced?'instant':'smooth'});
  };
  const updateProgress=()=>{
    const done=spent+elapsed;
    const pct=Math.max(0,Math.min(100,Math.round(done*100/totalMs)));
    fill.style.width=pct+'%';
    track.setAttribute('aria-valuenow',String(pct));
  };
  const renderButtons=()=>{
    toggle.textContent=finished?'重新播放':playing?'暂停':'继续';
    next.textContent=finished?'查看界面':index===steps.length-1?'完成 ✓':'下一幕 →';
    close.textContent=finished?'收起 ×':'跳过 ×';
  };
  const activate=(i)=>{
    clearTimeout(scrollTimer);
    index=i;elapsed=0;remaining=steps[i].ms;
    spent=steps.slice(0,i).reduce((n,s)=>n+s.ms,0);
    setFocus(null);
    const s=steps[i];
    if(s.run && typeof setRun==='function')setRun(s.run);
    if(s.page && typeof openPage==='function')openPage(s.page);
    if(s.trace){
      const el=document.getElementById(s.trace==='trace'?'technicalViewBtn':'progressViewBtn');
      if(el && s.page==='workspace')el.click();
    }
    title.textContent=s.title;
    detail.textContent=s.detail;
    counter.textContent=`${i+1} / ${steps.length} · 自动演示`;
    const element=document.querySelector(s.focus);
    scrollTimer=setTimeout(()=>{
      // A tour may have been paused or replaced while an animation was pending.
      if(!playing || index!==i)return;
      setFocus(element);
      scrollFocus(element);
    },280);
    lastTick=Date.now();
    updateProgress();
    renderButtons();
  };
  const stopClock=()=>{if(interval!==null){clearInterval(interval);interval=null}};
  const tick=()=>{
    if(!playing || finished)return;
    const now=Date.now();
    const dt=Math.min(400,Math.max(0,now-lastTick));
    lastTick=now;
    elapsed+=dt;
    remaining=Math.max(0,steps[index].ms-elapsed);
    updateProgress();
    if(remaining<=0){
      if(index<steps.length-1)activate(index+1);
      else complete();
    }
  };
  function pause(){
    if(!playing || finished)return;
    playing=false;stopClock();clearTimeout(scrollTimer);
    setFocus(null);renderButtons();
  }
  function play(){
    if(finished){restart();return}
    if(playing)return;
    playing=true;lastTick=Date.now();
    interval=setInterval(tick,80);
    // Re-highlight when continuing after a manual pause.
    const element=document.querySelector(steps[index].focus);
    setFocus(element);
    scrollFocus(element);
    renderButtons();
  }
  function complete(){
    stopClock();clearTimeout(scrollTimer);playing=false;finished=true;setFocus(null);
    spent=totalMs;elapsed=0;updateProgress();
    title.textContent='演示完成 · 现在可以自由探索';
    detail.textContent='这是一个合成数据的可交互产品原型。你可以继续浏览，也可以查看 GitHub 中可本地运行的 Python Agent 实现。';
    counter.textContent='7 / 7 · 已完成';
    renderButtons();markSession();
  }
  function dismiss(){
    pause();finished=false;setFocus(null);root.hidden=true;
    reopen.classList.add('fg-visible');markSession();
  }
  function restart(){
    finished=false;root.hidden=false;reopen.classList.remove('fg-visible');
    markSession();playing=true;stopClock();
    activate(0);interval=setInterval(tick,80);
  }
  toggle.addEventListener('click',()=>finished?restart():playing?pause():play());
  next.addEventListener('click',()=>{
    if(finished){dismiss();return}
    if(index<steps.length-1){
      const wasPlaying=playing;
      playing=true;activate(index+1);
      playing=wasPlaying;
      if(!playing){clearTimeout(scrollTimer);setFocus(null)}
      lastTick=Date.now();renderButtons();
    }else complete();
  });
  close.addEventListener('click',dismiss);
  reopen.addEventListener('click',restart);
  document.addEventListener('pointerdown',e=>{
    if(playing && !root.contains(e.target) && !reopen.contains(e.target))pause();
  },{capture:true});
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape' && !root.hidden && !document.getElementById('drawerBack')?.classList.contains('open'))dismiss();
  });
  window.addEventListener('pagehide',()=>{stopClock();clearTimeout(scrollTimer)});
  if(!reduced && !userSession())restart();
  else {root.hidden=true;reopen.classList.add('fg-visible')}
})();