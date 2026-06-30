const $ = s => document.querySelector(s);
const log=$("#log"), chatList=$("#chatList");
/* ---------- sticky-bottom output scrolling ----------
   Follow new output only while the user is already at the bottom; if they scroll
   up to read, the view stays put. A jump-to-latest button appears when not at
   the bottom. (stick() uses spaced assignment so the bulk-replace below doesn't
   match it.) */
let stickBottom = true;
function atBottom(){ return log.scrollHeight - log.scrollTop - log.clientHeight < 64; }
function stick(){ if(stickBottom){ log.scrollTop = log.scrollHeight; } updateJump(); }
function forceBottom(){ stickBottom = true; log.scrollTop = log.scrollHeight; updateJump(); }
function updateJump(){ const b=document.getElementById("jumpBottom"); if(b) b.classList.toggle("show", !atBottom()); }
log.addEventListener("scroll", ()=>{ stickBottom = atBottom(); updateJump(); });
let es=null, currentRun=null, pending=null, cur=null;
// JayNet busy-logo trace: cell numbers (1-based, 3x3) in animation order.
const BUSY_PATH=[1,2,5,7,5,3,6,9]; let busyTimer=null, busyStep=0;
let chat = { id:null, cid:null, title:null, saved:false, turns:[] };
// A stable per-chat id used to key the agent's scratch workspace (work_root when
// no project). Uses the server chat id once saved, else a client-minted uuid that
// persists in localStorage so scratch survives across turns/reloads of this chat.
function ensureCid(){
  if(!chat.cid) chat.cid = chat.id || (crypto.randomUUID ? crypto.randomUUID()
    : "c"+Date.now().toString(36)+Math.random().toString(36).slice(2));
  return chat.cid;
}

/* ---------- session persistence (survive a browser refresh) ---------- */
const LS=window.localStorage, CHAT_KEY="jaynet.chat", SET_KEY="jaynet.settings";
function lsGet(k,def){ try{ const v=LS.getItem(k); return v==null?def:JSON.parse(v); }catch(e){ return def; } }
function lsSet(k,v){ try{ LS.setItem(k, JSON.stringify(v)); return true; }catch(e){ return false; } }
function lsDel(k){ try{ LS.removeItem(k); }catch(e){} }

// Composer settings: toggles + per-run budget fields + active project. (Tool
// enable/disable is already persisted server-side, so it's excluded here.)
function collectSettings(){
  return { share:$("#share").checked, auto:$("#auto").checked, think:$("#think").checked,
    bMaxIter:$("#bMaxIter").value, bWall:$("#bWall").value, bCost:$("#bCost").value, bTok:$("#bTok").value,
    cCompact:$("#cCompact").checked, cMaxChars:$("#cMaxChars").value, cKeepLast:$("#cKeepLast").value,
    cParallel:$("#cParallel").checked,
    projectId:(activeProject?activeProject.id:"") };
}
function saveSettings(){ lsSet(SET_KEY, collectSettings()); }
function applySettings(){
  const s=lsGet(SET_KEY,null); if(!s) return null;
  const ck=(id,v)=>{ if($(id)&&typeof v==="boolean") $(id).checked=v; };
  ck("#share",s.share); ck("#auto",s.auto); ck("#think",s.think);
  ck("#cCompact",s.cCompact); ck("#cParallel",s.cParallel);
  const set=(id,v)=>{ if($(id)&&v!=null&&v!=="") $(id).value=v; };  // localStorage wins for fields the user set
  set("#bMaxIter",s.bMaxIter); set("#bWall",s.bWall); set("#bCost",s.bCost); set("#bTok",s.bTok);
  set("#cMaxChars",s.cMaxChars); set("#cKeepLast",s.cKeepLast);
  return s;   // projectId applied by the caller after refreshProjects()
}

// Active chat: persisted on every change so a refresh restores it verbatim.
// Verbose per-turn event logs are dropped only if the payload exceeds the quota.
function persistChat(){
  // localStorage keeps only a slim transcript (no per-turn event logs) so it can
  // never hit the storage quota. The full timeline (commentary + pinned tool/skill
  // calls) lives in the server copy of a saved chat and is rehydrated on load.
  const slim=t=>({user_message:t.user_message, answer:t.answer, run_id:t.run_id,
                  status:t.status, trajectory:t.trajectory||""});
  lsSet(CHAT_KEY, { id:chat.id, cid:chat.cid, title:chat.title, saved:chat.saved,
                    turns:chat.turns.map(slim) });
}

// Re-render the whole current chat into the log (shared by loadChat + restore).
function renderChatTurns(){
  log.innerHTML=""; cur=null; pending=null; currentRun=null;
  chat.turns.forEach((t,i)=>{
    if(i>0) sep("— turn "+(i+1)+" —");
    addMsg(t.user_message,"user");
    const c2=startResponse();
    let fin=null;
    for(const ev of (t.events||[])){ if(ev.type==="run_finish") fin=ev.data; else applyEvent(c2, ev); }
    finalize(c2, fin || {answer:t.answer, status:t.status, budget:{}});
  });
}

/* ---------- auth + tools panel ---------- */
async function api(p,o){ const r=await fetch(p,o); if(r.status===401){ location.href="/login"; throw new Error("unauth"); } return r; }
const TOOLS={ list:[], disabled:new Set(), collapsed:new Set() };
async function loadMe(){
  try{
    const me=await (await api("/api/me")).json();
    $("#who").textContent=me.username;
    if(me.is_admin) $("#adminLink").style.display="";
    // Pre-fill the per-run budget controls from the user's saved defaults
    // (set on the account page). Blank stays blank -> server config default.
    const b=me.budget||{}, set=(id,v)=>{ if(v!=null && $(id)) $(id).value=v; };
    set("#bMaxIter",b.max_iterations); set("#bWall",b.max_wall_clock_s);
    set("#bCost",b.max_cost_usd); set("#bTok",b.max_total_tokens);
    // Show the current admin-set house defaults as placeholders for blank fields.
    const eff=me.budget_defaults||{}, ph=(id,v)=>{ if(v!=null && $(id)) $(id).placeholder=String(v); };
    ph("#bMaxIter",eff.max_iterations); ph("#bWall",eff.max_wall_clock_s);
    ph("#bCost",eff.max_cost_usd); ph("#bTok",eff.max_total_tokens);
  }catch(e){}
}
async function loadTools(){
  try{
    const d=await (await api("/api/tools")).json();
    TOOLS.list=d.tools; TOOLS.disabled=new Set(d.tools.filter(t=>!t.enabled).map(t=>t.name));
    TOOLS.collapsed=new Set(d.tools.map(t=>t.namespace));   // groups collapsed by default
    renderTools();
  }catch(e){}
}
function renderTools(){
  const host=$("#toolList"); host.innerHTML="";
  // group tools by namespace, preserving first-seen order
  const order=[]; const byNs={};
  for(const t of TOOLS.list){
    if(!byNs[t.namespace]){ byNs[t.namespace]=[]; order.push(t.namespace); }
    byNs[t.namespace].push(t);
  }
  for(const ns of order){
    const tools=byNs[ns];
    const enabledCount=()=>tools.filter(t=>!TOOLS.disabled.has(t.name)).length;
    const group=document.createElement("div");
    group.className="nsgroup";
    if(TOOLS.collapsed.has(ns)) group.classList.add("collapsed");
    const head=document.createElement("button");
    head.type="button"; head.className="nshead";
    head.innerHTML=`<span class="caret">▸</span><span class="nsname">${ns}</span>`
      +`<span class="nscount">${enabledCount()}/${tools.length}</span>`;
    head.onclick=()=>{
      if(TOOLS.collapsed.has(ns)) TOOLS.collapsed.delete(ns); else TOOLS.collapsed.add(ns);
      group.classList.toggle("collapsed");
    };
    group.appendChild(head);
    const body=document.createElement("div"); body.className="nsbody";
    for(const t of tools){
      const row=document.createElement("div"); row.className="trow";
      const on=!TOOLS.disabled.has(t.name);
      const verb=t.name.split(".").slice(1).join(".")||t.name;
      row.innerHTML=`<span class="tname" title="${(t.description||'').replace(/"/g,'&quot;')}">${verb}</span>`
        +(t.private?'<span class="tflag">priv</span>':'')
        +(t.requires_confirmation?'<span class="tflag cf">confirm</span>':'')
        +`<label class="sw"><input type="checkbox" ${on?"checked":""}><span class="sl"></span></label>`;
      row.querySelector("input").onchange=e=>{
        if(e.target.checked) TOOLS.disabled.delete(t.name); else TOOLS.disabled.add(t.name);
        head.querySelector(".nscount").textContent=enabledCount()+"/"+tools.length;
        saveTools();
      };
      body.appendChild(row);
    }
    group.appendChild(body);
    host.appendChild(group);
  }
}
async function saveTools(){
  try{ await api("/api/tools",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({disabled:[...TOOLS.disabled]})}); }catch(e){}
}
function enabledTools(){ return TOOLS.list.filter(t=>!TOOLS.disabled.has(t.name)).map(t=>t.name); }
// Per-run budget overrides. Blank field => no override (server config default is used).
function budgetOverrides(){
  const o={};
  const it=parseInt($("#bMaxIter").value,10); if(Number.isFinite(it)&&it>0) o.max_iterations=it;
  const w=parseFloat($("#bWall").value);      if(Number.isFinite(w)&&w>0)  o.max_wall_clock_s=w;
  const c=parseFloat($("#bCost").value);      if(Number.isFinite(c)&&c>=0) o.max_cost_usd=c;
  const t=parseInt($("#bTok").value,10);      if(Number.isFinite(t)&&t>0)  o.max_total_tokens=t;
  return Object.keys(o).length?o:null;
}
/* Per-run context overrides (compaction + parallel exec) from the Run options.
   Toggles always send (UI is the source of truth per run); numbers only when set
   (else the server keeps its configured default). */
function compactionOverride(){
  const c={ enabled: $("#cCompact").checked };
  const mc=parseInt($("#cMaxChars").value,10); if(Number.isFinite(mc)&&mc>0) c.max_result_chars=mc;
  const kl=parseInt($("#cKeepLast").value,10); if(Number.isFinite(kl)&&kl>=0) c.keep_last=kl;
  return c;
}
function parallelOverride(){ return { enabled: $("#cParallel").checked }; }
/* Per-run sampler overrides. Only fields the user actually sets are sent; blanks
   fall through to the configured/server default (no restart needed either way). */
function samplingOverride(){
  const o={};
  const t=parseFloat($("#sTemp").value);   if(Number.isFinite(t)&&t>=0) o.temperature=t;
  const p=parseFloat($("#sTopP").value);   if(Number.isFinite(p)&&p>=0) o.top_p=p;
  const k=parseInt($("#sTopK").value,10);  if(Number.isFinite(k)&&k>=0) o.top_k=k;
  const r=parseFloat($("#sRepeat").value); if(Number.isFinite(r)&&r>=0) o.repeat_penalty=r;
  const s=parseInt($("#sSeed").value,10);  if(Number.isFinite(s))      o.seed=s;
  return Object.keys(o).length?o:null;
}
$("#allOn").onclick=()=>{ TOOLS.disabled.clear(); renderTools(); saveTools(); };
$("#allOff").onclick=()=>{ TOOLS.disabled=new Set(TOOLS.list.map(t=>t.name)); renderTools(); saveTools(); };
/* Advanced settings: hide tool-disabling + sampling by default; the toggle
   reveals them and the choice is remembered. */
const ADV_KEY="jaynet.advanced";
function applyAdvanced(on){ document.querySelectorAll(".adv").forEach(el=>{ el.hidden=!on; }); }
(function initAdvanced(){
  const t=$("#advToggle"); if(!t) return;
  const on=!!lsGet(ADV_KEY,false);
  t.checked=on; applyAdvanced(on);
  t.addEventListener("change", ()=>{ applyAdvanced(t.checked); lsSet(ADV_KEY,t.checked); });
})();
$("#logout").onclick=async()=>{ try{ await fetch("/api/logout",{method:"POST"}); }catch(e){} location.href="/login"; };

/* ---------- side panels: collapse on desktop, drawers on mobile ---------- */
function isNarrow(){ return innerWidth<=900; }
function closeDrawers(){ document.body.classList.remove("show-chats","show-tools","show-proj"); }
function drawer(name){ const cls="show-"+name, on=document.body.classList.contains(cls);
  closeDrawers(); if(!on) document.body.classList.add(cls); }   // mobile: one drawer at a time
$("#chatsToggle").addEventListener("click", ()=>{
  if(isNarrow()) drawer("chats"); else document.body.classList.toggle("collapse-chats");
});
$("#toolsToggle").addEventListener("click", ()=>{
  if(isNarrow()) drawer("tools"); else document.body.classList.toggle("collapse-tools");
});
$("#drawerScrim").addEventListener("click", closeDrawers);
addEventListener("keydown", e=>{ if(e.key==="Escape") closeDrawers(); });
chatList.addEventListener("click", ()=>{ if(isNarrow()) closeDrawers(); });
$("#newChat").addEventListener("click", ()=>{ if(isNarrow()) closeDrawers(); });
$("#newChatTop").addEventListener("click", ()=>$("#newChat").click());
/* side panels start collapsed (desktop); on mobile they're closed drawers anyway */
document.body.classList.add("collapse-chats","collapse-tools");

/* ---------- resizable panels (drag the inner edge; widths persist) ---------- */
(function(){
  const root=document.documentElement, KEY="jaynet.panels";
  try{ const w=JSON.parse(LS.getItem(KEY)||"{}");
       for(const [v,px] of Object.entries(w)) root.style.setProperty(v, px+"px"); }catch(_){}
  const MIN={"--sidew":190,"--projw":260,"--toolw":190}, MAX=860;
  function persist(v,px){ let w={}; try{ w=JSON.parse(LS.getItem(KEY)||"{}"); }catch(_){}
    w[v]=px; LS.setItem(KEY, JSON.stringify(w)); }
  function cur(v){ return parseInt(getComputedStyle(root).getPropertyValue(v))||300; }
  for(const h of document.querySelectorAll(".resizer")){
    h.addEventListener("pointerdown", e=>{
      if(isNarrow()) return;                      // drawers on mobile — no resize
      e.preventDefault();
      const v=h.dataset.var, left=h.classList.contains("resizer-l");
      const x0=e.clientX, w0=cur(v), min=MIN[v]||200;
      h.classList.add("dragging"); document.body.classList.add("resizing");
      h.setPointerCapture(e.pointerId);
      const move=ev=>{ const dx=left?(x0-ev.clientX):(ev.clientX-x0);
        root.style.setProperty(v, Math.max(min,Math.min(MAX,Math.round(w0+dx)))+"px"); };
      const up=()=>{ h.releasePointerCapture(e.pointerId);
        h.removeEventListener("pointermove",move); h.removeEventListener("pointerup",up);
        h.classList.remove("dragging"); document.body.classList.remove("resizing");
        persist(v, cur(v)); };
      h.addEventListener("pointermove",move); h.addEventListener("pointerup",up);
    });
  }
})();

/* ---------- loaded-models footer (orchestrator + coder) ---------- */
async function loadModels(){
  const foot=$("#modelsFoot"); if(!foot) return;
  let m; try{ m=await (await api("/api/models")).json(); }catch(_){ foot.innerHTML=""; return; }
  foot.innerHTML="";
  const add=(label,info)=>{
    if(!info) return;
    const row=document.createElement("div"); row.className="mrow";
    const dot=document.createElement("span");
    dot.className="mdot"+(info.online===true?" on":info.online===false?" off":"");
    const lab=document.createElement("span"); lab.className="mlabel"; lab.textContent=label;
    const nm=document.createElement("span"); nm.className="mname";
    nm.textContent=info.model||info.alias||"—";
    // Hover tooltip: alias + the model's present settings + liveness.
    const lines=[];
    if(info.alias && info.alias!==info.model) lines.push("alias: "+info.alias);
    const s=info.settings||{};
    for(const k of Object.keys(s)) lines.push(k+": "+s[k]);
    lines.push("status: "+(info.online===false?"offline":info.online===true?"online":"unknown"));
    const tip=lines.join("\n");
    row.title=tip; nm.title=tip; dot.title=tip;   // hover anywhere on the row
    row.append(dot,lab,nm); foot.appendChild(row);
  };
  add("brain", m.orchestrator); add("coder", m.coder);
}

/* 2FA enrollment/disable now lives on the dedicated account page (/account). */

/* ---------- basic rendering ---------- */
function addMsg(text, cls, atts){
  const d=document.createElement("div"); d.className="msg "+cls; d.textContent=text;
  if(atts && atts.length) d.appendChild(renderAtts(atts));
  log.appendChild(d); stick(); return d;
}
function renderAtts(atts){
  const w=document.createElement("div"); w.className="atts";
  for(const a of atts){
    if(a.kind==="image"){ const im=document.createElement("img"); im.src="/api/upload/"+a.id; im.alt=a.name; im.title=a.name; w.appendChild(im); }
    else { const f=document.createElement("span"); f.className="f"; f.textContent="📄 "+a.name; w.appendChild(f); }
  }
  return w;
}
function fmtSize(n){ return n<1024?n+" B":(n<1048576?(n/1024).toFixed(0)+" KB":(n/1048576).toFixed(1)+" MB"); }
function sep(t){ const d=document.createElement("div"); d.className="turnsep"; d.textContent=t; log.appendChild(d); }
function setStatus(s, live){ $("#status").textContent=s; $("#dot").classList.toggle("live",!!live);
  const f=$("#form"); if(f) f.classList.toggle("running",!!live);
  const send=$("#send"); if(send){ send.disabled=false;
    send.title=live?"Stop run":"Send"; send.setAttribute("aria-label",live?"Stop run":"Send"); }
  if(live) startBusy(); else stopBusy(); }
/* trace the JayNet logo through BUSY_PATH while a run is live */
function startBusy(){ if(busyTimer) return;
  const cells=document.querySelectorAll("#busy span"); if(!cells.length) return;
  busyStep=0;
  const tick=()=>{ cells.forEach(c=>c.classList.remove("on"));
    const n=BUSY_PATH[busyStep % BUSY_PATH.length]; const el=cells[n-1];
    if(el) el.classList.add("on"); busyStep++; };
  tick(); busyTimer=setInterval(tick, 230); }
function stopBusy(){ if(busyTimer){ clearInterval(busyTimer); busyTimer=null; }
  document.querySelectorAll("#busy span").forEach(c=>c.classList.remove("on")); }
function esc(o){ return JSON.stringify(o,null,2); }
function fmtUsd(x){ return "$"+Number(x||0).toFixed(4); }
function fmtTok(n){ n=Number(n||0); return n>=1000?(n/1000).toFixed(n>=10000?0:1)+"k":String(n); }

/* ---------- a response block: a visible timeline (commentary + the tool calls
   it triggered, pinned together) ending in the final answer ---------- */
function startResponse(){
  const root=document.createElement("div"); root.className="resp";
  const flow=document.createElement("div"); flow.className="flow";
  const think=document.createElement("details"); think.className="thinking"; think.hidden=true;
  think.innerHTML="<summary><span class='sum'>thinking</span></summary><div class='tk'></div>";
  const foot=document.createElement("div"); foot.className="foot"; foot.textContent="running…";
  root.append(flow, think, foot);
  log.appendChild(root); stick();
  return { root, flow, think, tk:think.querySelector(".tk"), foot,
           cur:null, curCalls:null, pending:[], ticker:null,
           toolCount:0, turns:0, model:null, hadThinking:false,
           llmLive:null, reasonLive:null, dlbox:null };
}
/* the active prose block — commentary while the run continues; the LAST one
   becomes the final answer at finalize() */
function curBlock(c){
  if(!c.cur){ c.cur=document.createElement("div"); c.cur.className="seg comment"; c.flow.appendChild(c.cur); }
  return c.cur;
}
function appendProse(c, text){ curBlock(c).textContent+=text; if(es) stick(); }
/* a container for the tool rows triggered by the current commentary */
function callsContainer(c){
  const box=document.createElement("div"); box.className="calls";
  c.flow.appendChild(box); c.curCalls=box; return box;
}
function fmtDur(ms){ ms=Math.max(0, ms||0);
  return ms<1000 ? Math.round(ms)+" ms" : (ms/1000).toFixed(ms<10000?1:0)+"s"; }
function ensureTicker(c){
  if(c.ticker || !c.pending.length) return;
  c.ticker=setInterval(()=>{ const now=Date.now();
    for(const p of c.pending){ if(p.timerEl) p.timerEl.textContent=fmtDur(now-p.start); }
  }, 100);
}
function stopTicker(c){ if(c.ticker){ clearInterval(c.ticker); c.ticker=null; } }
function takePending(c, name){
  if(!c.pending.length) return null;
  let i=c.pending.findIndex(p=>p.name===name); if(i<0) i=0;
  return c.pending.splice(i,1)[0];
}
function skillTag(name){ return name.startsWith("skill.") ? "<span class='skill'>skill</span> " : ""; }
/* a running tool row: spinner + live seconds, finalized to ✓/✗ + latency */
function addCalls(c, calls){
  if(!c.curCalls) callsContainer(c);
  for(const t of calls){
    const el=document.createElement("div"); el.className="callrow run";
    el.innerHTML="<div class='crhead'><span class='spin'></span>"+
                 "<span class='cn'>"+skillTag(t.name)+t.name+"</span>"+
                 "<span class='timer live'>0 ms</span></div>";
    c.curCalls.appendChild(el);
    c.pending.push({ el, name:t.name, start:Date.now(), timerEl:el.querySelector(".timer") });
  }
  ensureTicker(c);
  if(es) stick();
}
function addToolResult(c, d){
  const ok=d.status!=="error";
  const p=takePending(c, d.tool);
  let el;
  if(p){ el=p.el; }
  else { if(!c.curCalls) callsContainer(c); el=document.createElement("div"); el.className="callrow"; c.curCalls.appendChild(el); }
  el.classList.remove("run");
  const body=ok ? (d.result_preview||"") : ("ERROR: "+(d.error||""));
  const args=d.args ? ("\nargs: "+esc(d.args)) : "";
  const hasBody=!!(body||args);
  el.innerHTML=
    "<div class='crhead"+(hasBody?" exp":"")+"'>"+
      "<span class='cn "+(ok?"ok":"err")+"'>"+(ok?"✓ ":"✗ ")+skillTag(d.tool||"")+(d.tool||"")+"</span>"+
      "<span class='meta'>"+(d.latency_ms!=null?fmtDur(d.latency_ms):"")+"</span>"+
      (d.private?"<span class='priv'>private</span>":"")+
    "</div>"+(hasBody?"<pre></pre>":"");
  if(hasBody){
    el.querySelector("pre").textContent=body+args;
    el.querySelector(".crhead").onclick=()=>el.classList.toggle("open");
  }
  c.toolCount++;
  if(!c.pending.length) stopTicker(c);
}
function llmAppend(c, model, text){
  if(!c.llmLive){
    if(!c.curCalls) callsContainer(c);
    c.llmLive=document.createElement("div"); c.llmLive.className="callrow delegated";
    c.llmLive.innerHTML="<div class='crhead'><span class='cn'>delegated → "+(model||"")+"</span></div><pre></pre>";
    c.curCalls.appendChild(c.llmLive);
  }
  c.llmLive.querySelector("pre").textContent+=text;
  if(es) stick();
}
function reasonAppend(c, text){
  if(text==null) return;
  if(!c.reasonLive){
    if(!text.trim()) return;
    c.reasonLive=document.createElement("div"); c.reasonLive.className="tkitem";
    c.reasonLive.innerHTML="<pre></pre>"; c.tk.appendChild(c.reasonLive);
    c.hadThinking=true; c.think.hidden=false;
  }
  c.reasonLive.querySelector("pre").textContent+=text;
}
function warnRow(c, html){
  if(!c.curCalls) callsContainer(c);
  const el=document.createElement("div"); el.className="callrow";
  el.innerHTML="<div class='crhead'>"+html+"</div>"; c.curCalls.appendChild(el);
}
function footLive(c, costData){
  if(costData && costData.total_usd!=null){
    let s="running… "+fmtUsd(costData.total_usd)+" · "+fmtTok(costData.total_tokens||0)+" tok";
    if(costData.tokens_prompt!=null || costData.tokens_completion!=null)
      s+=" ("+fmtTok(costData.tokens_prompt||0)+" in · "+fmtTok(costData.tokens_completion||0)+" out)";
    c.foot.textContent=s;
  }
}
function finalize(c, d){
  stopTicker(c);
  for(const p of c.pending){
    const t=p.el.querySelector(".timer"); if(t) t.classList.remove("live");
    const s=p.el.querySelector(".spin"); if(s) s.remove();
  }
  c.pending=[]; c.llmLive=null;
  // The last prose block is the answer; promote it (or create one).
  if(c.cur && c.cur.textContent.trim()){
    c.cur.classList.remove("comment"); c.cur.classList.add("answer");
    if(d.answer && !c.cur.textContent.trim()) c.cur.textContent=d.answer;
  } else {
    if(c.cur) c.cur.remove();
    const a=document.createElement("div"); a.className="seg answer";
    a.textContent=(d.answer!=null ? (d.answer||"(no answer)") : "(no answer)");
    c.flow.appendChild(a);
  }
  c.cur=null;
  // footer line
  const b=d.budget||{};
  const tk=b.tokens||{};
  const tot=(tk.total!=null)?tk.total:0;
  let tokStr=fmtTok(tot)+" tok";
  if(tk.prompt!=null || tk.completion!=null)
    tokStr+=" ("+fmtTok(tk.prompt||0)+" in · "+fmtTok(tk.completion||0)+" out"+
            (tk.cached?" · "+fmtTok(tk.cached)+" cached":"")+")";
  const parts=[ c.model||"local",
                (b.iterations||c.turns||1)+" turn"+(((b.iterations||c.turns)>1)?"s":""),
                c.toolCount+" tool"+(c.toolCount===1?"":"s"),
                tokStr, fmtUsd(b.cost_usd),
                (b.elapsed_s!=null?Number(b.elapsed_s).toFixed(1)+"s":"") ];
  let line=parts.filter(Boolean).join(" · ");
  if(d.status && d.status!=="ok") line="<span class='badge'>"+d.status+"</span> · "+line;
  c.foot.innerHTML=line;
}

/* preview/open categories for a generated deliverable:
   - editable text/code/data  -> open in the in-app viewer popup (read-only)
   - browser-native (image/pdf/svg/html) -> open in a new tab (renders natively)
   - anything else (archives, binaries) -> download only, no open */
function _ext(name){ return (name||"").toLowerCase().split(".").pop(); }
function editableText(name){
  return ["txt","md","markdown","json","csv","tsv","log","xml","yaml","yml",
          "py","js","ts","tsx","jsx","css","ini","conf","cfg","toml","sh","sql"].includes(_ext(name));
}
function nativeView(name){
  return ["pdf","png","jpg","jpeg","gif","webp","svg","bmp","ico","html","htm"].includes(_ext(name));
}
/* Open a chat-produced output the same way the inline ↗/⬇ chip does:
   editable text -> in-app viewer; image/pdf/svg/html -> new tab; else download. */
function openOutputEntry(runId, name, kind){
  const href="/api/output/"+runId;
  if(kind!=="targz" && editableText(name)) openDeliverable(runId, name);
  else if(kind!=="targz" && nativeView(name)) window.open(href+"?inline=1","_blank","noopener");
  else window.open(href,"_blank","noopener");   // archive / binary -> download
}

/* apply one event to a response (used live AND when replaying a saved chat) */
function applyEvent(c, ev){
  const d=ev.data||{};
  switch(ev.type){
    case "model_turn":
      if(d.model) c.model=d.model;
      c.turns++;
      if(d.tool_calls && d.tool_calls.length){
        if(c.cur && !c.cur.textContent.trim()) c.cur.remove();   // drop empty commentary
        c.cur=null;
        callsContainer(c);                                       // calls pinned under the comment
        addCalls(c, d.tool_calls);
      } else if(d.content && (!c.cur || !c.cur.textContent)){
        appendProse(c, d.content);                               // non-streaming fallback
      }
      c.reasonLive=null; c.llmLive=null;
      break;
    case "tool_result": addToolResult(c, d); break;
    case "confirmation":
      warnRow(c, "<span class='cn warn'>confirmation "+(d.approved?"approved":"denied")+"</span>"); break;
    case "token":
      if(d.scope==="reasoning") reasonAppend(c, d.text);
      else if(d.scope==="llm.call") llmAppend(c, d.model, d.text);
      else appendProse(c, d.text);
      break;
    case "cost": footLive(c, d); break;
    case "output": {
      if(!c.dlbox){ c.dlbox=document.createElement("div"); c.dlbox.className="downloads"; c.root.appendChild(c.dlbox); }
      const href="/api/output/"+(ev.run_id||currentRun);
      let dl=c.dlbox.querySelector("a.dl");
      if(!dl){ dl=document.createElement("a"); dl.className="dl"; dl.setAttribute("download",""); c.dlbox.appendChild(dl); }
      dl.href=href;
      dl.title=(d.kind==="targz")?"download bundled archive":"download";
      dl.textContent="⬇ "+(d.name||"download")+" ("+fmtSize(d.size||0)+")";
      let op=c.dlbox.querySelector("a.dl.open");
      const runId=ev.run_id||currentRun;
      const canOpen = d.kind!=="targz" && (editableText(d.name)||nativeView(d.name));
      if(canOpen){
        if(!op){ op=document.createElement("a"); op.className="dl open"; c.dlbox.appendChild(op); }
        op.textContent="↗ open";
        if(editableText(d.name)){
          // editable text/code/data -> in-app viewer popup (like project files)
          op.removeAttribute("target"); op.removeAttribute("rel"); op.href="#";
          op.title="open in a viewer";
          op.onclick=(e)=>{ e.preventDefault(); openDeliverable(runId, d.name); };
        } else {
          // image / pdf / svg / html -> new tab (browser renders it natively)
          op.onclick=null; op.target="_blank"; op.rel="noopener";
          op.href=href+"?inline=1"; op.title="open in a new tab";
        }
      } else if(op){ op.remove(); }
      break;
    }
    case "budget_warning":
      warnRow(c, "<span class='cn warn'>budget</span> nearing the "+(d.dimension||"")+
                 " limit ("+Math.round((d.pressure||0)*100)+"%) — wrapping up");
      break;
    default: break; // run_start, tool_selection: not shown
  }
}

/* ---------- saved-chat sidebar ---------- */
async function refreshChats(){
  const {chats}=await (await fetch("/api/chats")).json();
  chatList.innerHTML = chats.length ? "" : "<div class='empty'>no saved chats</div>";
  for(const ch of chats){
    const it=document.createElement("div");
    it.className="chatItem"+(ch.id===chat.id?" active":"");
    it.innerHTML="<span class='ttl'></span><span class='when'>"+(ch.turns||0)+"</span><button class='del' title='delete'>×</button>";
    it.querySelector(".ttl").textContent=ch.title||"(untitled)";
    it.querySelector(".ttl").onclick=()=>loadChat(ch.id);
    if(ch.project_id && projectsById[ch.project_id]){
      const b=document.createElement("span"); b.className="pbadge";
      b.textContent=projectsById[ch.project_id].name;
      b.title="In project: "+projectsById[ch.project_id].name+" — click to open";
      b.onclick=(e)=>{ e.stopPropagation(); refreshProjects(ch.project_id); };
      it.insertBefore(b, it.querySelector(".when"));
    }
    it.querySelector(".del").onclick=(e)=>{ e.stopPropagation(); askDelete(ch.id, ch.title); };
    chatList.appendChild(it);
  }
}
function updateSaveBtn(){ const b=$("#saveBtn"); b.classList.toggle("on",chat.saved); b.title=chat.saved?"Saved — click to unsave":"Save this chat"; }
async function saveChat(){
  if(!chat.turns.length) return;
  const res=await (await fetch("/api/chats",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({id:chat.id, title:chat.title, turns:chat.turns, project_id:(activeProject?activeProject.id:null)})})).json();
  chat.id=res.id; chat.title=res.title; chat.saved=true; updateSaveBtn(); refreshChats(); persistChat();
}
async function syncIfSaved(){ if(chat.saved && chat.id) await saveChat(); }
function askDelete(id, title){
  showModal("Remove “"+(title||"this chat")+"” from saved? This permanently deletes it.", async ()=>{
    await fetch("/api/chats/"+id,{method:"DELETE"});
    if(id===chat.id){ chat.saved=false; chat.id=null; updateSaveBtn(); persistChat(); }
    refreshChats();
  });
}
$("#saveBtn").onclick=()=>{ if(!chat.saved) saveChat(); else askDelete(chat.id, chat.title); };

async function loadChat(id){
  const c=await (await fetch("/api/chats/"+id)).json();
  chat={ id:c.id, cid:c.id, title:c.title, saved:true, turns:c.turns.map(t=>({
    user_message:t.user_message, answer:t.answer, run_id:t.run_id, status:t.status, trajectory:t.trajectory||"", events:t.events||[] })) };
  renderChatTurns();
  setStatus("loaded saved chat", false);
  projSelect.value = c.project_id || "";
  syncActive();
  updateSaveBtn(); refreshChats(); persistChat();
}
$("#newChat").onclick=()=>{
  // Explicit new chat is the ONLY thing that starts fresh. If the current chat was
  // never saved it lived only here + in localStorage, so clearing it IS the delete.
  // A saved chat keeps its server copy in the list; we just detach from it.
  chat={ id:null, cid:null, title:null, saved:false, turns:[] };
  pending=null; currentRun=null; cur=null; log.innerHTML="";
  lsDel(CHAT_KEY);
  setStatus("idle", false); updateSaveBtn(); refreshChats();
  if(!activeProject) loadTree();                // reset the per-chat file list
};

/* ---------- projects ---------- */
let activeProject=null;                       // {id,name} or null
let projectsById={};                          // id -> {id,name,...} for chat badges
let editorFile=null, cmEditor=null;
const _CM_MODE={py:"python", js:"javascript",mjs:"javascript",cjs:"javascript",jsx:"javascript",
  ts:"javascript",tsx:"javascript", json:{name:"javascript",json:true},
  c:"text/x-csrc",h:"text/x-csrc",cpp:"text/x-c++src",hpp:"text/x-c++src",cc:"text/x-c++src",
  java:"text/x-java", rs:"rust", go:"go", html:"htmlmixed",htm:"htmlmixed", xml:"xml",
  svg:"xml", css:"css", md:"markdown",markdown:"markdown", yaml:"yaml",yml:"yaml", toml:"toml",
  sh:"shell",bash:"shell",zsh:"shell", sql:"sql", dockerfile:"dockerfile",
  ini:"properties",cfg:"properties",conf:"properties",env:"properties",properties:"properties"};
function modeForPath(p){
  const base=p.split("/").pop().toLowerCase();
  if(base==="dockerfile") return "dockerfile";
  return _CM_MODE[(base.split(".").pop()||"")] || null;
}
const projSelect=$("#projSelect"), projPanel=$("#projPanel"), fileTree=$("#fileTree");
async function refreshProjects(want){
  const {projects}=await (await fetch("/api/projects")).json();
  projectsById={}; for(const p of projects) projectsById[p.id]=p;
  const keep = want!==undefined ? want : (activeProject?activeProject.id:"");
  projSelect.innerHTML="<option value=''>— no project, showing current chat —</option>";
  for(const p of projects){
    const o=document.createElement("option");
    o.value=p.id; o.textContent=p.name+" ("+(p.file_count||0)+")";
    projSelect.appendChild(o);
  }
  projSelect.value = projects.some(p=>p.id===keep) ? keep : "";
  syncActive();
  refreshChats();                              // resolve project badges now names are known
}
function syncActive(){
  const id=projSelect.value;
  activeProject = id ? {id, name:(projSelect.selectedOptions[0]?.textContent||id)} : null;
  projPanel.hidden = false;                    // always shown: project files, or this chat's files
  $("#projSection").classList.toggle("chat-mode", !activeProject);
  loadTree();                                  // project tree, or chat files when no project
  // header indicator next to the status: a clickable project-name chip
  const chip=$("#projActive");
  if(activeProject){
    chip.hidden=false; chip.innerHTML="<span></span>";
    chip.querySelector("span").textContent=activeProject.name;
    chip.title="Project: "+activeProject.name+" — click to show files";
  } else {
    chip.hidden=true; chip.textContent="";
  }
}
$("#projActive").addEventListener("click", ()=>{
  if(isNarrow()) drawer("chats"); else document.body.classList.remove("collapse-chats");
  const p=$("#projPanel"); if(p && !p.hidden) p.scrollIntoView({block:"nearest"});
});
projSelect.onchange=()=>{ syncActive(); saveSettings(); };
$("#newProj").onclick=async()=>{
  const name=prompt("New project name:"); if(!name) return;
  const p=await (await fetch("/api/projects",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({name})})).json();
  await refreshProjects(p.id);
};
$("#projFromChat").onclick=async()=>{
  if(!chat.turns.length){ setStatus("nothing in this chat yet", false); return; }
  const name=prompt("Project name:", chat.title||"Project"); if(!name) return;
  await saveChat();                       // ensure the server has the chat + its run files
  const r=await (await fetch("/api/chats/"+chat.id+"/promote",{method:"POST",
    headers:{"content-type":"application/json"},body:JSON.stringify({name})})).json();
  if(r.project_id){
    await refreshProjects(r.project_id);  // select it -> future turns work in the project
    setStatus("created project “"+(r.project?r.project.name:name)+"” from this chat", false);
  } else setStatus("could not create project", false);
};
$("#delProj").onclick=()=>{
  if(!activeProject) return;
  showModal("Delete project “"+activeProject.name+"” and all its files? This cannot be undone.", async()=>{
    await fetch("/api/projects/"+activeProject.id,{method:"DELETE"});
    await refreshProjects("");
  });
};
let ftCollapsed = new Set();   // folder paths currently collapsed (kept across re-renders)
// The active filespace: a project, or (no project) this chat's scratch workspace.
// Both expose the same {entries:[{path,type,size}]} + /file CRUD shape.
function fsBase(){ return activeProject ? ("/api/projects/"+activeProject.id)
                                        : ("/api/chat-scratch/"+ensureCid()); }
async function loadTree(){
  const r=await fetch(fsBase()+"/files");
  if(!r.ok){ fileTree.innerHTML="<div class='empty'>—</div>"; return; }
  const {entries}=await r.json();
  if(!entries.length){
    fileTree.innerHTML = activeProject
      ? "<div class='empty'>empty — ＋ to add files</div>"
      : "<div class='empty'>files the agent creates in this chat appear here</div>";
    return;
  }
  // Build a nested tree from the flat {path,type} entries (synthesising any
  // intermediate folders implied by file paths).
  const root={dirs:new Map(), files:[], path:null};
  const dirNode=(parts)=>{ let n=root;
    for(let i=0;i<parts.length;i++){ const p=parts[i];
      if(!n.dirs.has(p)) n.dirs.set(p,{dirs:new Map(),files:[],path:parts.slice(0,i+1).join("/")});
      n=n.dirs.get(p); } return n; };
  for(const e of entries){
    const parts=e.path.split("/");
    if(e.type==="dir"){ dirNode(parts); }
    else { (parts.length>1?dirNode(parts.slice(0,-1)):root).files.push(e); }
  }
  fileTree.innerHTML="";
  fileTree.appendChild(renderDir(root));
}
function renderDir(node){
  const frag=document.createDocumentFragment();
  for(const name of [...node.dirs.keys()].sort((a,b)=>a.localeCompare(b))){
    const child=node.dirs.get(name), dpath=child.path||name, collapsed=ftCollapsed.has(dpath);
    const row=document.createElement("div"); row.className="ftrow ftdir"; row.dataset.path=dpath;
    if(collapsed) row.classList.add("collapsed-row");
    const caret=document.createElement("span"); caret.className="ftcaret"; caret.textContent="▾";
    const nm=document.createElement("span"); nm.className="ftname"; nm.textContent="📁 "+name;
    const del=document.createElement("button"); del.className="ftdel"; del.title="delete"; del.textContent="×";
    del.onclick=ev=>{ ev.stopPropagation(); deleteProjectPath(dpath,"dir"); };
    row.append(caret,nm,del);
    const kids=document.createElement("div"); kids.className="ftchildren"+(collapsed?" collapsed":"");
    kids.appendChild(renderDir(child));
    row.onclick=()=>{ const now=kids.classList.toggle("collapsed");
      row.classList.toggle("collapsed-row",now);
      if(now) ftCollapsed.add(dpath); else ftCollapsed.delete(dpath); };
    frag.append(row,kids);
  }
  for(const e of node.files.slice().sort((a,b)=>a.path.localeCompare(b.path))){
    const row=document.createElement("div"); row.className="ftrow ftfile";
    const pad=document.createElement("span"); pad.className="ftcaret";   // align under carets
    const nm=document.createElement("span"); nm.className="ftname";
    nm.textContent="📄 "+e.path.split("/").pop();
    nm.title=e.path+" · "+fmtSize(e.size); nm.onclick=()=>openFile(e.path);
    const del=document.createElement("button"); del.className="ftdel"; del.title="delete"; del.textContent="×";
    del.onclick=ev=>{ ev.stopPropagation(); deleteProjectPath(e.path,"file"); };
    row.append(pad,nm,del); frag.appendChild(row);
  }
  const wrap=document.createElement("div"); wrap.appendChild(frag); return wrap;
}
$("#collapseAll").addEventListener("click", ()=>{
  fileTree.querySelectorAll(".ftdir").forEach(r=>{ r.classList.add("collapsed-row");
    if(r.dataset.path) ftCollapsed.add(r.dataset.path); });
  fileTree.querySelectorAll(".ftchildren").forEach(k=>k.classList.add("collapsed"));
});
$("#projRefresh").addEventListener("click", ()=>{ loadTree(); });
async function deleteProjectPath(path, type){
  const what = type==="dir" ? ("folder “"+path+"” and everything in it") : ("“"+path+"”");
  const where = activeProject ? ("project “"+activeProject.name+"”") : "this chat";
  showModal("Delete "+what+" from "+where+"? This cannot be undone.", async()=>{
    const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(path),{method:"DELETE"});
    if(r.ok){
      if(editorFile===path || (type==="dir" && editorFile && editorFile.startsWith(path+"/"))){
        editorFile=null; const m=$("#editorModal"); if(m) m.hidden=true;
      }
      loadTree(); if(activeProject) refreshProjects();   // project file counts
    }
  });
}
async function openFile(path){
  const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(path));
  if(!r.ok) return;
  const f=await r.json();
  if(f.binary){ alert("“"+path+"” is a binary file and can't be edited here."); return; }
  editorFile=path;
  $("#editorSave").hidden=false; $("#editorDownload").hidden=true;   // edit mode
  $("#editorPath").textContent=(activeProject?activeProject.name:"chat")+" / "+path+(f.truncated?"  (truncated view — saving would clip)":"");
  $("#editorMsg").textContent="";
  $("#editorModal").hidden=false;
  const ro=!!f.truncated, content=f.content||"";
  if(window.CodeMirror){
    if(!cmEditor){
      cmEditor=CodeMirror.fromTextArea($("#editorArea"),
        {lineNumbers:true, theme:"dracula", indentUnit:2, viewportMargin:Infinity});
    }
    cmEditor.setOption("mode", modeForPath(path));
    cmEditor.setOption("readOnly", ro);
    cmEditor.setValue(content);
    setTimeout(()=>{ cmEditor.refresh(); cmEditor.focus(); }, 0);
  } else {
    const ta=$("#editorArea"); ta.value=content; ta.readOnly=ro; ta.focus();
  }
}
$("#editorSave").onclick=async()=>{
  if(editorFile==null) return;
  const content = cmEditor ? cmEditor.getValue() : $("#editorArea").value;
  const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(editorFile),
    {method:"PUT",headers:{"content-type":"text/plain"},body:content});
  $("#editorMsg").textContent = r.ok ? "saved ✓" : "save failed";
  if(r.ok) loadTree();
};
$("#editorClose").onclick=()=>{ $("#editorModal").hidden=true; editorFile=null; };

/* Open a generated deliverable (text/code/data) in the same popup as project
   files, but READ-ONLY with a download button instead of save. Falls back to a
   new tab if the content can't be fetched as text. */
async function openDeliverable(runId, name){
  const url="/api/output/"+runId;
  let text;
  try{
    const r=await fetch(url+"?inline=1");
    if(!r.ok) throw new Error("http "+r.status);
    text=await r.text();
  }catch(_){ window.open(url+"?inline=1","_blank","noopener"); return; }
  editorFile=null;                                   // read-only: the save handler no-ops
  $("#editorPath").textContent=name+"  (read-only)";
  $("#editorMsg").textContent="";
  $("#editorSave").hidden=true;                      // view mode
  const dl=$("#editorDownload"); dl.hidden=false; dl.href=url; dl.setAttribute("download","");
  $("#editorModal").hidden=false;
  if(window.CodeMirror){
    if(!cmEditor){
      cmEditor=CodeMirror.fromTextArea($("#editorArea"),
        {lineNumbers:true, theme:"dracula", indentUnit:2, viewportMargin:Infinity});
    }
    cmEditor.setOption("mode", modeForPath(name));
    cmEditor.setOption("readOnly", true);
    cmEditor.setValue(text);
    setTimeout(()=>{ cmEditor.refresh(); }, 0);
  } else {
    const ta=$("#editorArea"); ta.value=text; ta.readOnly=true;
  }
}
$("#projNewFile").onclick=async()=>{
  const path=prompt("New file path (e.g. notes.md):"); if(!path) return;
  await fetch(fsBase()+"/file?path="+encodeURIComponent(path),
    {method:"PUT",headers:{"content-type":"text/plain"},body:""});
  await loadTree(); openFile(path);
};
$("#projUpload").onclick=()=>$("#projFileInput").click();
$("#projFileInput").addEventListener("change", async()=>{
  if(!activeProject) return;
  const files=[...$("#projFileInput").files]; $("#projFileInput").value="";
  for(const f of files){
    const buf=await f.arrayBuffer();
    await fetch("/api/projects/"+activeProject.id+"/upload?path="+encodeURIComponent(f.name),
      {method:"POST",body:buf});
  }
  loadTree();
});

/* ---------- modal ---------- */
let modalYes=null;
function showModal(text, onYes){ $("#modalText").textContent=text; modalYes=onYes; $("#modal").classList.add("show"); }
function hideModal(){ $("#modal").classList.remove("show"); modalYes=null; }
$("#modalNo").onclick=hideModal;
$("#modalYes").onclick=async()=>{ const f=modalYes; hideModal(); if(f) await f(); };

/* ---------- tool approval ---------- */
function renderConfirm(d){
  const c=document.createElement("div"); c.className="confirm";
  c.innerHTML="<div>approve <span class='tool'>"+d.tool+"</span>?</div><pre></pre>"+
    "<div class='row'><button class='approve'>approve</button><button class='deny'>deny</button></div>";
  c.querySelector("pre").textContent=esc(d.args);
  const fin=ok=>{ c.classList.add("done");
    c.innerHTML="<span class='verdict "+(ok?"ok":"no")+"'>"+(ok?"✓ approved":"✗ denied")+
      "</span> <span class='tool'>"+d.tool+"</span>"; };
  c.querySelector(".approve").onclick=async()=>{ await approve(d.confirmation_id,true); fin(true); };
  c.querySelector(".deny").onclick=async()=>{ await approve(d.confirmation_id,false); fin(false); };
  log.appendChild(c); stick();
}
async function approve(cid, ok){
  await fetch("/api/approve/"+currentRun,{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({confirmation_id:cid, approved:ok})});
}

/* ---------- structured questions (ask.user) ---------- */
function renderQuestions(d){
  const wrap=document.createElement("div"); wrap.className="ask";
  const head=document.createElement("div"); head.className="ask-head";
  head.textContent="A few questions before continuing"; wrap.appendChild(head);
  const qEls=[];
  (d.questions||[]).forEach((q,qi)=>{
    const qd=document.createElement("div"); qd.className="ask-q";
    const qt=document.createElement("div"); qt.className="ask-qt"; qt.textContent=q.text; qd.appendChild(qt);
    const opts=q.options||[];
    if(q.type==="free_text" || !opts.length){
      const ta=document.createElement("textarea"); ta.className="ask-text"; ta.rows=2;
      ta.placeholder="Type your answer…"; qd.appendChild(ta);
      qEls.push({q, kind:"free", ta});
    } else {
      const multi=q.type==="multi_select";
      const name="q_"+d.ask_id+"_"+qi; const inputs=[];
      opts.forEach(opt=>{
        const lab=document.createElement("label"); lab.className="ask-opt";
        const inp=document.createElement("input"); inp.type=multi?"checkbox":"radio";
        inp.name=name; inp.value=opt; lab.appendChild(inp);
        const sp=document.createElement("span"); sp.textContent=opt; lab.appendChild(sp);
        qd.appendChild(lab); inputs.push(inp);
      });
      let other=null;
      if(q.allow_text){
        other=document.createElement("input"); other.type="text"; other.className="ask-text";
        other.placeholder="Other / add detail…"; qd.appendChild(other);
      }
      qEls.push({q, kind:multi?"multi":"single", inputs, other});
    }
    wrap.appendChild(qd);
  });
  const row=document.createElement("div"); row.className="row";
  const submit=document.createElement("button"); submit.className="approve"; submit.textContent="Submit answers";
  row.appendChild(submit); wrap.appendChild(row);
  const runId=currentRun;
  submit.onclick=async()=>{
    const answers={};
    for(const e of qEls){
      if(e.kind==="free"){ answers[e.q.id]={value:e.ta.value.trim(), text:""}; }
      else if(e.kind==="single"){ const c=e.inputs.find(i=>i.checked);
        answers[e.q.id]={value:c?c.value:"", text:e.other?e.other.value.trim():""}; }
      else { const vals=e.inputs.filter(i=>i.checked).map(i=>i.value);
        answers[e.q.id]={value:vals, text:e.other?e.other.value.trim():""}; }
    }
    submit.disabled=true;
    try{
      await fetch("/api/answer/"+runId,{method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({ask_id:d.ask_id, answers})});
      row.innerHTML="<span style='color:var(--muted)'>answers sent</span>";
      wrap.querySelectorAll("input,textarea").forEach(el=>el.disabled=true);
      setStatus("running…", true);
    }catch(err){ submit.disabled=false; alert("Could not send answers: "+err); }
  };
  log.appendChild(wrap); stick();
  setStatus("waiting for your answer…", true);
}

/* ---------- run / stream ---------- */
function openStream(runId){
  es=new EventSource("/api/stream/"+runId);
  const onEv = h => e => { try{ h(JSON.parse(e.data)); }catch(_){} };
  const handle = ev => { if(pending) pending.events.push(ev); applyEvent(cur, ev); };
  ["run_start","tool_selection","model_turn","tool_result","confirmation","token","cost","output","budget_warning"]
    .forEach(t=>es.addEventListener(t, onEv(handle)));
  es.addEventListener("confirmation_request", onEv(ev=>renderConfirm(ev.data)));
  es.addEventListener("questions_request", onEv(ev=>renderQuestions(ev.data)));
  es.addEventListener("run_finish", onEv(ev=>{
    if(pending) pending.events.push(ev);
    finalize(cur, ev.data);
    if(pending){ pending.answer=ev.data.answer||""; pending.status=ev.data.status; pending.run_id=ev.run_id;
      pending.trajectory=ev.data.trajectory||"";
      chat.turns.push(pending); pending=null; syncIfSaved(); persistChat();
      if(!activeProject) loadTree(); }            // show files this turn produced
    setStatus("done · "+ev.data.status, false);
    es.close(); es=null; currentRun=null; cur=null;
  }));
  es.onerror=()=>{};
}
/* ---------- chat attachments ---------- */
let pendingAttachments=[];
$("#attachBtn").addEventListener("click", ()=>$("#fileInput").click());
function extFor(mime){ return ({"image/png":".png","image/jpeg":".jpg","image/gif":".gif",
  "image/webp":".webp","image/bmp":".bmp"})[mime]||".png"; }
async function uploadFiles(files){
  for(const f of files){
    const name=f.name||("pasted-"+Date.now()+extFor(f.type));
    try{
      const r=await fetch("/api/upload?filename="+encodeURIComponent(name),{method:"POST",body:f});
      if(!r.ok){ const e=await r.json().catch(()=>({})); alert("Upload failed for "+name+": "+(e.detail||r.status)); continue; }
      pendingAttachments.push(await r.json());
    }catch(err){ alert("Upload error for "+name+": "+err); }
  }
  renderChips();
}
$("#fileInput").addEventListener("change", async ()=>{
  await uploadFiles(Array.from($("#fileInput").files||[]));
  $("#fileInput").value="";
});
// Paste images / screenshots straight into the composer (Ctrl/Cmd+V) — works
// anywhere on the page, not just when the textarea is focused. Skips when the
// file editor modal is open so pasting into code doesn't hijack the image.
document.addEventListener("paste", async e=>{
  const ed=$("#editorModal"); if(ed && !ed.hidden) return;
  const items=Array.from((e.clipboardData||{}).items||[]);
  const files=items.filter(it=>it.kind==="file" && it.type.startsWith("image/"))
                   .map(it=>it.getAsFile()).filter(Boolean);
  if(!files.length) return;            // no image -> let normal text paste happen
  e.preventDefault();
  await uploadFiles(files);
});
function renderChips(){
  const box=$("#chips"); box.innerHTML="";
  pendingAttachments.forEach((a,i)=>{
    const c=document.createElement("div"); c.className="chip";
    if(a.kind==="image"){ const im=document.createElement("img"); im.src="/api/upload/"+a.id; c.appendChild(im); }
    const nm=document.createElement("span"); nm.className="nm"; nm.textContent=a.name+" ("+fmtSize(a.size)+")"; c.appendChild(nm);
    const x=document.createElement("span"); x.className="x"; x.textContent="×"; x.title="remove";
    x.onclick=()=>{ pendingAttachments.splice(i,1); renderChips(); }; c.appendChild(x);
    box.appendChild(c);
  });
}

$("#form").addEventListener("submit", async e=>{
  e.preventDefault();
  // While a run is live, the send button is a STOP button — cancel instead of sending.
  if(currentRun){
    const rid=currentRun;
    try{ await fetch("/api/cancel/"+rid,{method:"POST"}); }catch(_){}
    setStatus("cancelling…", true);
    // Authoritative reset is the server's run_finish; but if it's delayed (cancel
    // landed during a blocking tool) or lost, force the UI back to the logo so the
    // Stop button + animation don't stick.
    setTimeout(()=>{
      if(currentRun===rid){
        try{ if(es) es.close(); }catch(_){}
        es=null; currentRun=null; cur=null;
        setStatus("cancelled", false);
      }
    }, 2500);
    return;
  }
  const msg=$("#input").value.trim();
  if(!msg && !pendingAttachments.length) return;
  $("#input").value=""; autosize(); if(typeof mdPreviewOn!=="undefined" && mdPreviewOn) setPreview(false);
  stickBottom=true;   // a new turn re-engages follow-to-bottom
  const atts=pendingAttachments.slice();
  pendingAttachments=[]; renderChips();
  if(chat.turns.length) sep("— turn "+(chat.turns.length+1)+" —");
  addMsg(msg||"(attachments)","user", atts);
  cur=startResponse();
  pending={ user_message:msg, events:[], answer:null, status:null, run_id:null };
  setStatus("running…", true);
  const history=[];
  for(const t of chat.turns){ history.push({role:"user",content:t.user_message});
    history.push({role:"assistant",content:t.answer||"",trajectory:t.trajectory||""}); }
  const r=await fetch("/api/chat",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({message:msg, history, tools:enabledTools(), share_private:$("#share").checked, auto_confirm:$("#auto").checked, think:$("#think").checked, budget_overrides:budgetOverrides(), compaction:compactionOverride(), parallel_tools:parallelOverride(), sampling:samplingOverride(), attachments:atts.map(a=>a.id), project_id:(activeProject?activeProject.id:null), conversation_id:ensureCid()})});
  currentRun=(await r.json()).run_id;
  openStream(currentRun);
});
$("#input").addEventListener("keydown", e=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault();
  if(currentRun) return;            // don't fire while a run is live (send is a stop button)
  $("#form").requestSubmit(); } });

/* auto-grow the composer with its content, up to the CSS max-height */
function autosize(){ const t=$("#input"); if(!t) return; t.style.height="auto";
  t.style.height=Math.min(t.scrollHeight, 300)+"px"; }
const _jb=$("#jumpBottom"); if(_jb) _jb.addEventListener("click", ()=>forceBottom());
updateJump();
$("#input").addEventListener("input", autosize);
autosize();

/* ---------- composer Markdown preview ----------
   Toggle the prompt box between edit (textarea) and a rendered-Markdown preview.
   The renderer escapes first, so it's safe to inject the result as HTML. */
function renderMarkdown(src){
  const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const blocks=[];
  src=String(src||"").replace(/```[ \t]*\w*\n?([\s\S]*?)```/g,(m,code)=>{
    blocks.push(esc(code.replace(/\n$/,""))); return "\u0001"+(blocks.length-1)+"\u0001"; });
  const lines=esc(src).split(/\n/), out=[]; let i=0;
  const inline=s=>s
    .replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>")
    .replace(/`([^`]+)`/g,"<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  const isBlock=l=>/^(#{1,6}\s|\s*[-*+]\s|\s*\d+\.\s|&gt;\s?|\s*\u0001\d+\u0001\s*$)/.test(l);
  while(i<lines.length){
    const ln=lines[i], m=ln.match(/^(#{1,6})\s+(.*)$/);
    if(m){ out.push("<h"+m[1].length+">"+inline(m[2])+"</h"+m[1].length+">"); i++; continue; }
    if(/^\s*\u0001\d+\u0001\s*$/.test(ln)){ out.push(ln.trim()); i++; continue; }
    if(/^&gt;\s?/.test(ln)){ const b=[]; while(i<lines.length&&/^&gt;\s?/.test(lines[i])){ b.push(inline(lines[i].replace(/^&gt;\s?/,""))); i++; } out.push("<blockquote>"+b.join("<br>")+"</blockquote>"); continue; }
    if(/^\s*[-*+]\s+/.test(ln)){ const b=[]; while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){ b.push("<li>"+inline(lines[i].replace(/^\s*[-*+]\s+/,""))+"</li>"); i++; } out.push("<ul>"+b.join("")+"</ul>"); continue; }
    if(/^\s*\d+\.\s+/.test(ln)){ const b=[]; while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){ b.push("<li>"+inline(lines[i].replace(/^\s*\d+\.\s+/,""))+"</li>"); i++; } out.push("<ol>"+b.join("")+"</ol>"); continue; }
    if(ln.trim()===""){ i++; continue; }
    const para=[]; while(i<lines.length && lines[i].trim()!=="" && !isBlock(lines[i])){ para.push(inline(lines[i])); i++; }
    out.push("<p>"+para.join("<br>")+"</p>");
  }
  return out.join("\n").replace(/\u0001(\d+)\u0001/g,(m,n)=>"<pre><code>"+blocks[+n]+"</code></pre>");
}
let mdPreviewOn=false;
function setPreview(on){
  mdPreviewOn=on;
  const ta=$("#input"), pv=$("#inputPreview"), btn=$("#mdToggle");
  if(!ta||!pv||!btn) return;
  if(on){
    pv.innerHTML = ta.value.trim() ? renderMarkdown(ta.value)
                                   : "<p style='color:var(--muted,#9aa)'>Nothing to preview yet.</p>";
    pv.hidden=false; ta.hidden=true;
    btn.classList.add("on"); btn.setAttribute("aria-pressed","true"); btn.title="Back to edit";
  } else {
    pv.hidden=true; ta.hidden=false;
    btn.classList.remove("on"); btn.setAttribute("aria-pressed","false"); btn.title="Preview Markdown";
    ta.focus();
  }
}
const _mdToggle=$("#mdToggle"); if(_mdToggle) _mdToggle.addEventListener("click", ()=>setPreview(!mdPreviewOn));
const _mdPrev=$("#inputPreview"); if(_mdPrev) _mdPrev.addEventListener("click", ()=>setPreview(false));

async function init(){
  updateSaveBtn();
  await loadTools();                       // server-side tool prefs
  await loadMe();                          // account budget prefill + placeholders
  loadModels();                            // loaded-models footer (orchestrator + coder)
  const s=applySettings();                 // localStorage settings override prefill for fields the user set
  await refreshProjects(s ? s.projectId : undefined);   // restore the selected project
  // Restore the active chat so a refresh does NOT start a new one. Only blank if
  // there's genuinely nothing to restore.
  const saved=lsGet(CHAT_KEY,null);
  if(saved && saved.turns && saved.turns.length){
    const slimRestore=()=>{
      chat={ id:saved.id||null, cid:saved.cid||null, title:saved.title||null, saved:!!saved.saved,
        turns:saved.turns.map(t=>({ user_message:t.user_message, answer:t.answer, run_id:t.run_id,
          status:t.status, trajectory:t.trajectory||"", events:[] })) };
      renderChatTurns(); updateSaveBtn(); setStatus("restored", false);
    };
    // A saved chat's full event timeline lives on the server (no longer in
    // localStorage), so pull the server copy to restore commentary + tool rows.
    // Unsaved chats restore the slim transcript (text only) — save to keep the timeline.
    if(saved.saved && saved.id){
      try{ await loadChat(saved.id); }
      catch(_){ slimRestore(); }              // server unreachable / chat gone
    } else {
      slimRestore();
    }
  }
  await refreshChats();
  ["share","auto","think","bMaxIter","bWall","bCost","bTok",
   "cCompact","cMaxChars","cKeepLast","cParallel"].forEach(id=>{
    const el=$("#"+id); if(el) el.addEventListener("change", saveSettings); });
}
init();

/* ---- Hero background: particle network (adapted from jaynet.ch) ----
   Scoped (no globals), sized to its container, mouse mapped to canvas space,
   paused when the tab is hidden, and skipped under reduced-motion. The chat
   overlays it; body.chat-active dims it to the back (see app.css). */
(function(){
  const hero = document.getElementById("hero");
  if(!hero) return;
  const canvas = hero.querySelector("canvas");
  const ctx = canvas.getContext("2d");
  const TAU = 2*Math.PI;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let balls = [], mouseX = -1e9, mouseY = -1e9, raf = null, lastTime = Date.now();

  function Ball(){
    this.x = Math.random()*canvas.width;
    this.y = Math.random()*canvas.height;
    this.vx = Math.random()*2 - 1;
    this.vy = Math.random()*2 - 1;
  }
  Ball.prototype.step = function(){
    if(this.x > canvas.width+50 || this.x < -50) this.vx = -this.vx;
    if(this.y > canvas.height+50 || this.y < -50) this.vy = -this.vy;
    this.x += this.vx; this.y += this.vy;
  };

  function resize(){
    const r = hero.getBoundingClientRect();
    canvas.width  = Math.max(1, r.width|0);
    canvas.height = Math.max(1, r.height|0);
    // density matches the original (~1 node per 65×65 px); keep existing nodes,
    // just add/trim to the new target so a resize doesn't reset the field.
    const target = Math.max(1, Math.round(canvas.width*canvas.height/(65*65)));
    while(balls.length < target) balls.push(new Ball());
    if(balls.length > target) balls.length = target;
  }

  const dist = (a,b) => Math.hypot(a.x-b.x, a.y-b.y);
  const distMouse = b => Math.hypot(b.x-mouseX, b.y-mouseY);

  function update(){
    const diff = Date.now() - lastTime;
    for(let f=0; f*16.6667 < diff; f++) for(const b of balls) b.step();
    lastTime = Date.now();
  }
  function draw(){
    const cs = getComputedStyle(hero);
    const line = (cs.getPropertyValue("--net-line")||"#8f9aa3").trim();
    const hot  = (cs.getPropertyValue("--net-hot") ||"#DAA520").trim();
    ctx.clearRect(0,0,canvas.width,canvas.height);
    for(let i=0;i<balls.length;i++){
      const b = balls[i];
      ctx.beginPath();
      for(let j=balls.length-1;j>i;j--){
        const b2 = balls[j];
        if(dist(b,b2) < 100){
          if(distMouse(b2) > 150){ ctx.strokeStyle = line; ctx.globalAlpha = .2; }
          else { ctx.globalAlpha = 0; }
          ctx.moveTo((0.5+b.x)|0,(0.5+b.y)|0);
          ctx.lineTo((0.5+b2.x)|0,(0.5+b2.y)|0);
        }
      }
      ctx.stroke();
      ctx.beginPath();
      const dm = distMouse(b);
      if(dm > 200){ ctx.fillStyle = line; ctx.globalAlpha = .2; }
      else { ctx.fillStyle = hot; ctx.globalAlpha = 1 - dm/240; }
      ctx.arc((0.5+b.x)|0,(0.5+b.y)|0,3,0,TAU,false);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  function frame(){ update(); draw(); raf = requestAnimationFrame(frame); }
  function start(){ if(raf==null && !reduce){ lastTime = Date.now(); raf = requestAnimationFrame(frame); } }
  function stop(){ if(raf!=null){ cancelAnimationFrame(raf); raf = null; } }

  // Document-level mouse, mapped into canvas space (works through the overlay).
  addEventListener("mousemove", e => {
    const r = canvas.getBoundingClientRect();
    mouseX = e.clientX - r.left; mouseY = e.clientY - r.top;
  });
  addEventListener("mouseout", e => { if(!e.relatedTarget){ mouseX = mouseY = -1e9; } });
  document.addEventListener("visibilitychange", () => document.hidden ? stop() : start());
  if(window.ResizeObserver) new ResizeObserver(resize).observe(hero);
  else addEventListener("resize", resize);

  resize();
  reduce ? draw() : start();
})();

/* Show the hero only while the chat is empty; dim it once anything is rendered. */
(function(){
  const apply = () => document.body.classList.toggle("chat-active", log.children.length > 0);
  new MutationObserver(apply).observe(log, {childList:true});
  apply();
})();
