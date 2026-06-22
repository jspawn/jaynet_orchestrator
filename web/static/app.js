const $ = s => document.querySelector(s);
const log=$("#log"), chatList=$("#chatList");
let es=null, currentRun=null, pending=null, cur=null;
let chat = { id:null, title:null, saved:false, turns:[] };

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
$("#allOn").onclick=()=>{ TOOLS.disabled.clear(); renderTools(); saveTools(); };
$("#allOff").onclick=()=>{ TOOLS.disabled=new Set(TOOLS.list.map(t=>t.name)); renderTools(); saveTools(); };
$("#logout").onclick=async()=>{ try{ await fetch("/api/logout",{method:"POST"}); }catch(e){} location.href="/login"; };

/* ---------- side panels: collapse on desktop, drawers on mobile ---------- */
function isNarrow(){ return innerWidth<=900; }
function closeDrawers(){ document.body.classList.remove("show-chats","show-tools"); }
$("#chatsToggle").addEventListener("click", ()=>{
  if(isNarrow()){ document.body.classList.remove("show-tools"); document.body.classList.toggle("show-chats"); }
  else { document.body.classList.toggle("collapse-chats"); }
});
$("#toolsToggle").addEventListener("click", ()=>{
  if(isNarrow()){ document.body.classList.remove("show-chats"); document.body.classList.toggle("show-tools"); }
  else { document.body.classList.toggle("collapse-tools"); }
});
$("#drawerScrim").addEventListener("click", closeDrawers);
addEventListener("keydown", e=>{ if(e.key==="Escape") closeDrawers(); });
chatList.addEventListener("click", ()=>{ if(isNarrow()) closeDrawers(); });
$("#newChat").addEventListener("click", ()=>{ if(isNarrow()) closeDrawers(); });
$("#newChatTop").addEventListener("click", ()=>$("#newChat").click());
/* both side panels start collapsed (desktop); on mobile they're closed drawers anyway */
document.body.classList.add("collapse-chats","collapse-tools");

/* 2FA enrollment/disable now lives on the dedicated account page (/account). */

/* ---------- basic rendering ---------- */
function addMsg(text, cls, atts){
  const d=document.createElement("div"); d.className="msg "+cls; d.textContent=text;
  if(atts && atts.length) d.appendChild(renderAtts(atts));
  log.appendChild(d); log.scrollTop=log.scrollHeight; return d;
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
  $("#cancel").disabled=!live; $("#send").disabled=!!live; }
function esc(o){ return JSON.stringify(o,null,2); }
function fmtUsd(x){ return "$"+Number(x||0).toFixed(4); }

/* ---------- a response block (answer + expandable activity + footer) ---------- */
function startResponse(){
  const root=document.createElement("div"); root.className="resp";
  const answer=document.createElement("div"); answer.className="answer";
  const activity=document.createElement("details"); activity.className="activity"; activity.hidden=true;
  activity.innerHTML="<summary><span class='sum'></span></summary><div class='steps'></div>";
  const foot=document.createElement("div"); foot.className="foot"; foot.textContent="running…";
  root.append(answer, activity, foot);
  log.appendChild(root); log.scrollTop=log.scrollHeight;
  return { root, answer, activity, sum:activity.querySelector(".sum"), steps:activity.querySelector(".steps"),
           foot, toolCount:0, stepCount:0, turns:0, hadReasoning:false, model:null, llmLive:null,
           pending:[], ticker:null };
}
function showActivity(c){ c.activity.hidden=false; }
function addStep(c, html){
  const d=document.createElement("div"); d.className="st"; d.innerHTML=html;
  c.steps.appendChild(d); c.stepCount++; showActivity(c);
  if(c.activity.open) log.scrollTop=log.scrollHeight;
  return d;
}
function addReason(c, text){
  const d=addStep(c, "<span class='k reason'>thinking</span><pre></pre>");
  d.querySelector("pre").textContent=text; c.hadReasoning=true;
}
function fmtDur(ms){
  ms=Math.max(0, ms||0);
  return ms<1000 ? Math.round(ms)+" ms" : (ms/1000).toFixed(ms<10000?1:0)+"s";
}
function ensureTicker(c){
  if(c.ticker || !c.pending.length) return;
  c.ticker=setInterval(()=>{
    const now=Date.now();
    for(const p of c.pending){ if(p.timerEl) p.timerEl.textContent=fmtDur(now-p.start); }
  }, 100);
}
function stopTicker(c){ if(c.ticker){ clearInterval(c.ticker); c.ticker=null; } }
function takePending(c, name){
  if(!c.pending.length) return null;
  let i=c.pending.findIndex(p=>p.name===name); if(i<0) i=0;   // FIFO, prefer same name
  return c.pending.splice(i,1)[0];
}
/* Each tool call gets its own row: a spinner + a live seconds counter while it
   runs, finalized to ✓/✗ and the real latency when its result arrives. */
function addCalls(c, calls){
  for(const t of calls){
    const el=addStep(c, "<span class='k call run'><span class='spin'></span>"+t.name+
                        "</span><span class='timer live'>0 ms</span>");
    c.pending.push({ el, name:t.name, start:Date.now(), timerEl:el.querySelector(".timer") });
  }
  ensureTicker(c);
}
function addToolResult(c, d){
  const ok=d.status!=="error";
  const head="<span class='k "+(ok?"ok":"err")+"'>"+(ok?"✓ ":"✗ ")+d.tool+"</span>"+
             "<span class='meta'>"+(d.latency_ms!=null?fmtDur(d.latency_ms):"")+"</span>"+
             (d.private?"<span class='priv'>private</span>":"");
  const body=ok ? (d.result_preview||"") : ("ERROR: "+(d.error||""));
  const args=d.args?("\nargs: "+esc(d.args)):"";
  const p=takePending(c, d.tool);                 // finalize the running row in place
  let el;
  if(p){ el=p.el; el.innerHTML=head+((body||args)?"<pre></pre>":""); }
  else  { el=addStep(c, head+((body||args)?"<pre></pre>":"")); }   // replay/fallback
  const pre=el.querySelector("pre"); if(pre) pre.textContent=body+args;
  c.toolCount++;
  if(!c.pending.length) stopTicker(c);
}
function llmAppend(c, model, text){
  if(!c.llmLive){
    c.llmLive=addStep(c, "<span class='k call'>delegated → "+(model||"")+"</span><pre></pre>");
  }
  c.llmLive.querySelector("pre").textContent+=text;
  if(c.activity.open) log.scrollTop=log.scrollHeight;
}
function reasonAppend(c, text){
  if(text==null) return;
  if(!c.reasonLive){
    if(!text.trim()) return;            // ignore an empty <think></think>
    c.reasonLive=addStep(c, "<span class='k reason'>thinking</span><pre></pre>");
    c.hadReasoning=true;
  }
  c.reasonLive.querySelector("pre").textContent+=text;
  if(c.activity.open) log.scrollTop=log.scrollHeight;
}
function footLive(c, costData){
  if(costData && costData.total_usd!=null)
    c.foot.textContent="running… "+fmtUsd(costData.total_usd)+" · "+(costData.total_tokens||0)+" tok";
}
function finalize(c, d){
  // stop any live tool timers; neutralize rows that never got a result
  stopTicker(c);
  for(const p of c.pending){
    const t=p.el.querySelector(".timer"); if(t) t.classList.remove("live");
    const s=p.el.querySelector(".spin"); if(s) s.remove();
  }
  c.pending=[];
  if(d.answer!=null) c.answer.textContent=d.answer || "(no answer)";
  else if(!c.answer.textContent) c.answer.textContent="(no answer)";
  c.llmLive=null;
  // expandable one-liner summary — only if there was activity worth showing
  if(c.stepCount>0){
    const parts=[];
    if(c.hadReasoning) parts.push("thinking");
    if(c.toolCount) parts.push(c.toolCount+" tool"+(c.toolCount>1?"s":""));
    parts.push(c.stepCount+" step"+(c.stepCount>1?"s":""));
    c.sum.textContent=parts.join(" · ");
    c.activity.hidden=false; c.activity.open=false;
  } else { c.activity.hidden=true; }
  // footer line
  const b=d.budget||{};
  const tok=(b.tokens&&b.tokens.total!=null)?b.tokens.total:0;
  const parts=[ c.model||"local",
                (b.iterations||c.turns||1)+" turn"+(((b.iterations||c.turns)>1)?"s":""),
                c.toolCount+" tool"+(c.toolCount===1?"":"s"),
                tok+" tok", fmtUsd(b.cost_usd),
                (b.elapsed_s!=null?Number(b.elapsed_s).toFixed(1)+"s":"") ];
  let line=parts.filter(Boolean).join(" · ");
  if(d.status && d.status!=="ok") line="<span class='badge'>"+d.status+"</span> · "+line;
  c.foot.innerHTML=line;
}

/* apply one event to a response (used live AND when replaying a saved chat) */
function applyEvent(c, ev){
  const d=ev.data||{};
  switch(ev.type){
    case "model_turn":
      if(d.model) c.model=d.model;
      c.turns++;
      if(d.tool_calls && d.tool_calls.length){
        const txt=c.answer.textContent;
        if(txt && txt.trim()){ addReason(c, txt); }   // fallback: stray brain text was planning
        c.answer.textContent="";
        addCalls(c, d.tool_calls);
      } else if(!c.answer.textContent && d.content){
        c.answer.textContent=d.content;               // non-streaming fallback
      }
      c.reasonLive=null; c.llmLive=null;              // next turn's streams are fresh steps
      break;
    case "tool_result": addToolResult(c, d); break;
    case "confirmation":
      addStep(c, "<span class='k warn'>confirmation</span> "+(d.approved?"approved":"denied")); break;
    case "token":
      if(d.scope==="reasoning") reasonAppend(c, d.text);
      else if(d.scope==="llm.call") llmAppend(c, d.model, d.text);
      else { c.answer.textContent+=d.text; if(es) log.scrollTop=log.scrollHeight; }
      break;
    case "cost": footLive(c, d); break;
    case "output": {
      if(!c.dlbox){ c.dlbox=document.createElement("div"); c.dlbox.className="downloads"; c.root.appendChild(c.dlbox); }
      let chip=c.dlbox.querySelector("a.dl");
      if(!chip){ chip=document.createElement("a"); chip.className="dl"; chip.setAttribute("download",""); c.dlbox.appendChild(chip); }
      chip.href="/api/output/"+(ev.run_id||currentRun);
      chip.title=(d.kind==="targz")?"bundled archive":"download";
      chip.textContent="⬇ "+(d.name||"download")+" ("+fmtSize(d.size||0)+")";
      break;
    }
    case "budget_warning":
      addStep(c, "<span class='k warn'>budget</span> nearing the "+(d.dimension||"")+
                 " limit ("+Math.round((d.pressure||0)*100)+"%) — saving progress & wrapping up");
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
function updateSaveBtn(){ const b=$("#saveBtn"); b.classList.toggle("on",chat.saved); b.textContent=chat.saved?"★":"☆"; b.title=chat.saved?"Saved — click to unsave":"Save this chat"; }
async function saveChat(){
  if(!chat.turns.length) return;
  const res=await (await fetch("/api/chats",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({id:chat.id, title:chat.title, turns:chat.turns, project_id:(activeProject?activeProject.id:null)})})).json();
  chat.id=res.id; chat.title=res.title; chat.saved=true; updateSaveBtn(); refreshChats();
}
async function syncIfSaved(){ if(chat.saved && chat.id) await saveChat(); }
function askDelete(id, title){
  showModal("Remove “"+(title||"this chat")+"” from saved? This permanently deletes it.", async ()=>{
    await fetch("/api/chats/"+id,{method:"DELETE"});
    if(id===chat.id){ chat.saved=false; chat.id=null; updateSaveBtn(); }
    refreshChats();
  });
}
$("#saveBtn").onclick=()=>{ if(!chat.saved) saveChat(); else askDelete(chat.id, chat.title); };

async function loadChat(id){
  const c=await (await fetch("/api/chats/"+id)).json();
  chat={ id:c.id, title:c.title, saved:true, turns:c.turns.map(t=>({
    user_message:t.user_message, answer:t.answer, run_id:t.run_id, status:t.status, trajectory:t.trajectory||"", events:t.events||[] })) };
  log.innerHTML=""; cur=null; pending=null; currentRun=null;
  chat.turns.forEach((t,i)=>{
    if(i>0) sep("— turn "+(i+1)+" —");
    addMsg(t.user_message,"user");
    const c2=startResponse();
    let fin=null;
    for(const ev of (t.events||[])){ if(ev.type==="run_finish") fin=ev.data; else applyEvent(c2, ev); }
    finalize(c2, fin || {answer:t.answer, status:t.status, budget:{}});
  });
  setStatus("loaded saved chat", false);
  projSelect.value = c.project_id || "";
  syncActive();
  updateSaveBtn(); refreshChats();
}
$("#newChat").onclick=()=>{
  chat={ id:null, title:null, saved:false, turns:[] };
  pending=null; currentRun=null; cur=null; log.innerHTML="";
  setStatus("idle", false); updateSaveBtn(); refreshChats();
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
  projSelect.innerHTML="<option value=''>— no project —</option>";
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
  projPanel.hidden = !activeProject;
  if(activeProject) loadTree(); else fileTree.innerHTML="";
}
projSelect.onchange=syncActive;
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
async function loadTree(){
  if(!activeProject){ fileTree.innerHTML=""; return; }
  const r=await fetch("/api/projects/"+activeProject.id+"/files");
  if(!r.ok){ fileTree.innerHTML="<div class='empty'>—</div>"; return; }
  const {entries}=await r.json();
  fileTree.innerHTML = entries.length ? "" : "<div class='empty'>empty — ＋ or ⬆ to add files</div>";
  for(const e of entries){
    const depth=e.path.split("/").length-1;
    const row=document.createElement("div");
    row.className="ftrow "+(e.type==="dir"?"ftdir":"ftfile");
    row.style.paddingLeft=(6+depth*12)+"px";
    const name=document.createElement("span"); name.className="ftname";
    name.textContent=(e.type==="dir"?"📁 ":"📄 ")+e.path.split("/").pop();
    if(e.type==="file"){ name.title=e.path+" · "+fmtSize(e.size); name.onclick=()=>openFile(e.path); }
    row.appendChild(name);
    const del=document.createElement("button"); del.className="ftdel"; del.title="delete"; del.textContent="×";
    del.onclick=(ev)=>{ ev.stopPropagation(); deleteProjectPath(e.path, e.type); };
    row.appendChild(del);
    fileTree.appendChild(row);
  }
}
async function deleteProjectPath(path, type){
  if(!activeProject) return;
  const what = type==="dir" ? ("folder “"+path+"” and everything in it") : ("“"+path+"”");
  showModal("Delete "+what+" from project “"+activeProject.name+"”? This cannot be undone.", async()=>{
    const r=await fetch("/api/projects/"+activeProject.id+"/file?path="+encodeURIComponent(path),{method:"DELETE"});
    if(r.ok){
      if(editorFile===path || (type==="dir" && editorFile && editorFile.startsWith(path+"/"))){
        editorFile=null; const m=$("#editorModal"); if(m) m.hidden=true;
      }
      loadTree(); refreshProjects();   // refresh tree + project file counts
    }
  });
}
async function openFile(path){
  const r=await fetch("/api/projects/"+activeProject.id+"/file?path="+encodeURIComponent(path));
  if(!r.ok) return;
  const f=await r.json();
  if(f.binary){ alert("“"+path+"” is a binary file and can't be edited here."); return; }
  editorFile=path;
  $("#editorPath").textContent=activeProject.name+" / "+path+(f.truncated?"  (truncated view — saving would clip)":"");
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
  const r=await fetch("/api/projects/"+activeProject.id+"/file?path="+encodeURIComponent(editorFile),
    {method:"PUT",headers:{"content-type":"text/plain"},body:content});
  $("#editorMsg").textContent = r.ok ? "saved ✓" : "save failed";
  if(r.ok) loadTree();
};
$("#editorClose").onclick=()=>{ $("#editorModal").hidden=true; editorFile=null; };
$("#projNewFile").onclick=async()=>{
  if(!activeProject) return;
  const path=prompt("New file path (e.g. src/main.py):"); if(!path) return;
  await fetch("/api/projects/"+activeProject.id+"/file?path="+encodeURIComponent(path),
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
  const fin=ok=>{ c.querySelector(".row").innerHTML="<span style='color:var(--muted)'>"+(ok?"approved":"denied")+"</span>"; };
  c.querySelector(".approve").onclick=async()=>{ await approve(d.confirmation_id,true); fin(true); };
  c.querySelector(".deny").onclick=async()=>{ await approve(d.confirmation_id,false); fin(false); };
  log.appendChild(c); log.scrollTop=log.scrollHeight;
}
async function approve(cid, ok){
  await fetch("/api/approve/"+currentRun,{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({confirmation_id:cid, approved:ok})});
}

/* ---------- run / stream ---------- */
function openStream(runId){
  es=new EventSource("/api/stream/"+runId);
  const onEv = h => e => { try{ h(JSON.parse(e.data)); }catch(_){} };
  const handle = ev => { if(pending) pending.events.push(ev); applyEvent(cur, ev); };
  ["run_start","tool_selection","model_turn","tool_result","confirmation","token","cost","output","budget_warning"]
    .forEach(t=>es.addEventListener(t, onEv(handle)));
  es.addEventListener("confirmation_request", onEv(ev=>renderConfirm(ev.data)));
  es.addEventListener("run_finish", onEv(ev=>{
    if(pending) pending.events.push(ev);
    finalize(cur, ev.data);
    if(pending){ pending.answer=ev.data.answer||""; pending.status=ev.data.status; pending.run_id=ev.run_id;
      pending.trajectory=ev.data.trajectory||"";
      chat.turns.push(pending); pending=null; syncIfSaved(); }
    setStatus("done · "+ev.data.status, false);
    es.close(); es=null; currentRun=null; cur=null;
  }));
  es.onerror=()=>{};
}
/* ---------- chat attachments ---------- */
let pendingAttachments=[];
$("#attachBtn").addEventListener("click", ()=>$("#fileInput").click());
$("#fileInput").addEventListener("change", async ()=>{
  const files=Array.from($("#fileInput").files||[]);
  for(const f of files){
    try{
      const r=await fetch("/api/upload?filename="+encodeURIComponent(f.name),{method:"POST",body:f});
      if(!r.ok){ const e=await r.json().catch(()=>({})); alert("Upload failed for "+f.name+": "+(e.detail||r.status)); continue; }
      pendingAttachments.push(await r.json());
    }catch(err){ alert("Upload error for "+f.name+": "+err); }
  }
  $("#fileInput").value=""; renderChips();
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
  const msg=$("#input").value.trim();
  if(!msg && !pendingAttachments.length) return;
  $("#input").value="";
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
    body:JSON.stringify({message:msg, history, tools:enabledTools(), share_private:$("#share").checked, auto_confirm:$("#auto").checked, think:$("#think").checked, budget_overrides:budgetOverrides(), attachments:atts.map(a=>a.id), project_id:(activeProject?activeProject.id:null)})});
  currentRun=(await r.json()).run_id;
  openStream(currentRun);
});
$("#input").addEventListener("keydown", e=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); $("#form").requestSubmit(); } });
$("#cancel").addEventListener("click", async()=>{ if(currentRun){ await fetch("/api/cancel/"+currentRun,{method:"POST"}); setStatus("cancelling…", true); } });

updateSaveBtn(); refreshChats(); refreshProjects(); loadMe(); loadTools();

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
    const hot  = (cs.getPropertyValue("--net-hot") ||"#62b0ff").trim();
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
