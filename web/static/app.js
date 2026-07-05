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
let MAX_FILE_MB = 25;                          // per-file upload cap; refreshed from /api/me
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
  const val=id=>{ const e=$(id); return e?e.value:""; };
  return { share:$("#share").checked, auto:$("#auto").checked, think:$("#think").checked,
    sTemp:val("#sTemp"), sTopP:val("#sTopP"), sTopK:val("#sTopK"), sRepeat:val("#sRepeat"), sSeed:val("#sSeed"),
    bMaxIter:val("#bMaxIter"), bWall:val("#bWall"), bCost:val("#bCost"), bTok:val("#bTok"), bSubIter:val("#bSubIter"),
    cCompact:$("#cCompact").checked, cMaxChars:val("#cMaxChars"), cKeepLast:val("#cKeepLast"),
    cParallel:$("#cParallel").checked, aThresh:val("#aThresh"),
    projectId:(activeProject?activeProject.id:"") };
}
function saveSettings(){ lsSet(SET_KEY, collectSettings()); }
function applySettings(){
  const s=lsGet(SET_KEY,null); if(!s) return null;
  const ck=(id,v)=>{ if($(id)&&typeof v==="boolean") $(id).checked=v; };
  ck("#share",s.share); ck("#auto",s.auto); ck("#think",s.think);
  ck("#cCompact",s.cCompact); ck("#cParallel",s.cParallel);
  const set=(id,v)=>{ if($(id)&&v!=null&&v!=="") $(id).value=v; };  // localStorage wins for fields the user set
  set("#bMaxIter",s.bMaxIter); set("#bWall",s.bWall); set("#bCost",s.bCost); set("#bTok",s.bTok); set("#bSubIter",s.bSubIter);
  set("#cMaxChars",s.cMaxChars); set("#cKeepLast",s.cKeepLast); set("#aThresh",s.aThresh);
  set("#sTemp",s.sTemp); set("#sTopP",s.sTopP); set("#sTopK",s.sTopK); set("#sRepeat",s.sRepeat); set("#sSeed",s.sSeed);
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
    ph("#bSubIter", me.sub_iterations_default);
    if(me.max_file_mb) MAX_FILE_MB=me.max_file_mb;
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
  const host=$("#toolList"); if(!host) return;   // tool UI moved to Account → Settings → Tool access
  host.innerHTML="";
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
// Per-run sub-agent (agent.spawn) budget override. Blank => server/config default.
function subBudgetOverride(){
  const it=parseInt($("#bSubIter").value,10);
  return (Number.isFinite(it)&&it>0) ? {max_iterations:it} : null;
}

// Per-run budget overrides. Blank field => no override (server config default is used).
function archThreshold(){   // planning complexity gate (1-10); null falls back to server default
  const n=parseInt($("#aThresh").value,10);
  return (Number.isFinite(n) && n>=0 && n<=4) ? n : null;
}
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
$("#logout").onclick=async()=>{ try{ await fetch("/api/logout",{method:"POST"}); }catch(e){} location.href="/login"; };

/* ---------- side panels: collapse on desktop, drawers on mobile ---------- */
function isNarrow(){ return innerWidth<=900; }
function closeDrawers(){ document.body.classList.remove("show-chats","show-tools","show-proj"); }
function drawer(name){ const cls="show-"+name, on=document.body.classList.contains(cls);
  closeDrawers(); if(!on) document.body.classList.add(cls); }   // mobile: one drawer at a time
$("#chatsToggle").addEventListener("click", ()=>{
  if(isNarrow()) drawer("chats"); else document.body.classList.toggle("collapse-chats");
});

/* ---------- quick-settings popover (left of the send button) ---------- */
(function initQuickSettings(){
  const btn=$("#qsBtn"), pop=$("#qsPop"); if(!btn||!pop) return;
  const close=()=>{ pop.hidden=true; btn.setAttribute("aria-expanded","false"); };
  const open =()=>{ pop.hidden=false; btn.setAttribute("aria-expanded","true"); };
  btn.addEventListener("click", (e)=>{ e.stopPropagation(); pop.hidden?open():close(); });
  document.addEventListener("click", (e)=>{
    if(!pop.hidden && e.target!==btn && !btn.contains(e.target) && !pop.contains(e.target)) close(); });
  document.addEventListener("keydown", (e)=>{ if(e.key==="Escape" && !pop.hidden) close(); });
})();
/* When settings change elsewhere (the account Settings tab writes the same
   localStorage key), re-apply them here so both views stay in sync. */
window.addEventListener("storage", (e)=>{ if(e.key===SET_KEY) applySettings(); });
$("#drawerScrim").addEventListener("click", closeDrawers);
addEventListener("keydown", e=>{ if(e.key==="Escape") closeDrawers(); });
chatList.addEventListener("click", ()=>{ if(isNarrow()) closeDrawers(); });
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
  // The final answer, rendered rich: prose as Markdown and each fenced code
  // block in its own box, every block with copy / download / save-to-folder.
  // Prefer the authoritative d.answer; fall back to the streamed text.
  const answerText = (d.answer != null && d.answer !== "") ? d.answer
                     : (c.cur && c.cur.textContent.trim() ? c.cur.textContent : "");
  if (c.cur) { c.cur.remove(); c.cur = null; }
  const a = document.createElement("div"); a.className = "seg answer";
  if (answerText && answerText.trim()) { a.classList.add("rich"); a.appendChild(renderAnswer(answerText)); }
  else a.textContent = "(no answer)";
  c.flow.appendChild(a);
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
$("#newChatTop").onclick=()=>{
  if(isNarrow()) closeDrawers();
  // Explicit new chat is the ONLY thing that starts fresh. If the current chat was
  // never saved it lived only here + in localStorage, so clearing it IS the delete.
  // A saved chat keeps its server copy in the list; we just detach from it.
  chat={ id:null, cid:null, title:null, saved:false, turns:[] };
  pending=null; currentRun=null; cur=null; log.innerHTML="";
  lsDel(CHAT_KEY);
  setStatus("idle", false); updateSaveBtn(); refreshChats();
  if(!activeProject) refreshFiles();           // reset the per-chat file counter
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
const projSelect=$("#projSelect");
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
  $("#projSection").classList.toggle("chat-mode", !activeProject);
  refreshFiles();                              // update the top-bar file counter (+ modal if open)
  // header indicator next to the status: a clickable project-name chip
  const chip=$("#projActive");
  chip.hidden=false;
  const nm=chip.querySelector(".chip-name");
  if(activeProject){
    chip.classList.remove("none");
    if(nm) nm.textContent=activeProject.name;
    chip.title="Project: "+activeProject.name+" — click to open files";
  } else {
    chip.classList.add("none");
    if(nm) nm.textContent="no project";
    chip.title="No project — files live in this chat. Click to open files.";
  }
}
$("#projActive").addEventListener("click", ()=>{ fmOpen(); });
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
/* ================= workspace file manager (modal) ================= */
// Active filespace: a project, else this chat's scratch workspace. Both expose
// the same {entries:[{path,type,size}]} list + /file CRUD + /mkdir + /rename.
function fsBase(){ return activeProject ? ("/api/projects/"+activeProject.id)
                                        : ("/api/chat-scratch/"+ensureCid()); }

let fmEntries=[];             // last-loaded flat entries [{path,type,size}]
let fmSel=new Set();          // selected paths (files and/or folders)
let fmCollapsed=new Set();    // collapsed folder paths (kept across renders)
let fmOrder=[];               // visible paths in render order (for shift-range select)
let fmAnchor=null;            // last-picked path (shift-range anchor)

// Fetch the list, update the top-bar counter, and re-render the modal if open.
async function refreshFiles(){
  let entries=[];
  try{ const r=await fetch(fsBase()+"/files"); if(r.ok) entries=(await r.json()).entries||[]; }
  catch(_){ }
  fmEntries=entries;
  const n=entries.filter(e=>e.type!=="dir").length;
  const badge=$("#filesCount"); if(badge){ badge.textContent=n; badge.hidden=!(n>0); }
  const chip=$("#projActive");
  if(chip) chip.title = (activeProject?("Project: "+activeProject.name):"No project — files live in this chat")
    + " · " + (n?(n+" file"+(n===1?"":"s")):"no files yet") + " — click to open";
  if(!$("#filesModal").hidden) renderFm();
}

function fmOpen(){
  $("#fmWhere").textContent = activeProject ? ("Project · "+activeProject.name) : "Current chat";
  $("#filesModal").hidden=false;
  refreshFiles();
}
function fmClose(){ $("#filesModal").hidden=true; fmUpHide(); }

function renderFm(){
  const tree=$("#fmTree"), entries=fmEntries;
  const valid=new Set(entries.map(e=>e.path));
  for(const p of [...fmSel]) if(!valid.has(p)) fmSel.delete(p);   // prune deleted selections
  fmOrder=[];
  if(!entries.length){
    tree.innerHTML="<div class='fm-empty'>"+(activeProject
      ? "empty — use ＋ file, ＋ folder, or ⬆ upload"
      : "files the agent creates in this chat will appear here")+"</div>";
    fmSyncToolbar(); return;
  }
  const root={dirs:new Map(),files:[],path:null};
  const dnode=(parts)=>{ let n=root;
    for(let i=0;i<parts.length;i++){ const p=parts[i];
      if(!n.dirs.has(p)) n.dirs.set(p,{dirs:new Map(),files:[],path:parts.slice(0,i+1).join("/")});
      n=n.dirs.get(p);} return n; };
  for(const e of entries){ const parts=e.path.split("/");
    if(e.type==="dir") dnode(parts);
    else (parts.length>1?dnode(parts.slice(0,-1)):root).files.push(e); }
  tree.innerHTML=""; tree.appendChild(fmDir(root,0));
  fmSyncToolbar();
}

function fmDir(node, depth){
  const frag=document.createDocumentFragment();
  for(const name of [...node.dirs.keys()].sort((a,b)=>a.localeCompare(b))){
    const child=node.dirs.get(name), dp=child.path||name, collapsed=fmCollapsed.has(dp);
    frag.appendChild(fmRow(dp,"dir",name,0,depth,collapsed));
    fmOrder.push(dp);
    const kids=document.createElement("div"); kids.className="fm-kids"+(collapsed?" hidden":"");
    kids.appendChild(fmDir(child, depth+1));
    frag.appendChild(kids);
  }
  for(const e of node.files.slice().sort((a,b)=>a.path.localeCompare(b.path))){
    frag.appendChild(fmRow(e.path,"file",e.path.split("/").pop(),e.size,depth,false));
    fmOrder.push(e.path);
  }
  const wrap=document.createElement("div"); wrap.appendChild(frag); return wrap;
}

function fmRow(path,type,name,size,depth,collapsed){
  const row=document.createElement("div");
  row.className="fm-row fm-"+type+(fmSel.has(path)?" sel":"");
  row.dataset.path=path; row.style.paddingLeft=(6+depth*15)+"px";
  const cb=document.createElement("input"); cb.type="checkbox"; cb.className="fm-cb"; cb.checked=fmSel.has(path);
  cb.onclick=ev=>{ ev.stopPropagation(); fmPick(path, ev.shiftKey); };
  const caret=document.createElement("span"); caret.className="fm-caret";
  caret.textContent = type==="dir" ? (collapsed?"▸":"▾") : "";
  const nm=document.createElement("span"); nm.className="fm-name";
  nm.textContent=(type==="dir"?"📁 ":"📄 ")+name;
  nm.title = type==="file" ? (path+" · "+fmtSize(size)) : path;
  row.append(cb,caret,nm);
  if(type==="file"){ const sz=document.createElement("span"); sz.className="fm-size"; sz.textContent=fmtSize(size); row.append(sz); }
  row.onclick=(ev)=>{
    if(ev.target===cb) return;
    if(ev.shiftKey || ev.ctrlKey || ev.metaKey){ ev.preventDefault(); fmPick(path, ev.shiftKey); return; }
    if(type==="dir"){ if(fmCollapsed.has(path)) fmCollapsed.delete(path); else fmCollapsed.add(path); renderFm(); }
    else openFile(path);
  };
  return row;
}

// Toggle a path; shift extends a contiguous range from the last anchor in visible order.
function fmPick(path, range){
  if(range && fmAnchor){
    const a=fmOrder.indexOf(fmAnchor), b=fmOrder.indexOf(path);
    if(a>=0 && b>=0){ const lo=Math.min(a,b), hi=Math.max(a,b);
      for(let i=lo;i<=hi;i++) fmSel.add(fmOrder[i]); }
  } else {
    if(fmSel.has(path)) fmSel.delete(path); else fmSel.add(path);
    fmAnchor=path;
  }
  renderFm();
}

function fmSyncToolbar(){
  const n=fmSel.size;
  $("#fmRename").disabled = n!==1;
  $("#fmDelete").disabled = n===0;
  $("#fmSelInfo").textContent = n ? (n+" selected") : "";
  const all=$("#fmSelAll"); if(all) all.checked = fmOrder.length>0 && fmSel.size>=fmOrder.length;
  const nf=fmEntries.filter(e=>e.type!=="dir").length, nd=fmEntries.filter(e=>e.type==="dir").length;
  $("#fmCount").textContent = fmEntries.length ? (nf+" file"+(nf===1?"":"s")+", "+nd+" folder"+(nd===1?"":"s")) : "";
}

/* ---- toolbar operations ---- */
async function fmDelete(){
  const items=[...fmSel]; if(!items.length) return;
  const where = activeProject ? ("project “"+activeProject.name+"”") : "this chat";
  showModal("Delete "+items.length+" item"+(items.length>1?"s":"")+" from "+where+"? "+
            "Folders are removed with all their contents. This cannot be undone.", async()=>{
    for(const p of items){
      const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(p),{method:"DELETE"});
      if(r.ok && (editorFile===p || (editorFile&&editorFile.startsWith(p+"/")))){ editorFile=null; $("#editorModal").hidden=true; }
    }
    fmSel.clear();
    await refreshFiles(); if(activeProject) refreshProjects();
  });
}
async function fmNewFile(){
  const path=prompt("New file path (e.g. notes.md or sub/dir/file.txt):"); if(!path) return;
  const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(path),
    {method:"PUT",headers:{"content-type":"text/plain"},body:""});
  if(!r.ok){ alert("Could not create file."); return; }
  await refreshFiles(); if(activeProject) refreshProjects(); openFile(path);
}
async function fmNewFolder(){
  const path=prompt("New folder path (e.g. drafts or 2026/reports):"); if(!path) return;
  const r=await fetch(fsBase()+"/mkdir",
    {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({path})});
  if(!r.ok){ alert("Could not create folder."); return; }
  await refreshFiles();
}
async function fmRename(){
  if(fmSel.size!==1) return;
  const from=[...fmSel][0];
  const to=prompt("Rename / move — new path:", from); if(!to || to===from) return;
  const r=await fetch(fsBase()+"/rename",
    {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({from,to})});
  if(!r.ok){ const d=await r.json().catch(()=>({})); alert("Rename failed: "+(d.detail||("HTTP "+r.status))); return; }
  if(editorFile===from) editorFile=to;
  fmSel.clear(); fmSel.add(to); fmAnchor=to;
  await refreshFiles(); if(activeProject) refreshProjects();
}
// Upload with a live progress bar + per-file success/error, and a client-side
// size pre-check so oversize files fail instantly instead of silently.
function fmUpHide(){ const b=$("#fmUp"); if(b){ b.hidden=true; b.innerHTML=""; } }
function fmUpRow(id, name){
  let row=document.getElementById(id);
  if(!row){
    row=document.createElement("div"); row.className="fm-uprow"; row.id=id;
    row.innerHTML='<span class="nm"></span><div class="bar"><i></i></div><span class="st"></span>';
    row.querySelector(".nm").textContent=name;
    $("#fmUp").appendChild(row);
  }
  return row;
}
function fmUpProgress(id, name, frac){
  const row=fmUpRow(id,name); row.querySelector(".bar>i").style.width=Math.round(frac*100)+"%";
  row.querySelector(".st").textContent=Math.round(frac*100)+"%";
}
function fmUpDone(id, name, ok, msg){
  const row=fmUpRow(id,name); row.classList.toggle("ok",ok); row.classList.toggle("err",!ok);
  row.querySelector(".bar>i").style.width="100%";
  const st=row.querySelector(".st"); st.textContent=(ok?"✓ ":"✕ ")+msg; st.title=msg;
}
function fmUploadOne(id, file){
  return new Promise((resolve)=>{
    const xhr=new XMLHttpRequest();
    xhr.open("PUT", fsBase()+"/file?path="+encodeURIComponent(file.name));
    xhr.upload.onprogress=(e)=>{ if(e.lengthComputable) fmUpProgress(id, file.name, e.loaded/e.total); };
    xhr.onload=()=>{
      if(xhr.status>=200 && xhr.status<300) resolve({ok:true});
      else{ let d="HTTP "+xhr.status; try{ d=JSON.parse(xhr.responseText).detail||d; }catch(_){}
            resolve({ok:false, error: xhr.status===413 ? ("too large (max "+MAX_FILE_MB+" MB)") : d}); }
    };
    xhr.onerror=()=>resolve({ok:false, error:"network error"});
    xhr.send(file);
  });
}
async function fmUploadFiles(files){
  const cap=MAX_FILE_MB*1024*1024, box=$("#fmUp");
  box.hidden=false; box.innerHTML="";
  let ok=0, bad=0, i=0;
  for(const f of files){
    const id="up_"+(i++);
    if(f.size>cap){ fmUpDone(id, f.name, false, "too large — "+fmtSize(f.size)+" > "+MAX_FILE_MB+" MB limit"); bad++; continue; }
    fmUpProgress(id, f.name, 0);
    const r=await fmUploadOne(id, f);
    if(r.ok){ fmUpDone(id, f.name, true, fmtSize(f.size)+" uploaded"); ok++; }
    else    { fmUpDone(id, f.name, false, r.error); bad++; }
  }
  await refreshFiles(); if(activeProject) refreshProjects();
  if(!bad) setTimeout(fmUpHide, 2500);          // all good → auto-dismiss; keep errors visible
}

/* ---- wire the modal ---- */
$("#fmClose").onclick=fmClose;
$("#fmRefresh").onclick=refreshFiles;
$("#fmNewFile").onclick=fmNewFile;
$("#fmNewFolder").onclick=fmNewFolder;
$("#fmRename").onclick=fmRename;
$("#fmDelete").onclick=fmDelete;
$("#fmUpload").onclick=()=>$("#fmFileInput").click();
$("#fmFileInput").addEventListener("change", async()=>{
  const fs=[...$("#fmFileInput").files]; $("#fmFileInput").value=""; if(fs.length) await fmUploadFiles(fs); });
$("#fmSelAll").addEventListener("change",(e)=>{
  if(e.target.checked) fmOrder.forEach(p=>fmSel.add(p)); else fmSel.clear(); renderFm(); });
$("#filesModal").addEventListener("click",(e)=>{ if(e.target.id==="filesModal") fmClose(); });  // backdrop closes
document.addEventListener("keydown",(e)=>{
  if($("#filesModal").hidden) return;
  const t=e.target, typing = t && (t.tagName==="INPUT"||t.tagName==="TEXTAREA"||t.isContentEditable);
  if(e.key==="Escape") fmClose();
  else if(e.key==="Delete" && !typing && fmSel.size){ e.preventDefault(); fmDelete(); }
});

/* ---- file editor (opened from the manager, or read-only for deliverables) ---- */
async function openFile(path){
  const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(path));
  if(!r.ok) return;
  const f=await r.json();
  if(f.binary){ alert("“"+path+"” is a binary file and can't be edited here."); return; }
  editorFile=path;
  $("#editorSave").hidden=false; $("#editorDownload").hidden=true;
  $("#editorPath").textContent=(activeProject?activeProject.name:"chat")+" / "+path+(f.truncated?"  (truncated view — saving would clip)":"");
  $("#editorMsg").textContent=""; $("#editorModal").hidden=false;
  const ro=!!f.truncated, content=f.content||"";
  if(window.CodeMirror){
    if(!cmEditor) cmEditor=CodeMirror.fromTextArea($("#editorArea"),
      {lineNumbers:true,theme:"dracula",indentUnit:2,viewportMargin:Infinity});
    cmEditor.setOption("mode",modeForPath(path)); cmEditor.setOption("readOnly",ro);
    cmEditor.setValue(content); setTimeout(()=>{cmEditor.refresh();cmEditor.focus();},0);
  } else { const ta=$("#editorArea"); ta.value=content; ta.readOnly=ro; ta.focus(); }
}
$("#editorSave").onclick=async()=>{
  if(editorFile==null) return;
  const content=cmEditor?cmEditor.getValue():$("#editorArea").value;
  const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(editorFile),
    {method:"PUT",headers:{"content-type":"text/plain"},body:content});
  $("#editorMsg").textContent=r.ok?"saved ✓":"save failed";
  if(r.ok){ refreshFiles(); if(activeProject) refreshProjects(); }
};
$("#editorClose").onclick=()=>{ $("#editorModal").hidden=true; editorFile=null; };

/* Open a generated deliverable in the same editor popup, READ-ONLY + download. */
async function openDeliverable(runId, name){
  const url="/api/output/"+runId; let text;
  try{ const r=await fetch(url+"?inline=1"); if(!r.ok) throw new Error("http "+r.status); text=await r.text(); }
  catch(_){ window.open(url+"?inline=1","_blank","noopener"); return; }
  editorFile=null;
  $("#editorPath").textContent=name+"  (read-only)"; $("#editorMsg").textContent="";
  $("#editorSave").hidden=true;
  const dl=$("#editorDownload"); dl.hidden=false; dl.href=url; dl.setAttribute("download","");
  $("#editorModal").hidden=false;
  if(window.CodeMirror){
    if(!cmEditor) cmEditor=CodeMirror.fromTextArea($("#editorArea"),
      {lineNumbers:true,theme:"dracula",indentUnit:2,viewportMargin:Infinity});
    cmEditor.setOption("mode",modeForPath(name)); cmEditor.setOption("readOnly",true);
    cmEditor.setValue(text); setTimeout(()=>{cmEditor.refresh();},0);
  } else { const ta=$("#editorArea"); ta.value=text; ta.readOnly=true; }
}
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
      if(!activeProject) refreshFiles(); }         // show files this turn produced
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
/* ---------- smart paste: rich text -> markdown source ----------
   When the clipboard carries formatted text (a web page, a doc, rendered
   markdown), convert its HTML to markdown SOURCE and drop that at the cursor,
   so structure survives instead of being flattened. Plain text and raw markdown
   paste unchanged; image paste (handled below) is left alone. */
function htmlToMarkdown(html){
  const root=document.createElement("div");
  root.innerHTML=html;
  root.querySelectorAll("script,style,noscript,head,meta,link,title").forEach(n=>n.remove());

  const inline = el => walk(el).replace(/\s+/g," ");
  function listBlock(el, ordered){
    let n=1; const lines=[];
    Array.from(el.children).filter(c=>c.tagName && c.tagName.toLowerCase()==="li").forEach(li=>{
      const marker = ordered ? (n++)+". " : "- ";
      const parts = walk(li).trim().split("\n");
      lines.push(marker + (parts.shift()||""));
      parts.forEach(p=> lines.push(p.length ? "  "+p : ""));   // indent continuations/nested
    });
    return lines.join("\n");
  }
  function tableBlock(tbl){
    const rows=Array.from(tbl.querySelectorAll("tr")); if(!rows.length) return "";
    const cells = tr => Array.from(tr.querySelectorAll("th,td")).map(c=>inline(c).trim().replace(/\|/g,"\\|"));
    const head=cells(rows[0]);
    const out=["| "+head.join(" | ")+" |", "| "+head.map(()=>"---").join(" | ")+" |"];
    rows.slice(1).forEach(tr=>{ const c=cells(tr); if(c.length) out.push("| "+c.join(" | ")+" |"); });
    return out.join("\n");
  }
  function applyInlineStyle(el, s){
    const st=el.style; if(!st) return s;
    const lead=(s.match(/^\s*/)||[""])[0], trail=(s.match(/\s*$/)||[""])[0];
    let core=s.slice(lead.length, s.length-trail.length);
    if(!core) return s;
    const fw=(st.fontWeight||"")+"";
    if(fw==="bold"||fw==="bolder"||parseInt(fw,10)>=600) core="**"+core+"**";
    const fs=(st.fontStyle||"");
    if(fs==="italic"||fs==="oblique") core="*"+core+"*";
    if(/line-through/.test((st.textDecorationLine||"")+(st.textDecoration||""))) core="~~"+core+"~~";
    return lead+core+trail;
  }
  function elem(el){
    switch(el.tagName.toLowerCase()){
      case "h1": return "\n\n# "     +inline(el).trim()+"\n\n";
      case "h2": return "\n\n## "    +inline(el).trim()+"\n\n";
      case "h3": return "\n\n### "   +inline(el).trim()+"\n\n";
      case "h4": return "\n\n#### "  +inline(el).trim()+"\n\n";
      case "h5": return "\n\n##### " +inline(el).trim()+"\n\n";
      case "h6": return "\n\n###### "+inline(el).trim()+"\n\n";
      case "strong": case "b":       { const s=inline(el).trim(); return s?"**"+s+"**":""; }
      case "em": case "i":           { const s=inline(el).trim(); return s?"*"+s+"*":""; }
      case "s": case "del": case "strike": { const s=inline(el).trim(); return s?"~~"+s+"~~":""; }
      case "code": return "`"+el.textContent+"`";
      case "pre":  return "\n\n```\n"+el.textContent.replace(/\n+$/,"")+"\n```\n\n";
      case "a":    { const h=(el.getAttribute("href")||"").trim(); const t=inline(el).trim()||h; return h?"["+t+"]("+h+")":t; }
      case "img":  { const alt=(el.getAttribute("alt")||"").trim(); const src=(el.getAttribute("src")||"").trim(); return (src&&/^(https?:|data:)/.test(src))?"!["+alt+"]("+src+")":alt; }
      case "br":   return "\n";
      case "hr":   return "\n\n---\n\n";
      case "p": case "div": { const s=walk(el).trim(); return s?"\n\n"+s+"\n\n":""; }
      case "blockquote":    { const t=walk(el).trim(); return t?"\n\n"+t.split("\n").map(l=>"> "+l).join("\n")+"\n\n":""; }
      case "ul": return "\n\n"+listBlock(el,false)+"\n\n";
      case "ol": return "\n\n"+listBlock(el,true)+"\n\n";
      case "table": return "\n\n"+tableBlock(el)+"\n\n";
      default: return applyInlineStyle(el, walk(el));   // span/font: honor style-based bold/italic
    }
  }
  function walk(node){
    let out="";
    node.childNodes.forEach(ch=>{
      if(ch.nodeType===3) out += ch.nodeValue.replace(/\s+/g," ");
      else if(ch.nodeType===1) out += elem(ch);
    });
    return out;
  }
  return walk(root).replace(/[ \t]+\n/g,"\n").replace(/\n{3,}/g,"\n\n").replace(/^\s+|\s+$/g,"");
}

function insertIntoInput(text){
  const el=$("#input"); if(!el) return;
  el.focus();
  let done=false;
  try{ done = document.execCommand && document.execCommand("insertText", false, text); }catch(e){ done=false; }
  if(!done){                                  // fallback for contenteditable: insert at caret
    const sel=getSelection();
    if(sel && sel.rangeCount){
      const r=sel.getRangeAt(0); r.deleteContents();
      const node=document.createTextNode(text); r.insertNode(node);
      r.setStartAfter(node); r.collapse(true); sel.removeAllRanges(); sel.addRange(r);
    } else { el.appendChild(document.createTextNode(text)); }
  }
  composerRender();                           // normalise + apply live styling
}

$("#input").addEventListener("paste", e=>{
  const cd=e.clipboardData; if(!cd) return;
  // leave image paste to the handler below
  if(Array.from(cd.items||[]).some(it=>it.kind==="file" && it.type.startsWith("image/"))) return;
  const html = cd.getData ? cd.getData("text/html") : "";
  if(!html || !html.trim()) return;           // plain text / raw markdown -> default paste
  const md = htmlToMarkdown(html);
  if(!md) return;
  e.preventDefault();
  insertIntoInput(md);
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
  const msg=composerText().trim();
  if(!msg && !pendingAttachments.length) return;
  composerClear();
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
    body:JSON.stringify({message:msg, history, tools:enabledTools(), share_private:$("#share").checked, auto_confirm:$("#auto").checked, think:$("#think").checked, budget_overrides:budgetOverrides(), compaction:compactionOverride(), parallel_tools:parallelOverride(), sampling:samplingOverride(), sub_budget:subBudgetOverride(), architect_threshold:archThreshold(), attachments:atts.map(a=>a.id), project_id:(activeProject?activeProject.id:null), conversation_id:ensureCid()})});
  currentRun=(await r.json()).run_id;
  openStream(currentRun);
});
$("#input").addEventListener("keydown", e=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault();
  if(currentRun) return;            // don't fire while a run is live (send is a stop button)
  $("#form").requestSubmit(); } });

/* the composer auto-grows via CSS (min/max-height + overflow-y); autosize is kept
   as a no-op so existing callers (paste, etc.) don't break. */
function autosize(){}
const _jb=$("#jumpBottom"); if(_jb) _jb.addEventListener("click", ()=>forceBottom());
updateJump();

/* ===================== rich composer engine =====================
   #input is contenteditable. The user types plain Markdown; we re-style it live
   on every input while keeping the *text content* equal to the raw Markdown
   (markers are shrunk to zero width, not removed) so: (a) the model receives
   exactly what was typed, and (b) caret math over text nodes stays 1:1 with the
   source. Lists indent and show a • ; #, **, _, ` are hidden and their content
   styled. */
let _ceComposing=false;
function _ceEsc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
const _BT=String.fromCharCode(96);   // backtick, kept out of template literals

// Inline: wrap **bold**, _italic_/*italic*, `code` — markers hidden, source intact.
function _ceInline(t){
  t=_ceEsc(t);
  const codeRe=new RegExp(_BT+"([^"+_BT+"\\n]+)"+_BT,"g");
  t=t.replace(codeRe,(m,x)=>'<span class="mk">'+_BT+'</span><code>'+x+'</code><span class="mk">'+_BT+'</span>');
  t=t.replace(/\*\*([^*\n]+)\*\*/g,(m,x)=>'<span class="mk">**</span><b>'+x+'</b><span class="mk">**</span>');
  t=t.replace(/(^|[^\w*])_([^_\n]+)_(?=[^\w]|$)/g,(m,p,x)=>p+'<span class="mk">_</span><i>'+x+'</i><span class="mk">_</span>');
  t=t.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,(m,p,x)=>p+'<span class="mk">*</span><i>'+x+'</i><span class="mk">*</span>');
  return t;
}

// One source line -> {cls, style, html}. Preserves every source char in the text.
function _ceLine(line){
  let m=line.match(/^(#{1,6})(\s+)(.*)$/);
  if(m){ const lvl=Math.min(m[1].length,3);
    return {cls:"h"+lvl, style:"", html:'<span class="mk">'+_ceEsc(m[1]+m[2])+'</span>'+_ceInline(m[3])}; }
  const lead=(line.match(/^[ \t]*/)||[""])[0];
  const rest=line.slice(lead.length);
  const baseLevels=Math.floor(lead.replace(/\t/g,"  ").length/2);
  m=rest.match(/^([-*])(\s+)(.*)$/);          // unordered -> • + indent
  if(m){ const depth=baseLevels+1;
    return {cls:"ul", style:"padding-left:"+(depth*1.3).toFixed(2)+"em",
      html:'<span class="mk">'+_ceEsc(lead)+'</span><span class="bul">'+_ceEsc(m[1]+m[2])+'</span>'+_ceInline(m[3])}; }
  m=rest.match(/^(\d+(?:\.\d+)*[.)]?|[A-Za-z][.)]|[ivxlcdmIVXLCDM]+[.)])(\s+)(.*)$/);   // ordered/lettered/roman -> indent, marker kept
  if(m){ const marker=m[1], seg=(marker.match(/\d+/g)||[]).length, depth=baseLevels+Math.max(seg,1);
    return {cls:"ol", style:"padding-left:"+(depth*1.3).toFixed(2)+"em",
      html:'<span class="mk">'+_ceEsc(lead)+'</span><span class="olm">'+_ceEsc(marker+m[2])+'</span>'+_ceInline(m[3])}; }
  return {cls:"", style:"", html:_ceInline(line)};
}

function _ceRenderLines(text){
  return text.split("\n").map(line=>{
    const r=_ceLine(line);
    return '<div class="cl'+(r.cls?(" "+r.cls):"")+'"'+(r.style?(' style="'+r.style+'"'):"")+'>'+(r.html||"<br>")+'</div>';
  }).join("");
}

// Read the (possibly browser-dirtied) DOM back to source, optionally tracking caret.
function _ceRead(withCaret){
  const el=$("#input"); const sel=withCaret?getSelection():null;
  const cont=(sel&&sel.rangeCount)?sel.getRangeAt(0).endContainer:null;
  const coff=(sel&&sel.rangeCount)?sel.getRangeAt(0).endOffset:0;
  let s="", caret=null;
  const rec=(node)=>{
    const kids=node.childNodes;
    for(let i=0;i<kids.length;i++){
      if(withCaret && cont===node && i===coff && caret===null) caret=s.length;
      const n=kids[i];
      if(n.nodeType===3){
        if(withCaret && cont===n) caret=s.length+Math.min(coff,n.nodeValue.length);
        s+=n.nodeValue;
      } else if(n.nodeType===1){
        if(n.tagName==="BR"){ s+="\n"; }
        else{ const block=(n.tagName==="DIV"||n.tagName==="P");
          if(block && s.length && !s.endsWith("\n")) s+="\n";
          rec(n);
          if(block && !s.endsWith("\n")) s+="\n"; }
      }
    }
    if(withCaret && cont===node && coff===kids.length && caret===null) caret=s.length;
  };
  rec(el);
  s=s.replace(/\u00a0/g," ");
  if(s.endsWith("\n")) s=s.slice(0,-1);
  if(caret===null) caret=s.length;
  return {text:s, caret:Math.min(caret,s.length)};
}

function _ceLineLen(line){ let n=0;
  (function w(x){ x.childNodes.forEach(c=>{ if(c.nodeType===3)n+=c.nodeValue.length;
    else if(c.nodeType===1 && c.tagName!=="BR") w(c); }); })(line); return n; }

function _ceSetCaretInLine(line, off){
  const range=document.createRange(), sel=getSelection(); let rem=off, target=null, tOff=0;
  (function w(x){ for(const c of x.childNodes){ if(target) return;
    if(c.nodeType===3){ if(rem<=c.nodeValue.length){ target=c; tOff=rem; return; } rem-=c.nodeValue.length; }
    else if(c.nodeType===1 && c.tagName!=="BR") w(c); } })(line);
  if(target) range.setStart(target,tOff); else range.setStart(line,0);
  range.collapse(true); sel.removeAllRanges(); sel.addRange(range);
}

function _cePlaceCaret(pos){
  const lines=[...$("#input").children]; if(!lines.length) return;
  let rem=Math.max(0,pos);
  for(let i=0;i<lines.length;i++){
    const len=_ceLineLen(lines[i]);
    if(rem<=len || i===lines.length-1){ _ceSetCaretInLine(lines[i], Math.min(rem,len)); return; }
    rem-=len+1;
    if(rem<0){ _ceSetCaretInLine(lines[i], len); return; }
  }
}

function composerText(){ return _ceRead(false).text; }
function composerClear(){ const el=$("#input"); if(el){ el.innerHTML=""; el.blur?.(); } }
function composerRender(){
  const el=$("#input"); if(!el) return;
  const {text,caret}=_ceRead(true);
  if(text===""){ el.innerHTML=""; return; }     // empty -> show placeholder
  el.innerHTML=_ceRenderLines(text);
  _cePlaceCaret(caret);
}

$("#input").addEventListener("input", ()=>{ if(_ceComposing) return; composerRender(); });
$("#input").addEventListener("compositionstart", ()=>{ _ceComposing=true; });
$("#input").addEventListener("compositionend", ()=>{ _ceComposing=false; composerRender(); });

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
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(ln)){ out.push("<hr>"); i++; continue; }
    if(/^\s*\u0001\d+\u0001\s*$/.test(ln)){ out.push(ln.trim()); i++; continue; }
    if(/^&gt;\s?/.test(ln)){ const b=[]; while(i<lines.length&&/^&gt;\s?/.test(lines[i])){ b.push(inline(lines[i].replace(/^&gt;\s?/,""))); i++; } out.push("<blockquote>"+b.join("<br>")+"</blockquote>"); continue; }
    if(/^\s*[-*+]\s+/.test(ln)){ const b=[]; while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){ b.push("<li>"+inline(lines[i].replace(/^\s*[-*+]\s+/,""))+"</li>"); i++; } out.push("<ul>"+b.join("")+"</ul>"); continue; }
    if(/^\s*\d+\.\s+/.test(ln)){ const b=[]; while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){ b.push("<li>"+inline(lines[i].replace(/^\s*\d+\.\s+/,""))+"</li>"); i++; } out.push("<ol>"+b.join("")+"</ol>"); continue; }
    if(/\|/.test(ln) && i+1<lines.length && /^\s*\|?[ :|-]*-[ :|-]*\|?\s*$/.test(lines[i+1])){
      const cells=r=>r.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
      let t="<table><thead><tr>"+cells(ln).map(c=>"<th>"+inline(c)+"</th>").join("")+"</tr></thead><tbody>";
      i+=2;
      while(i<lines.length && /\|/.test(lines[i]) && lines[i].trim()!==""){
        t+="<tr>"+cells(lines[i]).map(c=>"<td>"+inline(c)+"</td>").join("")+"</tr>"; i++; }
      out.push(t+"</tbody></table>"); continue;
    }
    if(ln.trim()===""){ i++; continue; }
    const para=[]; while(i<lines.length && lines[i].trim()!=="" && !isBlock(lines[i])){ para.push(inline(lines[i])); i++; }
    out.push("<p>"+para.join("<br>")+"</p>");
  }
  return out.join("\n").replace(/\u0001(\d+)\u0001/g,(m,n)=>"<pre><code>"+blocks[+n]+"</code></pre>");
}
let mdPreviewOn=false;

/* ============ rich answer rendering: prose + code blocks, each saveable ============
   Split the final answer on ``` fences: prose runs render as Markdown (.md), code
   runs render in a labelled box. Every block gets copy / download / save-to-folder,
   where "folder" is the current workspace (project or this chat's scratch). */
const _EXT = {python:"py",py:"py",javascript:"js",js:"js",jsx:"jsx",typescript:"ts",ts:"ts",
  tsx:"tsx",bash:"sh",sh:"sh",shell:"sh",zsh:"sh",json:"json",yaml:"yml",yml:"yml",toml:"toml",
  html:"html",xml:"xml",css:"css",scss:"scss",sql:"sql",rust:"rs",go:"go",c:"c",h:"h",cpp:"cpp",
  cc:"cpp",java:"java",kotlin:"kt",swift:"swift",ruby:"rb",rb:"rb",php:"php",r:"r",lua:"lua",
  dockerfile:"Dockerfile",make:"mk",makefile:"mk",diff:"diff",patch:"patch",ini:"ini",
  markdown:"md",md:"md",text:"txt","":"txt"};

function _splitFences(text){
  const lines=String(text).split("\n"), segs=[]; let buf=[], i=0;
  const flush=()=>{ if(buf.length){ segs.push({type:"prose", body:buf.join("\n")}); buf=[]; } };
  const FENCE=/^(\s*)(`{3,}|~{3,})[ \t]*([\w+.#-]*)[ \t]*$/;
  while(i<lines.length){
    const o=FENCE.exec(lines[i]);
    if(o){
      const ch=o[2][0], cnt=o[2].length, lang=(o[3]||"").toLowerCase();
      flush();
      // Depth-aware close: a same-char fence of >= cnt WITH a language opens a
      // nested level (e.g. a ```python inside a ```markdown example); a BARE one
      // closes. The block ends when depth returns to 0. So a 3-backtick markdown
      // example keeps its nested 3-backtick code fence instead of closing early.
      const body=[]; let j=i+1, depth=1, closed=false;
      while(j<lines.length){
        const f=FENCE.exec(lines[j]);
        if(f && f[2][0]===ch && f[2].length>=cnt){
          if(f[3]) depth++;                          // ```lang → nested open
          else { depth--; if(depth===0){ closed=true; break; } }   // ``` → close
        }
        body.push(lines[j]); j++;
      }
      segs.push({type:"code", lang, body:body.join("\n")});
      i = closed ? j+1 : j;
    } else { buf.push(lines[i]); i++; }
  }
  flush();
  return segs;
}
function _filenameFor(lang, n){
  const ext=_EXT[(lang||"").toLowerCase()]||"txt";
  return "snippet"+(n>1?("-"+n):"")+"."+ext;
}
function renderAnswer(text){
  const wrap=document.createElement("div"); wrap.className="ans";
  let codeN=0;
  for(const s of _splitFences(text)){
    if(s.type==="code"){ codeN++; wrap.appendChild(_codeBox(s.body, s.lang, codeN)); }
    else if(s.body.trim()){ wrap.appendChild(_proseBox(s.body)); }
  }
  if(!wrap.children.length) wrap.textContent=text;      // e.g. whitespace-only
  return wrap;
}
function _btn(label, title, fn){
  const b=document.createElement("button"); b.type="button"; b.className="ab-btn";
  b.textContent=label; b.title=title; b.onclick=e=>{ e.stopPropagation(); fn(b); }; return b;
}
function _crudTools(src, name){
  return [
    _btn("copy","Copy to clipboard", ()=>_copyText(src)),
    _btn("download","Download (without saving)", ()=>_downloadText(src, name)),
    _btn("save","Save into the current workspace folder", ()=>_saveToFolder(src, name)),
  ];
}
function _rawPre(text){
  const pre=document.createElement("pre"); pre.className="ab-raw"; pre.hidden=true;
  const c=document.createElement("code"); c.textContent=text; pre.appendChild(c); return pre;
}
// view-source toggle: swap the rendered view for the raw source and back.
function _sourceToggle(rendered, raw){
  return _btn("source","Toggle rendered / raw source", (b)=>{
    const showRaw=raw.hidden;              // currently hidden → reveal it
    raw.hidden=!showRaw; rendered.hidden=showRaw;
    b.textContent=showRaw?"rendered":"source"; b.classList.toggle("on", showRaw);
  });
}
// language → a CodeMirror mode that's bundled locally (else null → no highlight).
/* Self-contained syntax highlighter — no external dependency, guaranteed to run.
   Tokenises comments, strings, numbers and keywords; unknown languages show
   plain (still monospaced). Good enough for a chat UI. */
const _KW = {
  python:"def class return if elif else for while in is not and or import from as with try except finally raise pass break continue lambda yield global nonlocal assert del async await match case with True False None self print",
  js:"function return if else for while do switch case break continue var let const class extends new this super typeof instanceof in of import export default from async await try catch finally throw delete void yield static get set null undefined true false",
  clike:"int long short char float double void bool auto const static struct class public private protected return if else for while do switch case break continue new delete typedef enum union namespace using template typename virtual override final sizeof true false null nullptr this",
  go:"func package import return if else for range switch case break continue var const type struct interface map chan go defer select fallthrough true false nil",
  rust:"fn let mut const struct enum impl trait pub use mod match if else for while loop return break continue as ref move dyn where async await self Self true false Some None Ok Err",
  sql:"select from where insert update delete into values set create table drop alter add primary key foreign references join left right inner outer full on group by order having limit offset distinct union as and or not null is like in between true false",
  bash:"if then elif else fi for while until do done case esac function return in export local readonly declare unset echo printf cd exit source",
  yaml:"true false null yes no on off",
};
const _LANGCFG = {
  python:{kw:"python",line:"#",str:['"""',"'''",'"',"'"]},
  bash:{kw:"bash",line:"#",str:['"',"'"]}, sh:{alias:"bash"}, shell:{alias:"bash"}, zsh:{alias:"bash"},
  js:{kw:"js",line:"//",block:["/*","*/"],str:["`",'"',"'"]},
  javascript:{alias:"js"}, ts:{alias:"js"}, typescript:{alias:"js"}, jsx:{alias:"js"}, tsx:{alias:"js"}, mjs:{alias:"js"},
  json:{kw:"json",str:['"']},
  c:{kw:"clike",line:"//",block:["/*","*/"],str:['"',"'"]}, cpp:{alias:"c"}, "c++":{alias:"c"}, cc:{alias:"c"},
  h:{alias:"c"}, hpp:{alias:"c"}, java:{alias:"c"}, cs:{alias:"c"}, kotlin:{alias:"c"}, kt:{alias:"c"}, scala:{alias:"c"},
  go:{kw:"go",line:"//",block:["/*","*/"],str:["`",'"']},
  rust:{kw:"rust",line:"//",block:["/*","*/"],str:['"']}, rs:{alias:"rust"},
  sql:{kw:"sql",line:"--",block:["/*","*/"],str:["'",'"'],ci:true},
  css:{kw:"css",block:["/*","*/"],str:['"',"'"]}, scss:{alias:"css"}, less:{alias:"css"},
  html:{html:true}, htm:{alias:"html"}, xml:{alias:"html"}, svg:{alias:"html"}, xhtml:{alias:"html"}, vue:{alias:"html"},
  markdown:{md:true}, md:{alias:"markdown"}, mdx:{alias:"markdown"},
  latex:{latex:true}, tex:{alias:"latex"}, sty:{alias:"latex"},
  yaml:{kw:"yaml",line:"#",str:['"',"'"]}, yml:{alias:"yaml"}, toml:{alias:"yaml"}, ini:{alias:"yaml"},
};
function _langCfg(lang){
  let c=_LANGCFG[(lang||"").toLowerCase()], g=0;
  while(c && c.alias && g++<4) c=_LANGCFG[c.alias];
  return c||null;
}
function _hlEsc(s){ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
const _TOKSTYLE={kw:"color:#c792ea",str:"color:#c3e88d",com:"color:#7b8aa0;font-style:italic",num:"color:#f78c6c"};
function _highlight(code, lang){
  const cfg=_langCfg(lang); if(!cfg) return null;      // unknown language → plain
  if(cfg.html) return _highlightHtml(code);            // tags/attrs need their own pass
  if(cfg.md) return _highlightMd(code);                // markdown source
  if(cfg.latex) return _highlightLatex(code);          // LaTeX source
  const kw=new Set((_KW[cfg.kw]||"").split(/\s+/).filter(Boolean).map(w=>cfg.ci?w.toLowerCase():w));
  let out="", i=0; const n=code.length;
  // inline styles (not CSS classes) so highlighting survives a stale app.css.
  const span=(cls,t)=>{ out+= cls?('<span style="'+_TOKSTYLE[cls]+'">'+_hlEsc(t)+'</span>'):_hlEsc(t); };
  while(i<n){
    const c=code[i];
    if(cfg.line && code.startsWith(cfg.line,i)){ let j=code.indexOf("\n",i); if(j<0)j=n; span("com",code.slice(i,j)); i=j; continue; }
    if(cfg.block && code.startsWith(cfg.block[0],i)){ let j=code.indexOf(cfg.block[1],i+cfg.block[0].length); j=j<0?n:j+cfg.block[1].length; span("com",code.slice(i,j)); i=j; continue; }
    let sm=false;
    for(const q of (cfg.str||[])){
      if(code.startsWith(q,i)){ let j=i+q.length;
        while(j<n){ if(code[j]==="\\"){ j+=2; continue; } if(code.startsWith(q,j)){ j+=q.length; break; } j++; }
        span("str", code.slice(i, Math.min(j,n))); i=Math.min(j,n); sm=true; break; }
    }
    if(sm) continue;
    if(/[0-9]/.test(c) && !/[\w.]/.test(code[i-1]||"")){
      const m=/^(0x[0-9a-fA-F]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?)/.exec(code.slice(i));
      if(m){ span("num", m[0]); i+=m[0].length; continue; } }
    if(/[A-Za-z_$]/.test(c)){ const w=/^[\w$]+/.exec(code.slice(i))[0];
      span(kw.has(cfg.ci?w.toLowerCase():w)?"kw":null, w); i+=w.length; continue; }
    span(null, c); i++;
  }
  return out;
}
function _highlightHtml(code){
  const S=_TOKSTYLE, TAG="color:#82aaff", ATTR="color:#c792ea";
  let out="", i=0; const n=code.length;
  const push=(st,t)=>{ out+= st?('<span style="'+st+'">'+_hlEsc(t)+'</span>'):_hlEsc(t); };
  while(i<n){
    if(code.startsWith("<!--",i)){ let j=code.indexOf("-->",i); j=j<0?n:j+3; push(S.com, code.slice(i,j)); i=j; continue; }
    if(code[i]==="<" && /[a-zA-Z!/?]/.test(code[i+1]||"")){
      push(null,"<"); i++;
      while(code[i]==="/"||code[i]==="!"||code[i]==="?"){ push(null,code[i]); i++; }
      const nm=/^[\w:.-]+/.exec(code.slice(i)); if(nm){ push(TAG, nm[0]); i+=nm[0].length; }
      while(i<n && code[i]!==">"){
        const c=code[i];
        if(c==='"'||c==="'"){ let k=i+1; while(k<n&&code[k]!==c)k++; k=Math.min(k+1,n); push(S.str, code.slice(i,k)); i=k; continue; }
        const at=/^[\w:.-]+/.exec(code.slice(i)); if(at){ push(ATTR, at[0]); i+=at[0].length; continue; }
        push(null, c); i++;
      }
      if(code[i]===">"){ push(null,">"); i++; }
      continue;
    }
    push(null, code[i]); i++;
  }
  return out;
}
function _highlightInto(codeEl, body, lang){
  const html=_highlight(body, lang);
  if(html!=null){ codeEl.innerHTML=html; codeEl.classList.add("hl"); }
  else codeEl.textContent=body;                       // plain, still monospaced
}
// Markdown SOURCE highlighting (headings, emphasis, code, links, structure).
function _mdInline(text){
  const S=_TOKSTYLE; let out="", i=0; const n=text.length; const raw=_hlEsc;
  while(i<n){
    if(text[i]==="`"){ const j=text.indexOf("`",i+1);
      if(j>=0){ out+='<span style="'+S.str+'">'+raw(text.slice(i,j+1))+'</span>'; i=j+1; continue; } }
    if(text.startsWith("**",i)||text.startsWith("__",i)){ const d=text.slice(i,i+2); const j=text.indexOf(d,i+2);
      if(j>=0){ out+='<span style="color:#ffcb6b">'+raw(text.slice(i,j+2))+'</span>'; i=j+2; continue; } }
    if(text[i]==="["){ const m=/^\[[^\]]+\]\([^)\s]+\)/.exec(text.slice(i));
      if(m){ out+='<span style="color:#c792ea">'+raw(m[0])+'</span>'; i+=m[0].length; continue; } }
    out+=raw(text[i]); i++;
  }
  return out;
}
function _highlightMd(code){
  const S=_TOKSTYLE;
  return String(code).split("\n").map(line=>{
    if(/^\s*(```|~~~)/.test(line)) return '<span style="'+S.com+'">'+_hlEsc(line)+'</span>';
    const h=/^(\s*#{1,6}\s+)(.*)$/.exec(line);
    if(h) return '<span style="color:#82aaff">'+_hlEsc(h[1])+'</span>'+_mdInline(h[2]);
    if(/^\s*>/.test(line)) return '<span style="'+S.com+'">'+_hlEsc(line)+'</span>';
    const li=/^(\s*(?:[-*+]|\d+\.)\s+)([\s\S]*)$/.exec(line);
    if(li) return '<span style="color:#f78c6c">'+_hlEsc(li[1])+'</span>'+_mdInline(li[2]);
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) return '<span style="'+S.com+'">'+_hlEsc(line)+'</span>';
    return _mdInline(line);
  }).join("\n");
}
// LaTeX SOURCE highlighting (commands, math delimiters, comments, braces).
function _highlightLatex(code){
  const S=_TOKSTYLE; let out="", i=0; const n=code.length;
  const push=(st,t)=>{ out+= st?('<span style="'+st+'">'+_hlEsc(t)+'</span>'):_hlEsc(t); };
  while(i<n){
    const c=code[i];
    if(c==="%"){ let j=code.indexOf("\n",i); if(j<0)j=n; push(S.com, code.slice(i,j)); i=j; continue; }
    if(c==="\\"){ const m=/^\\([a-zA-Z]+\*?|[^a-zA-Z\s]|\s)/.exec(code.slice(i));
      if(m){ push("color:#82aaff", m[0]); i+=m[0].length; continue; } }
    if(c==="$"){ push("color:#c792ea", "$"); i++; continue; }
    if(c==="{"||c==="}"||c==="["||c==="]"){ push("color:#f78c6c", c); i++; continue; }
    push(null, c); i++;
  }
  return out;
}
const _MD_LANGS=new Set(["markdown","md","mdx"]);
const _TEX_LANGS=new Set(["latex","tex"]);
function _codeBox(body, lang, n){
  const box=document.createElement("div"); box.className="ablock code";
  let rendered;
  if(_MD_LANGS.has(lang)){                 // render markdown as formatted output
    rendered=document.createElement("div"); rendered.className="ab-body md ab-rendered";
    try{ rendered.innerHTML=renderMarkdown(body); }catch(e){ rendered.textContent=body; }
  } else if(_TEX_LANGS.has(lang)){         // typeset the math (falls back to source)
    rendered=_renderLatex(body);
  } else {                                 // code: highlighted source
    rendered=document.createElement("pre"); rendered.className="ab-hl";
    const codeEl=document.createElement("code"); rendered.appendChild(codeEl);
    _highlightInto(codeEl, body, lang);
  }
  const raw=_rawPre(body);
  const head=document.createElement("div"); head.className="ab-head";
  const lbl=document.createElement("span"); lbl.className="ab-lang"; lbl.textContent=lang||"code";
  const tools=document.createElement("div"); tools.className="ab-tools";
  tools.append(_sourceToggle(rendered, raw), ..._crudTools(body, _filenameFor(lang, n)));
  head.append(lbl, tools);
  box.append(head, rendered, raw);
  return box;
}
// Render a LaTeX block: typeset each math segment via KaTeX (if present),
// leaving comments/text as-is. No KaTeX → highlighted source (graceful).
function _renderLatex(body){
  const k=window.katex;
  if(!k || typeof k.renderToString!=="function"){
    const pre=document.createElement("pre"); pre.className="ab-hl";
    const c=document.createElement("code"); c.innerHTML=_highlightLatex(body); c.classList.add("hl");
    pre.appendChild(c); return pre;
  }
  const div=document.createElement("div"); div.className="ab-body ab-math ab-rendered";
  div.innerHTML=_texToHtml(body, k);
  return div;
}
function _texToHtml(src, k){
  let out="", i=0; const n=src.length;
  const render=(expr, disp)=>{ try{ return k.renderToString(expr.trim(), {displayMode:disp, throwOnError:false}); }
                               catch(e){ return _hlEsc(expr); } };
  while(i<n){
    if(src[i]==="%"){ let j=src.indexOf("\n",i); if(j<0)j=n;
      out+='<span style="color:#7b8aa0;font-style:italic">'+_hlEsc(src.slice(i,j))+'</span>'; i=j; continue; }
    if(src.startsWith("\\[",i)){ const j=src.indexOf("\\]",i+2); if(j>=0){ out+=render(src.slice(i+2,j),true); i=j+2; continue; } }
    if(src.startsWith("$$",i)){ const j=src.indexOf("$$",i+2); if(j>=0){ out+=render(src.slice(i+2,j),true); i=j+2; continue; } }
    if(src.startsWith("\\(",i)){ const j=src.indexOf("\\)",i+2); if(j>=0){ out+=render(src.slice(i+2,j),false); i=j+2; continue; } }
    const be=/^\\begin\{(\w+\*?)\}/.exec(src.slice(i));
    if(be){ const end="\\end{"+be[1]+"}"; const j=src.indexOf(end,i);
      if(j>=0){ out+=render(src.slice(i,j+end.length),true); i=j+end.length; continue; } }
    if(src[i]==="$"){ const j=src.indexOf("$",i+1); if(j>=0){ out+=render(src.slice(i+1,j),false); i=j+1; continue; } }
    if(src[i]==="\n"){ out+="<br>"; i++; continue; }
    out+=_hlEsc(src[i]); i++;
  }
  return out;
}
function _proseBox(md){
  const box=document.createElement("div"); box.className="ablock prose";
  const rendered=document.createElement("div"); rendered.className="ab-body md";
  try{ rendered.innerHTML=renderMarkdown(md); }
  catch(e){ rendered.className="ab-body"; const p=document.createElement("pre");
    p.style.whiteSpace="pre-wrap"; p.textContent=md; rendered.appendChild(p); }
  const raw=_rawPre(md);
  const head=document.createElement("div"); head.className="ab-head";
  const tools=document.createElement("div"); tools.className="ab-tools";
  tools.append(_sourceToggle(rendered, raw), ..._crudTools(md, "answer.md"));
  head.appendChild(tools);
  box.append(head, rendered, raw);
  return box;
}
async function _copyText(s){
  try{ await navigator.clipboard.writeText(s); toast("copied"); }
  catch(_){ const ta=document.createElement("textarea"); ta.value=s; ta.style.position="fixed";
    ta.style.opacity="0"; document.body.appendChild(ta); ta.select();
    try{ document.execCommand("copy"); toast("copied"); }catch(e){ toast("copy failed"); }
    ta.remove(); }
}
function _downloadText(s, name){
  const blob=new Blob([s],{type:"text/plain;charset=utf-8"}), url=URL.createObjectURL(blob);
  const a=document.createElement("a"); a.href=url; a.download=name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 1000);
}
async function _saveToFolder(s, defaultName){
  const name=prompt("Save into the current workspace as:", defaultName); if(!name) return;
  try{
    const r=await fetch(fsBase()+"/file?path="+encodeURIComponent(name),
      {method:"PUT", headers:{"content-type":"text/plain"}, body:s});
    if(!r.ok){ const d=await r.json().catch(()=>({})); alert("Save failed: "+(d.detail||("HTTP "+r.status))); return; }
    toast("saved "+name); refreshFiles(); if(activeProject) refreshProjects();
  }catch(e){ alert("Save failed: "+e.message); }
}
let _toastTimer=null;
function toast(msg){
  let t=document.getElementById("toast");
  if(!t){ t=document.createElement("div"); t.id="toast"; t.className="toast"; document.body.appendChild(t); }
  t.textContent=msg; t.classList.add("show");
  clearTimeout(_toastTimer); _toastTimer=setTimeout(()=>t.classList.remove("show"), 1600);
}

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
  ["share","auto","think","sTemp","sTopP","sTopK","sRepeat","sSeed",
   "bMaxIter","bWall","bCost","bTok","bSubIter",
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
