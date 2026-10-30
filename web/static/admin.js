const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path, opts){
  opts=opts||{};
  // JSON string bodies need the header — without it FastAPI 422s and the UI
  // silently snaps back (connector mode toggle "can't change", 2026-10-14).
  // FormData must stay multipart (browser sets the boundary itself).
  if(typeof opts.body==="string"){
    opts.headers=Object.assign({"content-type":"application/json"},opts.headers||{});
  }
  const r=await fetch(path, opts);
  if(r.status===401){ location.href="/login"; throw new Error("unauth"); }
  if(r.status===403){ location.href="/"; throw new Error("forbidden"); }
  return r;
}
function note(el, msg, ok){ el.textContent=msg; el.className="note "+(ok?"ok":"err");
  setTimeout(()=>{el.textContent="";},2500); }

// prompt — rendered markdown by default, "edit source" toggles the textarea
// (same mini-renderer as the chat composer in app.js; escapes before injecting)
function renderMarkdown(src){
  const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
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
    if(ln.trim()===""){ i++; continue; }
    const para=[]; while(i<lines.length && lines[i].trim()!=="" && !isBlock(lines[i])){ para.push(inline(lines[i])); i++; }
    out.push("<p>"+para.join("<br>")+"</p>");
  }
  return out.join("\n").replace(/\u0001(\d+)\u0001/g,(m,n)=>"<pre><code>"+blocks[+n]+"</code></pre>");
}
function promptShowRendered(){
  $("#promptView").innerHTML=renderMarkdown($("#prompt").value);
  $("#promptView").hidden=false; $("#prompt").hidden=true;
  $("#savePrompt").hidden=true; $("#reloadPrompt").hidden=true;
  $("#promptToggle").textContent="edit source";
}
function promptShowSource(){
  $("#promptView").hidden=true; $("#prompt").hidden=false;
  $("#savePrompt").hidden=false; $("#reloadPrompt").hidden=false;
  $("#promptToggle").textContent="view rendered";
  $("#prompt").focus();
}
$("#promptToggle").onclick=()=>{ $("#prompt").hidden?promptShowSource():promptShowRendered(); };
async function loadPrompt(){
  const d=await (await api("/api/admin/prompt")).json();
  $("#prompt").value=d.content;
  const custom=d.layer==="custom";
  $("#promptPath").textContent=(custom?"live overlay: "+d.overlay_path+" (overrides shipped)":"shipped: "+d.path);
  $("#promptStale").hidden=!d.stale;
  $("#revertPrompt").hidden=!custom;
  $("#applyConsolidate").hidden=true;
  const n=d.tweak_bullets||0;
  $("#consolidatePrompt").hidden=!n;
  if(n) $("#consolidatePrompt").textContent=`Consolidate ${n} eval tweak${n>1?"s":""}`;
  promptShowRendered();
}
$("#savePrompt").onclick=async()=>{
  const r=await api("/api/admin/prompt",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({content:$("#prompt").value})});
  note($("#promptNote"), r.ok?"saved (live overlay)":"error", r.ok);
  if(r.ok) loadPrompt();
};
$("#reloadPrompt").onclick=loadPrompt;
$("#revertPrompt").onclick=async()=>{
  if(!await dlgConfirm("Delete the live overlay and go back to the shipped prompt?", {yes:"revert"})) return;
  const r=await api("/api/admin/prompt",{method:"DELETE"});
  note($("#promptNote"), r.ok?"reverted to shipped":"error", r.ok);
  if(r.ok) loadPrompt();
};
$("#consolidatePrompt").onclick=async()=>{
  note($("#promptNote"),"drafting consolidation…",true,true);
  const r=await api("/api/admin/prompt/consolidate",{method:"POST"});
  if(!r.ok){ note($("#promptNote"),"consolidation failed: "+(await r.json()).detail,false); return; }
  const d=await r.json();
  promptShowSource();
  $("#prompt").value=d.draft;
  $("#applyConsolidate").hidden=false;
  $("#savePrompt").hidden=true;
  note($("#promptNote"),`draft by ${d.model} (${d.bullets} tweaks folded) — review, then "Apply consolidation" (writes a backup) or Refresh to discard`,true,true);
};
$("#applyConsolidate").onclick=async()=>{
  if(!await dlgConfirm("Save the consolidation draft as the live prompt? The current prompt is backed up first.", {yes:"apply"})) return;
  const r=await api("/api/admin/prompt/consolidate/apply",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({content:$("#prompt").value})});
  const d=r.ok?await r.json():null;
  note($("#promptNote"), d?("consolidated — backup: "+d.backup):"error", r.ok);
  if(r.ok) loadPrompt();
};

// worker prompts (specialist children — agent.worker_prompt)
let wpEnabled=false;
function wpShowRendered(){
  $("#wpView").innerHTML=renderMarkdown($("#wpEdit").value||"*(empty — saving creates the overlay)*");
  $("#wpView").hidden=false; $("#wpEdit").hidden=true;
  $("#wpSave").hidden=true; $("#wpReload").hidden=true;
  $("#wpToggle").textContent="edit source";
}
function wpShowSource(){
  $("#wpView").hidden=true; $("#wpEdit").hidden=false;
  const pinned=$("#wpEdit").dataset.pinned==="1";
  $("#wpSave").hidden=pinned; $("#wpReload").hidden=false;
  $("#wpToggle").textContent="view rendered";
  if(!pinned) $("#wpEdit").focus();
}
$("#wpToggle").onclick=()=>{ $("#wpEdit").hidden?wpShowSource():wpShowRendered(); };
async function loadWorkerPrompts(sel){
  const d=await (await api("/api/admin/worker-prompts")).json();
  wpEnabled=!!d.enabled;
  $("#wpBlurb").innerHTML=wpEnabled
    ? "<b>agent.worker_prompt is ON</b> — specialist children run on these prompts instead of the full gate prompt."
    : "<b>agent.worker_prompt is OFF</b> (Harness → Runtime) — children currently get the full gate prompt; these files are staged for when you enable it.";
  const cur=sel||$("#wpPart").value||"base";
  $("#wpPart").innerHTML=(d.parts||[]).map(p=>{
    const tag=p.layer==="custom"?" · overlay":p.layer==="pin"?" · pinned":p.layer==="none"?" · empty":"";
    const stale=p.stale?" ⚠":"";
    const label=p.name==="base"?`base (always prepended)${tag}${stale}`:`${p.name} module${tag}${stale}`;
    return `<option value="${esc(p.name)}"${p.name===cur?" selected":""}>${esc(label)}</option>`;
  }).join("");
  await loadWorkerPart($("#wpPart").value||"base");
}
async function loadWorkerPart(name){
  if(!name) return;
  const d=await (await api("/api/admin/worker-prompts/"+encodeURIComponent(name))).json();
  $("#wpEdit").value=d.content||"";
  $("#wpEdit").dataset.pinned=d.editable?"0":"1";
  $("#wpPath").textContent=d.layer==="custom"?("live overlay: "+d.overlay_path+" (overrides shipped)")
    :d.layer==="pin"?("pinned by agent.worker_prompts: "+d.pin_path+" — read-only here")
    :d.layer==="shipped"?("shipped: "+d.shipped_path)
    :("no file yet — saving creates the overlay "+d.overlay_path);
  $("#wpStale").hidden=!d.stale;
  $("#wpRevert").hidden=d.layer!=="custom";
  wpShowRendered();
}
$("#wpPart").onchange=()=>loadWorkerPart($("#wpPart").value);
$("#wpSave").onclick=async()=>{
  const name=$("#wpPart").value;
  const r=await api("/api/admin/worker-prompts/"+encodeURIComponent(name),{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({content:$("#wpEdit").value})});
  note($("#wpNote"), r.ok?"saved (live overlay)":"error", r.ok);
  if(r.ok) loadWorkerPrompts(name);
};
$("#wpReload").onclick=()=>loadWorkerPart($("#wpPart").value);
$("#wpRevert").onclick=async()=>{
  const name=$("#wpPart").value;
  if(!await dlgConfirm(`Delete the live overlay for "${name}" and go back to the shipped file?`, {yes:"revert"})) return;
  const r=await api("/api/admin/worker-prompts/"+encodeURIComponent(name),{method:"DELETE"});
  note($("#wpNote"), r.ok?"reverted to shipped":"error", r.ok);
  if(r.ok) loadWorkerPrompts(name);
};
$("#wpNew").onclick=async()=>{
  const t=await dlgPrompt("Strength tag for the new module — lowercase a-z, 0-9, '-' (e.g. 'vision'). The module is picked when a delegation routes on that tag.", {placeholder:"vision"});
  if(!t) return;
  const name=t.trim().toLowerCase();
  if(!/^[a-z0-9][a-z0-9-]{0,31}$/.test(name)||name==="base"){
    note($("#wpNote"),"invalid tag — lowercase letters/digits/'-', max 32 chars",false); return;
  }
  if(![...$("#wpPart").options].some(o=>o.value===name))
    $("#wpPart").insertAdjacentHTML("beforeend",`<option value="${esc(name)}">${esc(name)} module · empty</option>`);
  $("#wpPart").value=name;
  loadWorkerPart(name);
  wpShowSource();
};

// status
function bytes(n){ if(n<1024)return n+" B"; if(n<1048576)return (n/1024).toFixed(1)+" KB";
  return (n/1048576).toFixed(1)+" MB"; }
function dur(s){ if(s<60)return s+"s"; if(s<3600)return Math.floor(s/60)+"m "+(s%60)+"s";
  return Math.floor(s/3600)+"h "+Math.floor((s%3600)/60)+"m"; }
async function loadStatus(){
  const d=await (await api("/api/admin/status")).json();
  const p=d.process;
  $("#stats").innerHTML=[
    ["version","v"+p.version],["uptime",dur(p.uptime_s)],["active runs",p.active_runs],["tools",p.tools],
    ["users",p.users],["brain model",p.model]
  ].map(([k,v])=>`<div class="stat"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join("");
  $("#svc").innerHTML=[{name:"JayNet web (this console)", self:true, ok:true,
                       status:200, latency_ms:0, url:""}, ...d.services].map(s=>{
    const up=s.ok; const detail=up?((s.status||"")+" · "+(s.latency_ms||0)+" ms"):(s.error||("status "+s.status));
    // restartable units: the console itself (jaynet-web) and the proxy
    const rk=s.self?"jaynet-web":(s.name==="LiteLLM proxy"?"litellm-proxy":"");
    return `<tr><td>${esc(s.name)}<div class="muted" style="font-size:11px">${esc(s.url||"")}</div></td>
      <td><span class="pill st ${up?'up':'down'}">${up?'up':'down'}</span></td><td class="muted">${esc(detail)}</td>
      <td>${rk?`<button class="mini" data-restart="${rk}">restart</button>`:""}</td></tr>`;
  }).join("");
  const fallbacks=d.model_fallbacks||[];
  $("#fallbacks").hidden=!fallbacks.length;
  $("#fallbacks").innerHTML=fallbacks.length
    ? "<b>Model fallback detected</b> — the proxy answered with a different model. "
      +"Check the requested backend and chat template; the fallback hides the original error. "
      +"<div class='muted' style='font-size:11px;margin-top:4px'>Recent events since web restart:<br>"
      +fallbacks.slice(-5).reverse().map(e=>
        `${esc(new Date(e.ts*1000).toLocaleString())}: ${esc(e.requested)} → ${esc(e.served)}`
      ).join("<br>")+"</div>"
    : "";
  $("#storage").innerHTML=(d.storage||[]).map(s=>
    `<tr><td>${esc(s.name)}</td><td class="muted">${esc(s.path)}</td><td>${bytes(s.size_bytes)}</td></tr>`).join("")
    || "<tr><td class='muted'>no stores yet</td></tr>";
}
$("#refreshStatus").onclick=loadStatus;
// updates card: installed vs upstream versions of the external components
// (report-only — the upgrade hint tells you the command; nothing auto-updates).
// Cached 24h server-side; "Check now" forces a fresh probe.
async function loadUpdates(refresh){
  const d=await (await api("/api/admin/updates"+(refresh?"?refresh=1":""))).json();
  if(d.enabled===false){ $("#updWhen").textContent="";
    $("#upd").innerHTML="<tr><td class='muted'>update check disabled (updates.enabled)</td></tr>"; return; }
  $("#updWhen").textContent="checked "+(d.checked_at_iso||"never")+" (cached 24h — Check now refreshes)";
  $("#upd").innerHTML=(d.components||[]).map(c=>{
    const cls=c.status==="current"?"up":(c.status==="behind"?"down":"");
    const latest=c.latest?esc(c.latest)+(c.pinned?` <span class="muted">(pin ${esc(c.pinned)})</span>`:""):"—";
    const detail=c.detail?Object.entries(c.detail).map(([k,v])=>`${esc(k)}: ${esc(v)}`).join("<br>"):"";
    return `<tr><td>${esc(c.component)}${detail?`<div class="muted" style="font-size:11px">${detail}</div>`:""}</td>
      <td>${esc(c.installed||"—")}</td><td>${latest}</td>
      <td><span class="pill st ${cls}">${esc(c.status)}</span></td>
      <td class="muted" style="font-size:11px">${esc(c.hint||"")}</td></tr>`;
  }).join("")||"<tr><td class='muted'>no components probed</td></tr>";
}
$("#refreshUpdates").onclick=()=>loadUpdates(true);
// per-service restart (whitelisted server-side: jaynet-web self + litellm-proxy)
$("#svc").addEventListener("click", async e=>{
  const b=e.target.closest("[data-restart]"); if(!b) return;
  const svc=b.dataset.restart; const n=$("#svcNote");
  if(svc==="jaynet-web"){
    if(!await dlgConfirm("Restart the JayNet web console? The page drops — reload in a few seconds.", {yes:"restart"})) return;
    await api("/api/admin/services/restart",{method:"POST",body:JSON.stringify({service:svc})});
    n.hidden=false; n.className="note ok";
    n.textContent="restarting — reload the page in a few seconds";
    setTimeout(()=>location.reload(), 8000);
  }else{
    if(!await dlgConfirm("Restart "+svc+"? Requests in flight will fail.", {yes:"restart"})) return;
    const r=await api("/api/admin/services/restart",{method:"POST",body:JSON.stringify({service:svc})});
    n.hidden=false;
    if(r.ok){ n.className="note ok"; n.textContent=svc+" restarted"; setTimeout(loadStatus, 2500); }
    else{ n.className="note err"; n.textContent=(await r.json()).detail||"restart failed"; }
  }
});

// hardware (RAM, CPU temp, GPU VRAM/temp)
async function loadHardware(){
  const d=await (await api("/api/admin/hardware")).json();
  const r=d.ram||{}; const stats=[];
  if(r.total_gib!=null){
    stats.push(["RAM", r.used_gib+" / "+r.total_gib+" GiB"]);
    stats.push(["RAM used", r.used_pct+" %"]);
  }
  if(d.cpu_temp_c!=null) stats.push(["CPU temp", Math.round(d.cpu_temp_c)+" °C"]);
  $("#hwStats").innerHTML=stats.map(([k,v])=>
    `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")
    || "<div class='muted'>no hardware data</div>";
  $("#hwGpus").innerHTML=(d.gpus||[]).map(g=>{
    const vram=g.vram_used_gib!=null
      ? g.vram_used_gib+" / "+(g.vram_total_gib!=null?g.vram_total_gib:"?")+" GiB"
        +(g.vram_used_pct!=null?" ("+g.vram_used_pct+" %)":"")
      : "—";
    return `<tr><td>${esc(g.name||g.card)}</td><td>${vram}</td>
      <td>${g.temp_c!=null?Math.round(g.temp_c)+" °C":"—"}</td>
      <td>${g.gpu_util_pct!=null?Math.round(g.gpu_util_pct)+" %":"—"}</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">${esc(d.gpu_error||"no GPU data")}</td></tr>`;
}
$("#refreshHw").onclick=loadHardware;

// logs
async function loadLogs(){
  const d=await (await api("/api/admin/logs?limit=40")).json();
  $("#logs").innerHTML=(d.runs||[]).map(r=>{
    const cls=r.status==="ok"?"s-ok":(r.status==="running"?"s-run":"s-err");
    const t=r.started_at?new Date(r.started_at*1000).toLocaleString():"";
    return `<div class="logrow" data-run="${esc(r.id)}">
      <span class="${cls}">${esc((r.status||"?").padEnd(8))}</span> ${t} ·
      ${r.duration_s!=null?r.duration_s+"s":"—"} · ${(r.message||"").replace(/</g,"&lt;")}</div>`;
  }).join("") || "<div class='muted'>no runs recorded</div>";
  $("#logs").querySelectorAll(".logrow").forEach(el=>el.onclick=()=>showRun(el.dataset.run));
}
async function showRun(id){
  const d=await (await api("/api/admin/logs?run_id="+id)).json();
  const pre=$("#logDetail"); pre.style.display="block";
  pre.textContent=(d.events||[]).map(e=>{
    const t=new Date(e.ts*1000).toLocaleTimeString();
    return `[${t}] (${e.iteration}) ${e.kind}: ${(e.payload_json||"").slice(0,400)}`;
  }).join("\n") || "(no events)";
  pre.scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#refreshLogs").onclick=loadLogs;

// users
async function loadUsers(){
  const d=await (await api("/api/admin/users")).json();
  $("#users").innerHTML=d.users.map(u=>`
    <tr><td>${u.username}</td>
      <td>${u.is_admin?"<span class='pill up'>admin</span>":"<span class='pill'>user</span>"}</td>
      <td>${u.twofa?"<span class='pill st up'>on</span>":"<span class='pill'>off</span>"}</td>
      <td><button class="pill st ${u.schedules_allowed?"up":"down"}" data-sched="${u.username}" data-on="${u.schedules_allowed?1:0}" title="allow prompt schedules">${u.schedules_allowed?"on":"off"}</button></td>
      <td class="muted">${(u.created_at||"").replace("T"," ").replace("Z","")}</td>
      <td style="text-align:right">
        <button data-pw="${u.username}">reset pw</button>
        ${u.twofa?`<button data-2fa="${u.username}">reset 2FA</button>`:""}
        <button data-adm="${u.username}" data-is="${u.is_admin?1:0}">${u.is_admin?"revoke admin":"make admin"}</button>
        <button class="danger" data-del="${u.username}">delete</button>
      </td></tr>`).join("");
  $("#users").querySelectorAll("[data-2fa]").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Reset (disable) 2FA for "+b.dataset["2fa"]+"? They can re-enroll after.", {yes:"reset"})) return;
    const r=await api("/api/admin/users/"+b.dataset["2fa"]+"/2fa/reset",{method:"POST"});
    if(!r.ok) await dlgAlert((await r.json()).detail); loadUsers();
  });
  $("#users").querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete user "+b.dataset.del+"?", {yes:"delete"}))return;
    const r=await api("/api/admin/users/"+b.dataset.del,{method:"DELETE"});
    if(!r.ok) await dlgAlert((await r.json()).detail); loadUsers();
  });
  $("#users").querySelectorAll("[data-sched]").forEach(b=>b.onclick=async()=>{
    const r=await api("/api/admin/users/"+b.dataset.sched+"/schedules",{method:"POST",
      headers:{"content-type":"application/json"},body:JSON.stringify({allowed:b.dataset.on!=="1"})});
    if(!r.ok) await dlgAlert((await r.json()).detail); loadUsers();
  });
  $("#users").querySelectorAll("[data-pw]").forEach(b=>b.onclick=async()=>{
    const pw=await dlgPrompt("New password for "+b.dataset.pw, {yes:"set password"}); if(!pw)return;
    const r=await api("/api/admin/users/"+b.dataset.pw+"/password",{method:"POST",
      headers:{"content-type":"application/json"},body:JSON.stringify({password:pw})});
    await dlgAlert(r.ok?"updated":"error");
  });
  $("#users").querySelectorAll("[data-adm]").forEach(b=>b.onclick=async()=>{
    const r=await api("/api/admin/users/"+b.dataset.adm+"/admin",{method:"POST",
      headers:{"content-type":"application/json"},body:JSON.stringify({is_admin:b.dataset.is!=="1"})});
    if(!r.ok) await dlgAlert((await r.json()).detail); loadUsers();
  });
}
$("#addUser").onclick=async()=>{
  const r=await api("/api/admin/users",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({username:$("#nu").value,password:$("#np").value,is_admin:$("#na").checked})});
  if(r.ok){ $("#nu").value="";$("#np").value="";$("#na").checked=false; note($("#userNote"),"added",true); loadUsers(); }
  else note($("#userNote"),(await r.json()).detail||"error",false);
};
$("#logout").onclick=async()=>{ await fetch("/api/logout",{method:"POST"});
  try{ ["jaynet.chat","jaynet.inputHistory","jaynet.activeRun"].forEach(k=>localStorage.removeItem(k)); }catch(e){}
  location.href="/login"; };  // FE-1: clear persisted chat data on logout

function fmtBytes(n){ n=n||0; return n<1024?n+" B":(n<1048576?(n/1024).toFixed(0)+" KB":(n/1048576).toFixed(1)+" MB"); }
async function loadRag(){
  const d=await (await api("/api/admin/rag")).json();
  $("#ragSummary").textContent=(d.collections.length||0)+" collection(s), "+
    (d.total_chunks||0)+" chunks · db "+fmtBytes(d.db_bytes)+" · "+(d.db_path||"");
  const tb=$("#ragRows"); tb.innerHTML="";
  if(!d.collections.length){ tb.innerHTML="<tr><td colspan=5 class='muted'>empty</td></tr>"; }
  for(const c of d.collections){
    const tr=document.createElement("tr");
    const sz=fmtBytes((c.text_bytes||0)+(c.embedding_bytes||0));
    tr.innerHTML="<td>"+esc(c.collection)+"</td><td>"+c.sources+"</td><td>"+c.chunks+"</td><td>"+sz+"</td>"+
      "<td><button data-del='"+esc(encodeURIComponent(c.collection))+"' style='color:var(--warn);border-color:var(--warn)'>delete</button></td>";
    tb.appendChild(tr);
  }
  tb.querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{
    const col=decodeURIComponent(b.dataset.del);
    if(!await dlgConfirm("Delete collection \""+col+"\" and all its chunks?", {yes:"delete"})) return;
    const r=await api("/api/admin/rag/"+encodeURIComponent(col),{method:"DELETE"});
    const j=await r.json(); note($("#ragNote"),"deleted "+(j.deleted||0)+" chunks",true); loadRag();
  });
}
$("#refreshRag").onclick=loadRag;
$("#ragEmpty").onclick=async()=>{
  if(!await dlgConfirm("Empty the ENTIRE RAG store? This deletes every collection and cannot be undone.", {yes:"empty"})) return;
  const r=await api("/api/admin/rag/empty",{method:"POST"});
  const j=await r.json(); note($("#ragNote"),"emptied — removed "+(j.deleted||0)+" chunks",true); loadRag();
};

// plugins (optional capability bundles; runtime/plugins.py)
async function loadPlugins(){
  const d=await (await api("/api/admin/plugins")).json();
  const tb=$("#pluginRows"); tb.innerHTML="";
  if(!d.plugins.length){
    tb.innerHTML="<tr><td colspan=6 class='muted'>no plugins found — drop one into the data dir's plugins/ folder</td></tr>";
    return;
  }
  for(const p of d.plugins){
    const provides=[];
    if(p.has_tools) provides.push("tools");
    if(p.has_skills) provides.push("skills");
    if(p.hooks&&p.hooks.length) provides.push("hooks");
    if(p.has_routes) provides.push("routes");
    if(p.has_ui) provides.push("ui");
    const state=p.state==="loaded"&&p.live===false
      ?"<span style='color:var(--warn)'>not loaded</span>"
      :p.state==="loaded"?"<span style='color:var(--ok)'>loaded</span>"
      :p.state==="disabled"?"<span class='muted'>disabled</span>"
      :"<span style='color:var(--warn)'>unavailable</span>";
    const bins=(p.missing_bins&&p.missing_bins.length)
      ?" · <span style='color:var(--warn)'>needs bin: "+esc(p.missing_bins.join(", "))+"</span>":"";
    const readme=p.readme
      ?"<details style='margin-top:4px'><summary style='cursor:pointer'>readme</summary>"+
       "<pre style='white-space:pre-wrap;font-size:11px;max-height:260px;overflow:auto'>"+esc(p.readme)+"</pre></details>":"";
    const tr=document.createElement("tr");
    tr.innerHTML="<td><b>"+esc(p.name)+"</b><div class='muted' style='font-size:11px'>"+esc(p.description||"")+
      (p.reason?" · "+esc(p.reason):"")+bins+"</div>"+readme+"</td>"+
      "<td>"+p.origin+"</td><td>"+esc(p.version)+"</td><td>"+state+"</td>"+
      "<td>"+(provides.join(", ")||"—")+"</td>"+
      "<td>"+(p.enabled&&p.state==="loaded"&&p.live===false
        ?"<button data-load='"+esc(p.name)+"' title='register tools/skills/routes now — no restart'>load now</button> ":"")+
      (p.has_ui&&p.state==="loaded"
        ?"<button data-ui='"+esc(p.name)+"'>open</button> ":"")+
      "<button data-exp='"+esc(p.name)+"' title='download as .jayplugin'>export</button> "+
      "<button data-pl='"+esc(p.name)+"' data-en='"+(p.enabled?"0":"1")+"'>"+(p.enabled?"disable":"enable")+"</button></td>";
    tb.appendChild(tr);
  }
  tb.querySelectorAll("[data-ui]").forEach(b=>b.onclick=()=>{
    const name=b.dataset.ui;
    const url="/api/admin/plugins/"+encodeURIComponent(name)+"/ui/";
    const panel=$("#pluginUiPanel");
    $("#pluginUiTitle").textContent=name;
    $("#pluginUiPop").href=url;
    $("#pluginUiFrame").src=url;
    panel.hidden=false;
    panel.scrollIntoView({behavior:"smooth",block:"nearest"});
  });
  $("#pluginUiClose").onclick=()=>{
    $("#pluginUiFrame").src="about:blank";
    $("#pluginUiPanel").hidden=true;
  };
  tb.querySelectorAll("[data-exp]").forEach(b=>b.onclick=()=>{
    window.location="/api/admin/studio/export/plugin/"+encodeURIComponent(b.dataset.exp);
  });
  tb.querySelectorAll("[data-pl]").forEach(b=>b.onclick=async()=>{
    const r=await api("/api/admin/plugins/"+encodeURIComponent(b.dataset.pl)+"/toggle",
      {method:"POST",body:JSON.stringify({enabled:b.dataset.en==="1"})});
    const j=await r.json();
    note($("#pluginNote"),j.note||"toggled",true);
    loadPlugins();
  });
  tb.querySelectorAll("[data-load]").forEach(b=>b.onclick=async()=>{
    const r=await api("/api/admin/plugins/"+encodeURIComponent(b.dataset.load)+"/toggle",
      {method:"POST",body:JSON.stringify({enabled:true})});
    const j=await r.json();
    note($("#pluginNote"),j.note||"loaded",true);
    loadPlugins();
  });
}
$("#refreshPlugins").onclick=loadPlugins;
// Install a .jayplugin pack (kind "plugin" via the studio pack import). The
// fresh plugin shows a "load now" button — hot-reload registers it without
// a restart (only brand-new pip dependencies still need one).
$("#installPlugin").onclick=()=>$("#pluginPackFile").click();
$("#pluginPackFile").onchange=async(e)=>{
  const f=e.target.files[0]; e.target.value="";
  if(!f) return;
  const send=async(overwrite)=>{
    const fd=new FormData(); fd.append("file",f);
    const r=await api("/api/admin/studio/import"+(overwrite?"?overwrite=true":""),
      {method:"POST",body:fd});
    return {status:r.status,body:await r.json().catch(()=>({}))};
  };
  let res=await send(false);
  if(res.status===409){
    const detail=res.body&&res.body.detail;
    const msg=(detail&&detail.error)||"plugin already exists";
    if(!await dlgConfirm(msg+" — overwrite?",{yes:"overwrite"})) return;
    res=await send(true);
  }
  if(res.body&&res.body.ok){
    note($("#pluginNote"),"installed '"+res.body.name+"' — hit 'load now' in the row to activate it (no restart)",true);
    loadPlugins();
  }else{
    const detail=res.body&&res.body.detail;
    note($("#pluginNote"),"install failed: "+((detail&&detail.error)||detail||res.status),false);
  }
};

// presets (DB-backed catalog; see runtime/preset_store.py)
let _presets=null;
function slotOf(name){ const out=[]; for(const [s,p] of Object.entries(_presets.slots||{})) if(p===name) out.push(s); return out; }
function renderStrengthsHint(){
  const el=$("#peStrengthsHint"); if(!el||!_presets) return;
  const reg=_presets.strengths_registry||{};
  const carriers={};
  (_presets.presets||[]).forEach(p=>(p.strengths||[]).forEach(t=>{
    (carriers[t]=carriers[t]||[]).push(p.name); }));
  const parts=Object.entries(reg).map(([t,d])=>{
    const c=carriers[t];
    return `<b>${esc(t)}</b> = ${esc(d)} <span class="muted">(${c?"on: "+esc(c.join(", ")):"unused"})</span>`;
  });
  const extra=Object.keys(carriers).filter(t=>!(t in reg))
    .map(t=>`<b>${esc(t)}</b> <span class="muted">(custom tag)</span>`);
  el.innerHTML=(parts.concat(extra).join(" · ")||"no tags registered (models.strengths)")+
    ' · <span class="muted">allround = catch-all fallback · delegation routes by these (agent.spawn strength=…)</span>';
}
// strengths registry editor (models.strengths + agent.role_temperature).
// No dedicated endpoint: entries are plain config-overrides via
// PUT /api/admin/config (dotpath set, null = revert to the shipped YAML) and
// apply on the next run — the brain's strength directory renders per run.
let _strengths=null;
async function loadStrengths(){
  // models.strengths.* is hidden from GET /api/admin/config (its home is
  // THIS pane, not the Runtime editor — v1.30.0), so descriptions come from
  // the presets payload's strengths_registry: the live merged config,
  // overrides applied. role_temperature keys still ride the config flat map.
  const [d,p]=await Promise.all([
    (await api("/api/admin/config")).json(),
    (await api("/api/admin/presets")).json()]);
  const flat=d.config||{}, tags={};
  const reg=(p&&p.strengths_registry)||{};
  for(const t of Object.keys(reg)) (tags[t]=tags[t]||{}).desc=reg[t];
  for(const k of Object.keys(flat)){
    if(k.startsWith("agent.role_temperature.")) (tags[k.slice(23)]=tags[k.slice(23)]||{}).temp=flat[k];
  }
  _strengths={tags,ov:d.overrides||{}};
  renderStrengths();
}
function renderStrengths(){
  if(!_strengths) return;
  const carriers={};
  ((_presets&&_presets.presets)||[]).forEach(p=>(p.strengths||[]).forEach(t=>{
    (carriers[t]=carriers[t]||[]).push(p.name); }));
  $("#strengthRows").innerHTML=Object.keys(_strengths.tags).sort().map(t=>{
    const m=_strengths.tags[t];
    const edited=("models.strengths."+t) in _strengths.ov || ("agent.role_temperature."+t) in _strengths.ov;
    const used=carriers[t]?`on ${esc(carriers[t].join(", "))}`:"unused";
    return `<tr><td><span class="pill">${esc(t)}</span><br><span class="muted" style="font-size:11px">${esc(used)}</span></td>
      <td><input data-st-desc="${esc(t)}" value="${esc(m.desc||"")}" style="width:100%"></td>
      <td><input data-st-temp="${esc(t)}" type="number" min="0" max="2" step="0.05" value="${m.temp!=null?esc(String(m.temp)):""}" style="width:70px" title="child temperature override — empty = preset default"></td>
      <td><span class="muted" style="font-size:11px">${edited?"edited":"shipped"}</span></td>
      <td style="white-space:nowrap"><button data-st-save="${esc(t)}">Save</button>
        ${edited?`<button data-st-reset="${esc(t)}" title="remove the override — a shipped default reverts, a custom tag disappears">Reset</button>`:""}</td></tr>`;
  }).join("")||`<tr><td colspan="5" class="muted">no tags registered</td></tr>`;
  $("#strengthRows").querySelectorAll("[data-st-save]").forEach(b=>b.onclick=()=>stSave(b.dataset.stSave));
  $("#strengthRows").querySelectorAll("[data-st-reset]").forEach(b=>b.onclick=()=>stReset(b.dataset.stReset));
}
async function stPut(updates){
  const r=await api("/api/admin/config",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({updates})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#stNote"),j.detail||"error",false); return false; }
  return true;
}
async function stSave(tag){
  const desc=document.querySelector(`[data-st-desc="${tag}"]`).value.trim();
  const tempRaw=document.querySelector(`[data-st-temp="${tag}"]`).value.trim();
  const updates={["agent.role_temperature."+tag]:tempRaw===""?null:parseFloat(tempRaw)};
  updates["models.strengths."+tag]=desc||null;   // empty desc = drop the entry (reverts if shipped)
  if(!await stPut(updates)) return;
  note($("#stNote"),desc?`saved '${tag}'`:`removed '${tag}'`,true);
  await loadStrengths(); await loadPresets();
}
async function stReset(tag){
  if(!await stPut({["models.strengths."+tag]:null,["agent.role_temperature."+tag]:null})) return;
  note($("#stNote"),`reset '${tag}'`,true);
  await loadStrengths(); await loadPresets();
}
$("#stAdd").onclick=async()=>{
  const tag=$("#stNewTag").value.trim();
  if(!/^[a-z0-9][a-z0-9-]*$/.test(tag)){ note($("#stNote"),"tag: lowercase letters, digits, dashes",false); return; }
  const desc=$("#stNewDesc").value.trim();
  if(!desc){ note($("#stNote"),"description required — the brain routes by it",false); return; }
  const tempRaw=$("#stNewTemp").value.trim();
  const updates={["models.strengths."+tag]:desc};
  if(tempRaw!=="") updates["agent.role_temperature."+tag]=parseFloat(tempRaw);
  if(!await stPut(updates)) return;
  $("#stNewTag").value=""; $("#stNewDesc").value=""; $("#stNewTemp").value="";
  note($("#stNote"),`added '${tag}'`,true);
  await loadStrengths(); await loadPresets();
};

async function loadPresets(){
  _presets=await (await api("/api/admin/presets")).json();
  renderStrengthsHint();
  if(_strengths) renderStrengths();   // carrier list may have changed
  $("#slotRows").innerHTML=(_presets.slot_names||[]).map(s=>{
    const none=`<option value="" ${!_presets.slots[s]?"selected":""}>(none — disabled)</option>`;
    const opts=_presets.presets.map(p=>`<option value="${esc(p.name)}" ${_presets.slots[s]===p.name?"selected":""}>${esc(p.name)}</option>`).join("");
    return `<div class="brow"><label>${esc(s)}</label><select data-slot="${esc(s)}">${s==="brain"?opts:none+opts}</select></div>`;
  }).join("");
  $("#slotRows").querySelectorAll("select").forEach(sel=>sel.onchange=async()=>{
    const r=await api("/api/admin/preset-slots",{method:"PUT",headers:{"content-type":"application/json"},
      body:JSON.stringify({updates:{[sel.dataset.slot]:sel.value}})});
    const j=await r.json();
    if(!r.ok){ note($("#slotNote"),j.detail||"error",false); loadPresets(); return; }
    _presets=j; renderPresetRows(); note($("#slotNote"),"saved — restart the process to apply",true);
  });
  renderGpuRows(); renderBinRows(); renderPresetRows();
}
function renderBinRows(){
  const rows=(_presets.binaries||[]).map((b,i)=>
    `<div class="brow"><label>binary</label>
      <input data-bf="name" value="${esc(b.name)}" placeholder="rocm" style="width:90px">
      <input data-bf="path" value="${esc(b.path||"")}" placeholder="/path/to/llama-server" style="width:340px">
      <input data-bf="env" value="${esc(b.device_env||"")}" placeholder="HIP_VISIBLE_DEVICES (empty = CPU, no pinning)" style="width:200px">
      <select data-bf="type" title="launch backend — llama argv via start-model.sh; vllm/radiance via runtime/launch_argv.py (shared conf keys translate)">
        ${["llama","vllm","radiance"].map(t=>`<option value="${t}" ${t===(b.type||"llama")?"selected":""}>${t}</option>`).join("")}
      </select>
      ${b.device_env?"":'<span class="muted" title="no GPU pinning — fine for CPU-only builds">cpu/none</span>'}
      ${b.exists===false?'<span class="pill" title="not executable on this host">missing</span>':""}
      <button data-bhelp="${esc(b.name)}" title="show this binary's --help output" ${b.name?"":"disabled"}>help</button>
      <button class="danger" data-bdel="${i}">remove</button></div>`);
  // The launcher's implicit fallback for binary-less presets — read-only,
  // so the hardcoded default is visible instead of magic.
  const d=_presets.default_binary;
  if(d) rows.push(`<div class="brow muted"><label>default</label>
      <span style="width:90px;display:inline-block">(implicit)</span>
      <code style="width:340px;display:inline-block;overflow:hidden;text-overflow:ellipsis">${esc(d.path)}</code>
      <span style="width:200px;display:inline-block"></span>
      ${d.exists===false?'<span class="pill" title="not executable on this host — presets without a binary will fail to start">missing</span>':""}</div>`);
  $("#binRows").innerHTML=rows.join("");
  $("#binRows").querySelectorAll("[data-bhelp]").forEach(b=>b.onclick=()=>showBinHelp(b.dataset.bhelp));
  $("#binRows").querySelectorAll("[data-bdel]").forEach(b=>b.onclick=()=>{
    _presets.binaries.splice(parseInt(b.dataset.bdel,10),1); renderBinRows();
  });
}
$("#binAdd").onclick=()=>{ (_presets.binaries=_presets.binaries||[]).push({name:"",path:"",device_env:"",type:"llama"}); renderBinRows(); };
$("#binSave").onclick=async()=>{
  const rows=[...$("#binRows").querySelectorAll(".brow")].map(r=>{
    const f=n=>r.querySelector(`[data-bf="${n}"]`).value.trim();
    return {name:f("name"),path:f("path"),device_env:f("env"),type:f("type")};
  });
  const r=await api("/api/admin/binaries",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({binaries:rows})});
  const j=await r.json();
  if(!r.ok){ note($("#binNote"),j.detail||"error",false); return; }
  _presets=j; renderBinRows(); renderPresetRows(); note($("#binNote"),"saved",true);
};

// ---- cloud models (llm.call escalation path) ----
let _cloud=null, _ceTarget=null;   // _ceTarget: name being edited, null = new
async function loadCloud(){ _cloud=await (await api("/api/admin/cloud-models")).json(); renderCloudRows(); }
function renderCloudRows(){
  $("#cloudRows").innerHTML=(_cloud.models||[]).map(m=>{
    const key=m.key_env?(m.key_set?'<span class="pill st up">set</span>':'<span class="pill st down">missing</span>')
      :'<span class="muted">—</span>';
    const state=m.enabled?'<span class="pill st up">on</span>':'<span class="pill">off</span>';
    return `<tr${m.enabled?"":' class="muted" style="opacity:.55"'}><td><b>${esc(m.name)}</b><div class="muted" style="font-size:11px">${esc(m.role||"")}</div></td>
      <td class="muted">${esc(m.litellm_alias)}</td>
      <td class="muted" style="font-size:12px">${esc(m.provider_model)}</td>
      <td>${key}</td>
      <td style="font-size:12px">${m.input_cost} / ${m.output_cost}</td>
      <td style="font-size:12px">${esc(m.thinking)}</td>
      <td>${state}</td>
      <td style="text-align:right;white-space:nowrap">
        <button data-cedit="${esc(m.name)}">edit</button>
        <button class="danger" data-cdel="${esc(m.name)}">delete</button></td></tr>`;
  }).join("");
  $("#cloudRows").querySelectorAll("[data-cedit]").forEach(b=>b.onclick=()=>editCloud(b.dataset.cedit));
  $("#cloudRows").querySelectorAll("[data-cdel]").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete cloud model "+b.dataset.cdel+"?", {yes:"delete"})) return;
    const rows=(_cloud.models||[]).filter(m=>m.name!==b.dataset.cdel);
    const r=await api("/api/admin/cloud-models",{method:"PUT",headers:{"content-type":"application/json"},
      body:JSON.stringify({models:rows})});
    const j=await r.json();
    if(!r.ok){ note($("#cloudNote"),j.detail||"error",false); return; }
    _cloud=j; renderCloudRows(); note($("#cloudNote"),"deleted — "+(j.proxy||"applied"),true);
  });
}
function editCloud(name){
  const m=name?(_cloud.models||[]).find(x=>x.name===name):null;
  _ceTarget=m?m.name:null;
  $("#cloudEditTitle").textContent=m?("Edit cloud model: "+m.name):"New cloud model";
  $("#ceName").value=m?m.name:""; $("#ceName").disabled=!!m;
  $("#ceAlias").value=m?m.litellm_alias:"";
  $("#ceProvider").value=m?m.provider_model:"";
  $("#ceBase").value=m?m.api_base||"":"";
  $("#ceKeyEnv").value=m?m.key_env||"":"";
  $("#ceKeyPill").innerHTML=m&&m.key_env?(m.key_set?'<span class="pill st up">set</span>':'<span class="pill st down">missing</span>'):"";
  $("#ceCostIn").value=m?m.input_cost:0; $("#ceCostOut").value=m?m.output_cost:0;
  $("#ceThinking").value=m?m.thinking:"on";
  $("#ceFallbacks").value=m?(m.fallbacks||[]).join(", "):"local-orchestrator";
  $("#ceRole").value=m?m.role||"":"";
  $("#ceEnabled").checked=m?m.enabled:true;
  $("#cloudEditSec").style.display="block";
  $("#cloudEditSec").scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#cloudAdd").onclick=()=>editCloud(null);
$("#ceCancel").onclick=()=>{ $("#cloudEditSec").style.display="none"; };
$("#ceSave").onclick=async()=>{
  const num=id=>{const v=parseFloat($(id).value);return Number.isFinite(v)?v:0;};
  const row={ name:$("#ceName").value.trim(), litellm_alias:$("#ceAlias").value.trim(),
    provider_model:$("#ceProvider").value.trim(), api_base:$("#ceBase").value.trim(),
    key_env:$("#ceKeyEnv").value.trim(), input_cost:num("#ceCostIn"), output_cost:num("#ceCostOut"),
    thinking:$("#ceThinking").value,
    fallbacks:$("#ceFallbacks").value.split(",").map(s=>s.trim()).filter(Boolean),
    enabled:$("#ceEnabled").checked, role:$("#ceRole").value.trim() };
  const rows=(_cloud.models||[]).filter(m=>m.name!==_ceTarget);
  const at=_ceTarget?(_cloud.models||[]).findIndex(m=>m.name===_ceTarget):-1;
  rows.splice(at<0?rows.length:at,0,row);          // keep the original position
  const r=await api("/api/admin/cloud-models",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({models:rows})});
  const j=await r.json();
  if(!r.ok){ note($("#ceNote"),j.detail||"error",false); return; }
  _cloud=j; renderCloudRows(); $("#cloudEditSec").style.display="none";
  note($("#cloudNote"),"saved — "+(j.proxy||"applied"),true);
};
function renderGpuRows(){
  $("#gpuRows").innerHTML=(_presets.gpus||[]).map((g,i)=>
    `<div class="brow"><label>GPU</label>
      <input data-gf="id" value="${esc(g.id)}" placeholder="0" style="width:60px">
      <input data-gf="label" value="${esc(g.label||"")}" placeholder="label (e.g. RX 7900 XTX)" style="width:220px">
      <input data-gf="vram" type="number" min="0" step="1" value="${g.vram_gib??""}" placeholder="VRAM GiB" style="width:90px">
      ${g.free_gib!=null?`<span class="muted" style="font-size:12px">free now: ${g.free_gib} GiB</span>`:""}
      <button class="danger" data-gdel="${i}">remove</button></div>`).join("");
  $("#gpuRows").querySelectorAll("[data-gdel]").forEach(b=>b.onclick=()=>{
    _presets.gpus.splice(parseInt(b.dataset.gdel,10),1); renderGpuRows();
  });
}
$("#gpuAdd").onclick=()=>{ (_presets.gpus=_presets.gpus||[]).push({id:"",label:"",vram_gib:null}); renderGpuRows(); };
$("#gpuSave").onclick=async()=>{
  const rows=[...$("#gpuRows").querySelectorAll(".brow")].map(r=>{
    const f=n=>r.querySelector(`[data-gf="${n}"]`).value.trim();
    return {id:f("id"),label:f("label"),vram_gib:f("vram")===""?null:parseFloat(f("vram"))};
  });
  const r=await api("/api/admin/gpus",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({gpus:rows})});
  const j=await r.json();
  if(!r.ok){ note($("#gpuNote"),j.detail||"error",false); return; }
  _presets=j; renderGpuRows(); renderPresetRows(); note($("#gpuNote"),"saved",true);
};
function gpuLabel(g){ g=g||""; return g===""?"CPU":g.includes(",")?"GPU "+g.split(",").join("+"):"GPU "+g; }
function vramCell(p){
  // Measured footprint (model.measure) beats the hand estimate — the loader
  // schedules by it. A stale record (ctx/GPU edited since) is ignored by the
  // planner, so it gets the ~ marker and a say-so tooltip.
  const mm=p.measured||{};
  if(mm.total_vram_gib!=null){
    const per=Object.entries(mm.vram_gib||{}).map(([g,v])=>`GPU${g} ${v}`).join(" + ");
    const tip=`measured ${mm.at||""}${p.measured_stale?" — STALE: ctx/GPU/binary changed since, re-run model.measure":""}${per?" · "+per:""}${mm.ram_gib!=null?` · RAM ${mm.ram_gib} GiB`:""}`;
    return `<span ${p.measured_stale?'class="muted" ':''}title="${esc(tip)}">${mm.total_vram_gib}${p.measured_stale?" ~":" ✓"}</span>`;
  }
  if(p.vram_gib!=null) return `<span class="muted" title="hand estimate — model.measure records the real footprint">${p.vram_gib}?</span>`;
  return `<span class="muted" title="no footprint known — the loader can't pack this model by fit">—</span>`;
}
function renderPresetRows(){
  $("#presetRows").innerHTML=_presets.presets.map(p=>{
    const slots=slotOf(p.name).map(s=>`<span class="pill up">${esc(s)}</span>`).join(" ");
    return `<tr${p.archived?' class="muted" style="opacity:.55"':""}><td><b>${esc(p.name)}</b>${p.archived?' <span class="pill">archived</span>':""}</td><td>${slots}</td><td>${p.remote_host?`<span class="pill">remote${p.backend&&p.backend!=="llama"?" · "+esc(p.backend):""}</span> ${esc(p.remote_host)}`:esc(gpuLabel(p.gpu))}</td><td class="muted">${esc(p.binary||"default")}</td><td>${p.port??""}</td>
      <td class="muted">${esc(p.alias||"")}</td><td>${vramCell(p)}</td>
      <td>${(p.strengths||[]).map(t=>`<span class="pill">${esc(t)}</span>`).join(" ")}</td>
      <td class="muted" style="font-size:12px">${esc(p.role||"")}</td>
      <td style="text-align:right;white-space:nowrap">
        ${p.remote_host||p.archived||!p.alias?"":`<button data-measure="${esc(p.name)}" title="model.measure: hibernates ALL models (brain too), loads this preset, probes it once, writes the real VRAM/RAM into the catalog, then restores — a few minutes, chat down meanwhile">measure</button>`}
        <button data-edit="${esc(p.name)}">edit</button>
        <button data-dup="${esc(p.name)}" title="Copy as a new preset">duplicate</button>
        <button data-x="${esc(p.name)}" title="Export as .jaypack (shareable preset pack)">export</button>
        <button class="danger" data-del="${esc(p.name)}">delete</button></td></tr>`;
  }).join("");
  $("#presetRows").querySelectorAll("[data-edit]").forEach(b=>b.onclick=()=>editPreset(b.dataset.edit));
  $("#presetRows").querySelectorAll("[data-measure]").forEach(b=>b.onclick=()=>presetMeasure(b.dataset.measure));
  $("#presetRows").querySelectorAll("[data-dup]").forEach(b=>b.onclick=()=>editPreset(b.dataset.dup,true));
  $("#presetRows").querySelectorAll("[data-x]").forEach(b=>b.onclick=()=>{
    window.location="/api/admin/presets/"+encodeURIComponent(b.dataset.x)+"/export";
  });
  $("#presetRows").querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete preset "+b.dataset.del+"?", {yes:"delete"})) return;
    const r=await api("/api/admin/presets/"+encodeURIComponent(b.dataset.del),{method:"DELETE"});
    const j=await r.json();
    if(!r.ok){ note($("#presetNote"),j.detail||"error",false); return; }
    _presets=j; renderPresetRows(); _modelTree=null; note($("#presetNote"),"deleted",true);
  });
}
// -- preset pack import (.jaypack, kind 'preset') --
async function presetImport(file, overwrite){
  const fd=new FormData(); fd.append("file",file);
  const r=await api("/api/admin/presets/import"+(overwrite?"?overwrite=true":""),{method:"POST",body:fd});
  const j=await r.json().catch(()=>({}));
  if(r.status===409 && j.detail && j.detail.manifest){
    const m=j.detail.manifest;
    if(await dlgConfirm("A preset named '"+m.name+"' already exists.\n\nOverwrite it with the packed version?", {yes:"overwrite"}))
      return presetImport(file,true);
    return;
  }
  if(!r.ok){ note($("#presetNote"),(j.detail&&j.detail.error)||j.detail||"import failed",false); return; }
  _presets=j; renderPresetRows(); _modelTree=null;
  note($("#presetNote"),"imported preset '"+j.installed+"' — check device, binary and model paths (they are machine-specific), then assign a slot",true);
}
$("#presetImport").onclick=()=>$("#presetImportFile").click();
$("#presetImportFile").onchange=function(){ if(this.files.length) presetImport(this.files[0],false); this.value=""; };
// ---- HuggingFace downloader (runtime/hf_pull.py) ----
let _hfJobs=[];
function _fmtGb(b){ return b? (b/1e9).toFixed(2)+" GB" : "?"; }
const _HF_KINDS={jinja:"template",bin:"whisper",rad:"radiance",recipe:"recipe"};
async function hfListFiles(){
  const repo=$("#hfRepo").value.trim();
  const r=await api("/api/admin/hf/files?repo="+encodeURIComponent(repo));
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#hfNote"),j.detail||"error",false); $("#hfFiles").innerHTML=""; return; }
  note($("#hfNote"),"",true);
  $("#hfFiles").innerHTML=j.files.length
    ? "<table><tbody>"+j.files.map(f=>
        "<tr><td>"+esc(f.name)+(_HF_KINDS[f.kind]?" <span class='pill'>"+_HF_KINDS[f.kind]+"</span>":"")+"</td><td class='muted'>"+_fmtGb(f.size)+"</td>"+
        "<td style='text-align:right'><button data-dl='"+esc(f.name)+"'>download</button></td></tr>").join("")+
      "</tbody></table>"
    : "<p class='muted' style='font-size:12px'>no .gguf/.jinja/.bin/.rad/.recipe files in this repo</p>";
  $("#hfFiles").querySelectorAll("[data-dl]").forEach(b=>b.onclick=()=>hfDownload(b.dataset.dl));
}
async function hfDownload(file){
  const r=await api("/api/admin/hf/download",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({repo:$("#hfRepo").value.trim(),file:file})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#hfNote"),j.detail||"error",false); return; }
  note($("#hfNote"),"download started",true);
  hfRefreshJobs();
}
async function hfRefreshJobs(){
  const r=await api("/api/admin/hf/jobs");
  const j=await r.json().catch(()=>({}));
  _hfJobs=j.jobs||[];
  renderHfJobs();
  if(_hfJobs.some(x=>x.status==="running")) setTimeout(hfRefreshJobs,2000);
}
function renderHfJobs(){
  $("#hfJobs").innerHTML=_hfJobs.map(j=>{
    let body;
    if(j.status==="running"){
      const pct=j.total_bytes?Math.min(100,Math.round(100*j.done_bytes/j.total_bytes)):0;
      body="<div class='muted' style='font-size:12px'>downloading — "+_fmtGb(j.done_bytes)+
        (j.total_bytes?" / "+_fmtGb(j.total_bytes)+" ("+pct+"%)":"")+"</div>"+
        "<div style='background:var(--border,#2a323d);border-radius:4px;height:8px;margin:6px 0'>"+
        "<div style='background:var(--accent,#E8C04A);height:8px;border-radius:4px;width:"+pct+"%'></div></div>";
    }else if(j.status==="done"){
      body="<div class='muted' style='font-size:12px'>done — "+esc(j.dest)+"</div>";
    }else{
      body="<div style='font-size:12px;color:var(--err,#e06c75)'>"+esc(j.status)+
        (j.error?": "+esc(j.error):"")+"</div>";
    }
    const btns=(j.status==="running"?"<button data-cx='"+j.id+"'>cancel</button>":"")+
      (j.status==="done"&&/\.(gguf|rad)$/i.test(j.file)?"<button class='primary' data-preset='"+j.id+"'>create preset</button>":"")+
      (j.status!=="running"?"<button data-dismiss='"+j.id+"'>dismiss</button>":"");
    return "<div style='border:1px solid var(--border,#2a323d);border-radius:8px;padding:10px 12px;margin:8px 0'>"+
      "<div style='font-size:13px;font-weight:600'>"+esc(j.file)+"</div>"+body+
      "<div class='row' style='margin-top:6px;display:flex;gap:8px'>"+btns+"</div></div>";
  }).join("");
  $("#hfJobs").querySelectorAll("[data-cx]").forEach(b=>b.onclick=async()=>{
    await api("/api/admin/hf/cancel",{method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({id:b.dataset.cx})}); hfRefreshJobs();
  });
  $("#hfJobs").querySelectorAll("[data-dismiss]").forEach(b=>b.onclick=async()=>{
    await api("/api/admin/hf/jobs/"+b.dataset.dismiss,{method:"DELETE"}); hfRefreshJobs();
  });
  $("#hfJobs").querySelectorAll("[data-preset]").forEach(b=>b.onclick=()=>{
    const j=_hfJobs.find(x=>x.id===b.dataset.preset); if(j) hfPreset(j);
  });
}
async function hfPreset(j){
  const r=await api("/api/admin/hf/preset-suggestion?repo="+encodeURIComponent(j.repo)+
    "&file="+encodeURIComponent(j.file));
  const s=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#hfNote"),s.detail||"error",false); return; }
  editPreset(null);
  $("#peName").value=s.name; $("#peAlias").value=s.name; $("#peServed").value=s.name;
  $("#pePort").value=s.port||""; $("#peVram").value=s.vram_gib!=null?s.vram_gib:"";
  peLoadConf(s.conf||"");
  if((s.strengths||[]).length){ peStrengthsRender(s.strengths);
    const h=$("#peStrengthsHint"); if(h) h.textContent="benchmark priors: "+(s.strength_notes||[]).join("; ")+" — priors, not measurements"; }
  let msg="suggestion loaded into the editor below — set the device, review, save";
  const missing=[];
  if(s.mmproj&&!s.mmproj.downloaded) missing.push("mmproj ("+s.mmproj.file+")");
  if(s.chat_template&&!s.chat_template.downloaded) missing.push("chat template ("+s.chat_template.file+")");
  if(missing.length) msg+=" — the conf references a "+missing.join(" and ")+" that isn't downloaded yet";
  note($("#hfNote"),msg,true);
}
$("#hfList").onclick=hfListFiles;
$("#hfRepo").addEventListener("keydown",e=>{ if(e.key==="Enter") hfListFiles(); });

// ---- structured preset editor ----
// One row per key scripts/start-model.sh reads (its whitelist + defaults are
// the authority — unknown conf keys are preserved verbatim via extraLines).
const CONF_KEYS=[
  {key:"MODEL_PATH",label:"model file",type:"file-gguf",def:"",std:true,shared:true,help:"GGUF weights — browse… picks from the models dir"},
  {key:"CTX_SIZE",label:"ctx size",type:"number",def:"131072",std:true,shared:true,help:"context window in tokens"},
  {key:"GPU_LAYERS",label:"gpu layers",type:"number",def:"99",std:true,help:"layers offloaded to GPU (99 = all)"},
  {key:"TEMP",label:"temp",type:"number",def:"0.6",std:true,help:"sampling temperature"},
  {key:"TOP_K",label:"top k",type:"number",def:"20",help:"top-k sampling (0 = off)"},
  {key:"TOP_P",label:"top p",type:"number",def:"0.95",help:"nucleus sampling"},
  {key:"MIN_P",label:"min p",type:"number",def:"0",help:"min-p sampling (0 = off)"},
  {key:"PRESENCE_PENALTY",label:"presence penalty",type:"number",def:"0",help:"penalize tokens already seen"},
  {key:"REPEAT_PENALTY",label:"repeat penalty",type:"number",def:"1.0",help:"1.0 = off"},
  {key:"BATCH_SIZE",label:"batch size",type:"number",def:"2048",help:"prompt-processing batch"},
  {key:"UBATCH_SIZE",label:"ubatch size",type:"number",def:"512",help:"physical batch (min 512 with vision)"},
  {key:"FLASH_ATTN",label:"flash attn",type:"enum",options:["on","off","auto"],def:"on",help:"flash-attention kernel"},
  {key:"SPLIT_MODE",label:"split mode",type:"enum",options:["none","layer","row","tensor"],def:"none",multi:true,help:"multi-GPU split; none = tensor by default on multi-card (pooled decode bandwidth), single card otherwise; layer = pipelined, for mismatched cards"},
  {key:"TENSOR_SPLIT",label:"tensor split",type:"text",def:"",multi:true,help:"per-GPU fractions, e.g. 3,1"},
  {key:"MMAP",label:"mmap",type:"enum",options:["on","off"],def:"on",help:"off = load weights without memory-mapping (--load-mode none) — fixes slow/hanging loads on ROCm"},
  {key:"CACHE_TYPE_K",label:"kv cache K",type:"enum",options:["q8_0","f16","bf16","q4_0"],def:"q8_0",help:"K cache quantization"},
  {key:"CACHE_TYPE_V",label:"kv cache V",type:"enum",options:["q8_0","f16","bf16","q4_0"],def:"q8_0",help:"V cache quantization"},
  {key:"MMPROJ",label:"mmproj (vision)",type:"file-gguf",def:"",help:"vision projector GGUF — empty = text-only"},
  {key:"MMPROJ_OFFLOAD",label:"mmproj offload",type:"enum",options:["off","on"],def:"off",help:"off = keep the projector on CPU"},
  {key:"MTP",label:"mtp (speculative)",type:"enum",options:["off","on"],def:"off",help:"self-speculative decoding (draft-mtp)"},
  {key:"SPEC_DRAFT_N_MAX",label:"spec draft n max",type:"number",def:"2",help:"max draft tokens per step (when MTP on)"},
  {key:"TOOLS_TEMPLATE",label:"tools template",type:"file-jinja",def:"",vllm:true,help:"custom chat-template file — empty = model's own"},
  {key:"THREADS",label:"threads",type:"text",def:"",help:"CPU threads; empty or -1 = llama default"},
  {key:"JINJA",label:"jinja template",type:"enum",options:["yes","no"],def:"yes",help:"use the model's jinja chat template"},
  {key:"EMBEDDINGS",label:"embeddings",type:"enum",options:["","on"],def:"",help:"on = serve embeddings (RAG servers)"},
  {key:"RERANKING",label:"reranking",type:"enum",options:["","on"],def:"",help:"on = serve reranking (implies embeddings)"},
  {key:"POOLING",label:"pooling",type:"enum",options:["","cls","mean","last","rank"],def:"",help:"embedding pooling type"},
  {key:"EXTRA_ARGS",label:"extra args",type:"text",def:"",shared:true,help:"extra server flags, appended verbatim (backend-specific)"},
  {key:"REASONING_FORMAT",label:"reasoning format",type:"enum",options:["","deepseek","none"],def:"",help:"thinking-content parsing — empty = model default"},
  {key:"REASONING_BUDGET",label:"reasoning budget",type:"number",def:"",help:"cap on THINKING tokens — reserves answer room inside the completion cap (orchestrator.sampling.max_tokens, default 8192); ~4096; empty = unlimited; inert if /props shows reasoning_format none (custom think tags) — use reasoning effort then"},
  {key:"REASONING_EFFORT",label:"reasoning effort",type:"enum",options:["","minimal","low","medium","high","xhigh"],def:"",help:"template think-mode level (needs a template with supports_reasoning_effort); use when reasoning budget is a no-op — e.g. K2-Horizon's <ifm|think> tags"},
];
// Per-backend extra keys (launch backend = the preset binary's registry
// type). CONF_KEYS entries marked shared map to every backend; vllm:true
// maps to vllm too; everything else is llama-only and stays inert in the
// conf until the binary type switches back — the launcher warns per key.
const BACKEND_KEYS={
  vllm:[
    {key:"KV_CACHE_DTYPE",label:"kv cache dtype",type:"enum",options:["","auto","fp8","fp8_e4m3","fp8_e5m2"],def:"",std:true,help:"KV cache quantization (llama CACHE_TYPE_K/V equivalent)"},
    {key:"GPU_MEM_UTIL",label:"gpu mem util",type:"number",def:"0.90",std:true,help:"fraction of card VRAM vLLM may claim"},
    {key:"MAX_NUM_SEQS",label:"max num seqs",type:"number",def:"",std:true,help:"max concurrent sequences"},
  ],
  radiance:[
    {key:"KV_CACHE_DTYPE",label:"kv cache dtype",type:"enum",options:["","fp8","bf16","f16"],def:"",std:true,help:"KV cache dtype (llama CACHE_TYPE_K/V equivalent)"},
    {key:"MAX_NUM_SEQS",label:"max num seqs",type:"number",def:"",std:true,help:"max concurrent sequences (≤32; hybrid models ≤8)"},
  ],
};
let _peAdvanced=false, _peFullForm=false, _peExtra=[], _peKnownShadow={};
function peAllKeys(){ return CONF_KEYS.concat(BACKEND_KEYS.vllm, BACKEND_KEYS.radiance); }
function peBackendType(){
  const b=(_presets.binaries||[]).find(x=>x.name===$("#peBin").value);
  return (b && b.type) || "llama";
}
function confToFields(text){
  const known={}, extra=[];
  for(const line of String(text||"").split("\n")){
    const m=/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
    if(m && peAllKeys().some(k=>k.key===m[1])) known[m[1]]=m[2].trim();
    else if(line.trim()!=="") extra.push(line);   // comments, slot keys, unknowns
  }
  return {known:known, extraLines:extra};
}
function fieldsToConf(f){
  const lines=[];
  for(const k of peAllKeys()){
    const v=String((f.known||{})[k.key]??"").trim();
    if(v!=="") lines.push(k.key+"="+v);
  }
  for(const l of (f.extraLines||[])) lines.push(l);
  return lines.length?lines.join("\n")+"\n":"";
}
function peFormFields(){
  // Rendered inputs win; keys hidden by the standard view survive via the
  // shadow copy, so toggling the view never silently drops a stored value.
  const known=Object.assign({}, _peKnownShadow);
  $("#peForm").querySelectorAll("[data-ck]").forEach(el=>{ known[el.dataset.ck]=el.value; });
  return {known:known, extraLines:_peExtra};
}
function peVisibleKeys(){
  const bt=peBackendType();
  if(bt!=="llama"){
    // Translation view: shared keys (model/ctx/template/extra args) keep
    // their values; the backend's own keys join; llama-only keys stay in
    // the conf, inert, until the binary type switches back.
    const shared=CONF_KEYS.filter(k=>k.shared || (bt==="vllm" && k.vllm));
    return shared.concat(BACKEND_KEYS[bt]||[]).filter(k=>_peFullForm || k.std);
  }
  const multi=peGpuValue().includes(",");
  return CONF_KEYS.filter(k=>_peFullForm || k.std || (multi && k.multi));
}
function peInertNote(){
  const bt=peBackendType();
  if(bt==="llama") return "";
  const visible=new Set(peVisibleKeys().map(k=>k.key));
  const n=Object.entries(_peKnownShadow)
    .filter(([k,v])=>v!=="" && peAllKeys().some(x=>x.key===k) && !visible.has(k)).length;
  return n ? `<div class="brow muted" style="font-size:12px">⚠ ${n} llama-only ${n===1?"key":"keys"} kept in the conf, inert under ${bt} — the launcher warns and skips ${n===1?"it":"them"}; switch the binary type back to reactivate.</div>` : "";
}
function peRenderForm(fields){
  _peKnownShadow=Object.assign({}, fields.known||{});
  $("#peForm").innerHTML=peInertNote()+peVisibleKeys().map(k=>{
    const v=String((fields.known||{})[k.key]??"");
    let input;
    if(k.type==="enum"){
      const opts=k.options.slice();
      if(v!=="" && !opts.includes(v)) opts.push(v);   // keep a non-standard stored value selectable
      input=`<select data-ck="${k.key}">`+opts.map(o=>
        `<option value="${esc(o)}" ${o===v?"selected":""}>${o===""?"—":esc(o)}</option>`).join("")+`</select>`;
    }else if(k.type==="number"){
      input=`<input data-ck="${k.key}" type="number" step="any" value="${esc(v)}" placeholder="${esc(k.def)}" style="width:120px">`;
    }else{
      input=`<input data-ck="${k.key}" value="${esc(v)}" placeholder="${esc(k.def)}" style="width:${k.type.startsWith("file-")?"340px":"300px"}">`;
      if(k.type.startsWith("file-"))
        input+=` <button type="button" data-browse="${k.key}" data-kind="${k.type}">browse…</button>`;
    }
    if(k.key==="EXTRA_ARGS")
      input+=` <button type="button" id="peBinHelpBtn" title="see the selected binary's --help for available flags">binary help</button>`;
    return `<div class="brow"><label title="${esc(k.key)}">${esc(k.label)}</label>${input}`+
      `<span class="muted" style="font-size:12px">${esc(k.help)}${k.def!==""?" (default "+esc(k.def)+")":""}</span></div>`;
  }).join("");
  $("#peForm").querySelectorAll("[data-browse]").forEach(b=>b.onclick=()=>{
    openModelPicker($("#peForm").querySelector(`[data-ck="${b.dataset.browse}"]`), b.dataset.kind);
  });
  const bh=$("#peBinHelpBtn");
  if(bh) bh.onclick=()=>{
    const b=$("#peBin").value || ((_presets.binaries||[])[0]||{}).name || "";
    if(!b){ note($("#peNote"),"no binaries registered — add one in Models → Servers",false); return; }
    showBinHelp(b);
  };
}
// One source of truth: the view you're leaving is serialized/parsed into the
// one you're entering, so toggling never loses edits.
function peShowAdvanced(on){
  _peAdvanced=on;
  if(on){ $("#peConf").value=fieldsToConf(peFormFields()); }
  else{ const f=confToFields($("#peConf").value); _peExtra=f.extraLines; peRenderForm(f); }
  $("#peForm").style.display=on?"none":"";
  $("#peFullToggle").style.display=on?"none":"";
  $("#peConfWrap").style.display=on?"":"none";
  $("#peAdvToggle").textContent=on?"structured form":"advanced (raw .conf)";
}
$("#peAdvToggle").onclick=()=>peShowAdvanced(!_peAdvanced);
$("#peFullToggle").onclick=()=>{
  _peFullForm=!_peFullForm;
  $("#peFullToggle").textContent=_peFullForm?"fewer options":"all launch options";
  peRenderForm(peFormFields());
};
function peLoadConf(text){ $("#peConf").value=text||""; _peAdvanced=true; _peFullForm=false;
  $("#peFullToggle").textContent="all launch options"; peShowAdvanced(false); }

// ---- models-dir inventory + file picker (GET /api/admin/models/tree) ----
let _modelTree=null;   // cached for the page load; Refresh re-fetches
async function loadModelTree(force){
  if(_modelTree && !force) return _modelTree;
  const r=await api("/api/admin/models/tree");
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#mfNote"),j.detail||"error",false); return null; }
  _modelTree=j; $("#mfDir").textContent=j.models_dir||""; return j;
}
function modelTreeHtml(j, exts, pickable){
  const rows=[];
  for(const e of (j.entries||[])){
    const depth=e.path.split("/").length-1, name=esc(e.path.split("/").pop());
    const pad=`style="padding-left:${depth*16}px"`;
    if(e.type==="dir"){ rows.push(`<div class="muted" ${pad}>${name}/</div>`); continue; }
    if(exts && !exts.some(x=>e.path.toLowerCase().endsWith(x))) continue;
    const users=(j.assigned||{})[e.path];
    const anno=users?` <span class="pill up" title="used by: ${esc(users.join(", "))}">★ ${esc(users.join(", "))}</span>`:"";
    const sz=` <span class="muted" style="font-size:11px">${fmtBytes(e.size)}</span>`;
    rows.push(pickable
      ? `<div ${pad}><a href="#" data-pick="${esc(e.path)}">${name}</a>${sz}${anno}</div>`
      : `<div ${pad}>${name}${sz}${anno}</div>`);
  }
  return rows.join("") || "<p class='muted' style='font-size:12px'>no matching files</p>";
}
let _mfSel=null, _mfSelDir=false, _mfShowAll=false, _mfExpanded=new Set();
// Dirs are derived from file paths, collapsed by default, pruned when
// nothing matching the filter lives beneath them.
function modelBrowseHtml(j){
  const root={dirs:{},files:{}};
  for(const e of (j.entries||[])){
    if(e.type!=="file") continue;
    const parts=e.path.split("/"); let node=root;
    for(let i=0;i<parts.length-1;i++)
      node=node.dirs[parts[i]]||(node.dirs[parts[i]]={dirs:{},files:{}});
    node.files[e.path]=e;
  }
  const match=p=>_mfShowAll||/\.(gguf|jinja|bin)$/i.test(p);
  const hasMatch=n=>Object.keys(n.files).some(match)||
    Object.values(n.dirs).some(hasMatch);
  const assigned=j.assigned||{};
  const aPaths=Object.keys(assigned);
  const dirAnno=path=>{
    const names=[...new Set(aPaths.filter(p=>p.startsWith(path+"/"))
      .flatMap(p=>assigned[p]))];
    return names.length?` <span class="pill up" title="contains model(s) used by: ${esc(names.join(", "))}">★</span>`:"";
  };
  const rows=[];
  const render=(node,prefix,depth)=>{
    for(const [name,sub] of Object.entries(node.dirs).sort((a,b)=>a[0].localeCompare(b[0]))){
      const path=prefix?prefix+"/"+name:name;
      if(!hasMatch(sub)) continue;
      const open=_mfExpanded.has(path);
      rows.push(`<div style="padding-left:${depth*16}px"><span data-dir="${esc(path)}" class="muted" style="cursor:pointer">${open?"▾":"▸"}</span> <span data-sel="${esc(path)}" data-gguf="0" data-dirrow="1" class="muted" style="cursor:pointer;border-radius:6px">${esc(name)}/</span>${dirAnno(path)}</div>`);
      if(open) render(sub,path,depth+1);
    }
    for(const e of Object.values(node.files).sort((a,b)=>a.path.localeCompare(b.path))){
      if(!match(e.path)) continue;
      const users=(j.assigned||{})[e.path];
      const anno=users?` <span class="pill up" title="used by: ${esc(users.join(", "))}">★ ${esc(users.join(", "))}</span>`:"";
      const sz=` <span class="muted" style="font-size:11px">${fmtBytes(e.size)}</span>`;
      const gguf=e.path.toLowerCase().endsWith(".gguf");
      rows.push(`<div data-sel="${esc(e.path)}" data-gguf="${gguf?1:0}" style="padding-left:${depth*16}px;cursor:pointer;border-radius:6px">${esc(e.path.split("/").pop())}${sz}${anno}</div>`);
    }
  };
  render(root,"",0);
  return rows.join("") || "<p class='muted' style='font-size:12px'>no matching files — try 'show all'</p>";
}
function mfRenderBrowse(){
  const j=_modelTree; if(!j) return;
  $("#mfBrowseList").innerHTML=modelBrowseHtml(j);
  $("#mfBrowseList").querySelectorAll("[data-dir]").forEach(d=>d.onclick=()=>{
    _mfExpanded.has(d.dataset.dir)?_mfExpanded.delete(d.dataset.dir):_mfExpanded.add(d.dataset.dir);
    mfRenderBrowse();
  });
  $("#mfBrowseList").querySelectorAll("[data-sel]").forEach(d=>{
    if(d.dataset.sel===_mfSel) d.style.background="var(--panel2)";
    d.onclick=()=>{
      _mfSel=d.dataset.sel; _mfSelDir=d.dataset.dirrow==="1";
      $("#mfBrowseList").querySelectorAll("[data-sel]").forEach(x=>x.style.background="");
      d.style.background="var(--panel2)";
      $("#mfMake").disabled=d.dataset.gguf!=="1";
      $("#mfDelete").disabled=false;
      $("#mfBrowseNote").textContent=_mfSel+(_mfSelDir?"/":"");
    };
  });
}
async function openModelBrowser(force){
  const j=await loadModelTree(force);
  if(!j) return;
  _mfSel=null; _mfSelDir=false; $("#mfMake").disabled=true; $("#mfDelete").disabled=true; $("#mfBrowseNote").textContent="";
  mfRenderBrowse();
  $("#mfBrowseOvl").hidden=false;
}
$("#mfOpen").onclick=()=>openModelBrowser(false);
$("#mfRefresh2").onclick=()=>openModelBrowser(true);
$("#mfAll").onclick=()=>{
  _mfShowAll=!_mfShowAll;
  $("#mfAll").textContent=_mfShowAll?"models only":"show all";
  mfRenderBrowse();
};
$("#mfBrowseClose").onclick=()=>{ $("#mfBrowseOvl").hidden=true; };
$("#mfDelete").onclick=async ()=>{
  if(!_mfSel) return;
  const path=_mfSel, isDir=_mfSelDir;
  if(!confirm(`Delete ${isDir?"folder and everything inside":"file"}:\n${path}\n\nThis cannot be undone.`)) return;
  const r=await api("/api/admin/models/file",{method:"DELETE",
    headers:{"content-type":"application/json"},body:JSON.stringify({path})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#mfBrowseNote"),j.detail||"delete failed",false); return; }
  _mfSel=null; _mfSelDir=false;
  $("#mfMake").disabled=true; $("#mfDelete").disabled=true;
  note($("#mfBrowseNote"),"deleted "+path,true);
  await loadModelTree(true); mfRenderBrowse();
};
$("#mfMake").onclick=()=>{
  if(!_mfSel) return;
  const path=_mfSel;
  $("#mfBrowseOvl").hidden=true;
  editPreset(null);
  const base=path.split("/").pop().replace(/\.gguf$/i,"")
    .replace(/[^A-Za-z0-9]+/g,"-").replace(/^-+|-+$/g,"").toLowerCase();
  $("#peName").value=base; $("#peAlias").value=base; $("#peServed").value=base;
  const entry=(_modelTree.entries||[]).find(e=>e.path===path);
  if(entry&&entry.size) $("#peVram").value=Math.ceil(entry.size/1073741824*1.1);
  peLoadConf("MODEL_PATH=$ORCH_MODELS/"+path+"\n");
  note($("#presetNote"),"draft for "+path.split("/").pop()+
    " loaded into the editor below — set device/port, review, save",true);
};
let _pickTarget=null;
async function openModelPicker(input, kind){
  const j=await loadModelTree(false);
  if(!j) return;
  _pickTarget=input;
  $("#mfPickTitle").textContent=kind==="file-jinja"?"Pick a chat template":"Pick a model file (.gguf / whisper .bin)";
  $("#mfPickList").innerHTML=modelTreeHtml(j, kind==="file-jinja"?[".jinja",".gguf"]:[".gguf",".bin"], true);
  $("#mfPickList").querySelectorAll("[data-pick]").forEach(a=>a.onclick=e=>{
    e.preventDefault();
    _pickTarget.value="$ORCH_MODELS/"+a.dataset.pick;
    $("#mfPickOvl").hidden=true;
  });
  $("#mfPickOvl").hidden=false;
}
$("#mfPickCancel").onclick=()=>{ $("#mfPickOvl").hidden=true; };

// ---- binary --help viewer (GET /api/admin/binaries/{name}/help) ----
async function showBinHelp(name){
  $("#binHelpTitle").textContent=name+" --help";
  $("#binHelpOut").textContent="running…";
  $("#binHelpOvl").hidden=false;
  const r=await api("/api/admin/binaries/"+encodeURIComponent(name)+"/help");
  const j=await r.json().catch(()=>({}));
  $("#binHelpOut").textContent=r.ok?(j.help||"(no output)"):(j.detail||"error");
}
$("#binHelpClose").onclick=()=>{ $("#binHelpOvl").hidden=true; };

// Themed multiselect (device picker, strengths): a button with a summary
// label + a checkbox popover. Outside click closes; one widget open at a time.
document.addEventListener("click",e=>{
  document.querySelectorAll(".msel.open").forEach(m=>{ if(!m.contains(e.target)) m.classList.remove("open"); });
});
function mselSummary(id, noneLabel){
  const v=mselValue(id).filter(x=>x!=="");   // "" = the "none" entry (CPU)
  document.querySelector("#"+id+" .msel-lab").textContent=
    v.length?_mselLab(id,v).join(" + "):noneLabel;
}
const _mselItems={};
function _mselLab(id, vals){
  const items=_mselItems[id]||[];
  return vals.map(v=>{ const it=items.find(i=>i.v===v); return it?(it.s||it.l):v; });
}
function mselRender(id, items, sel, noneLabel, onchange){
  const el=$("#"+id); sel=sel||[];
  _mselItems[id]=items;
  const lab=sel.filter(x=>x!=="");   // "" = the "none" entry (CPU) — checked, but not labelled
  el.innerHTML=`<button type="button"><span class="msel-lab">${esc(lab.length?_mselLab(id,lab).join(" + "):noneLabel)}</span></button>`+
    `<div class="mpop">${items.map(it=>`<label${it.title?` title="${esc(it.title)}"`:""}><input type="checkbox" data-v="${esc(it.v)}" ${sel.includes(it.v)?"checked":""}> ${esc(it.l)}</label>`).join("")}</div>`;
  el.classList.remove("open");
  el.querySelector("button").onclick=ev=>{ ev.stopPropagation();
    const was=el.classList.contains("open");
    document.querySelectorAll(".msel.open").forEach(m=>m.classList.remove("open"));
    if(!was) el.classList.add("open"); };
  el.querySelectorAll("input").forEach(b=>b.onchange=()=>{ if(onchange) onchange(b); mselSummary(id,noneLabel); });
}
function mselSet(id, vals){
  document.querySelectorAll("#"+id+" input[data-v]").forEach(b=>b.checked=vals.includes(b.dataset.v));
}
function mselValue(id){
  return [...document.querySelectorAll("#"+id+" input[data-v]:checked")].map(b=>b.dataset.v);
}

// Device picker: any subset of the topology GPUs, or CPU (exclusive).
function peGpuSet(cur){
  const sel=String(cur||"").split(",").filter(Boolean);
  const items=(_presets.gpus||[]).map(g=>({v:String(g.id), s:"GPU "+g.id,
    l:"GPU "+g.id+(g.label?" — "+g.label:"")+(g.vram_gib?" · "+g.vram_gib+" GiB":"")}));
  items.push({v:"", s:"CPU", l:"CPU"});
  mselRender("peGpuMsel", items, sel.length?sel:[""], "CPU", b=>{
    if(b.dataset.v===""&&b.checked) mselSet("peGpuMsel",[""]);
    else if(b.dataset.v!==""&&b.checked){
      const cpu=document.querySelector('#peGpuMsel input[data-v=""]');
      if(cpu) cpu.checked=false;
    }
    // multi-GPU selection pulls SPLIT_MODE/TENSOR_SPLIT into the standard form
    if(!_peAdvanced) peRenderForm(peFormFields());
  });
}
function peGpuValue(){
  const ids=mselValue("peGpuMsel").filter(v=>v!=="");
  ids.sort((a,b)=>parseInt(a,10)-parseInt(b,10));
  return ids.join(",");
}
let _peTarget=null;   // null = creating a new preset
function peRemoteSync(){
  const on=$("#peRemote").checked;
  $("#peRemoteHostRow").style.display=on?"":"none";
  $("#peKeyRow").style.display=on?"":"none";
  $("#peBackendRow").style.display=on?"":"none";
  $("#peCapsRow").style.display=on?"":"none";
  ["#peGpuRow","#peBinRow","#peVramRow","#peConfLabel"]
    .forEach(s=>$(s).style.display=on?"none":"");
  $("#peForm").style.display=(on||_peAdvanced)?"none":"";
  $("#peConfWrap").style.display=(!on&&_peAdvanced)?"":"none";
  // A URL endpoint that carries its own port owns it — lock the port field.
  const m=/^https?:\/\/[^/:]+:(\d+)/i.exec($("#peRemoteHost").value.trim());
  const portEl=$("#pePort");
  portEl.disabled=!!(on&&m);
  portEl.title=portEl.disabled?"the endpoint URL carries the port":"";
  if(portEl.disabled) portEl.value=m[1];
}
$("#peRemote").onchange=peRemoteSync;
$("#peRemoteHost").oninput=peRemoteSync;
function editPreset(name, duplicate){
  const p=name?_presets.presets.find(x=>x.name===name):null;
  _peTarget=(p&&!duplicate)?p.name:null;
  let nn="";   // duplicate gets a free name: <name>-copy, -copy2, ...
  if(p&&duplicate){ nn=p.name+"-copy"; let i=2;
    while(_presets.presets.some(x=>x.name===nn)) nn=p.name+"-copy"+(i++); }
  $("#presetEditTitle").textContent=p?(duplicate?("Duplicate preset: "+p.name):"Edit preset: "+p.name):"New preset";
  $("#peName").value=p?(duplicate?nn:p.name):""; $("#peName").disabled=!!(p&&!duplicate);
  $("#peRole").value=(p&&p.role)||""; $("#peAlias").value=(p&&p.alias)||"";
  $("#pePort").value=p&&p.port!=null?p.port:"";
  $("#peRemote").checked=!!(p&&p.remote_host);
  $("#peRemoteHost").value=(p&&p.remote_host)||"";
  $("#peApiKeyEnv").value=(p&&p.api_key_env)||"";
  $("#peBackend").value=(p&&p.backend)||"llama";
  const _caps=(p&&p.caps)||{};
  $("#peCapVision").value=_caps.vision==null?"":(_caps.vision?"1":"0");
  $("#peCapThinking").value=_caps.thinking==null?"":(_caps.thinking?"1":"0");
  peGpuSet(p?p.gpu||"":"");
  const curBin=p?p.binary||"":"";
  const _defBin=(_presets.default_binary||{}).path||"LLAMA_BIN";
  $("#peBin").innerHTML=[{v:"",l:`(default — ${_defBin})`}].concat(
    (_presets.binaries||[]).map(b=>({v:b.name,l:b.name+((b.type||"llama")!=="llama"?" · "+b.type:"")+(b.exists===false?" · missing":"")})))
    .map(o=>`<option value="${esc(o.v)}" ${o.v===curBin?"selected":""}>${esc(o.l)}</option>`).join("");
  // Switching the binary IS the backend translation: re-render the conf
  // fields for the new launch backend (values ride along via the shadow).
  $("#peBin").onchange=()=>{ if(!_peAdvanced) peRenderForm(peFormFields()); };
  $("#peServed").value=(p&&p.served_id)||""; $("#peVram").value=p&&p.vram_gib!=null?p.vram_gib:"";
  const _mm=(p&&p.measured)||{};
  $("#peVramMeasured").textContent=_mm.total_vram_gib!=null
    ? `measured ${_mm.total_vram_gib} GiB${p.measured_stale?" (stale — ctx/GPU/binary changed; re-measure)":""}`:"";
  peStrengthsRender((p&&p.strengths)||[]);
  $("#peArchived").checked=!!(p&&p.archived);
  const _prof=(p&&p.profile)||{};
  $("#peProfile").value=Object.keys(_prof).length?JSON.stringify(_prof,null,1):"";
  peLoadConf((p&&p.conf)||"");
  peRemoteSync();
  $("#presetEditSec").style.display="block";
  $("#presetEditSec").scrollIntoView({behavior:"smooth",block:"nearest"});
}
// Preset strengths: multiselect over the registry (Admin → Models →
// Strengths manages it) — allround (built-in catch-all) plus registered
// tags, plus any unregistered tag the preset already carries (kept, marked,
// never dropped). Meanings ride the option tooltips.
function peStrengthsRender(sel){
  sel=sel||[];
  const reg=(_presets&&_presets.strengths_registry)||{};
  const known=["allround",...Object.keys(reg).sort()];
  const extra=sel.filter(t=>!known.includes(t));
  const items=known.concat(extra).map(t=>({v:t, s:t,
    l:t+(extra.includes(t)?" (unregistered)":""),
    title:reg[t]||(t==="allround"?"built-in catch-all fallback":"")}));
  mselRender("peStrengthsMsel", items, sel, "none");
}
function peStrengthsValue(){ return mselValue("peStrengthsMsel"); }
$("#newPreset").onclick=()=>editPreset(null);
$("#peCancel").onclick=()=>{ $("#presetEditSec").style.display="none"; };
$("#peSave").onclick=async()=>{
  const caps={}; const _cv=$("#peCapVision").value, _ct=$("#peCapThinking").value;
  if(_cv!=="") caps.vision=_cv==="1"; if(_ct!=="") caps.thinking=_ct==="1";
  let profile={};
  const _profRaw=$("#peProfile").value.trim();
  if(_profRaw){
    try{ profile=JSON.parse(_profRaw); }
    catch(e){ note($("#peNote"),"harness profile: invalid JSON — "+e.message,false); return; }
    if(typeof profile!=="object"||profile===null||Array.isArray(profile)){
      note($("#peNote"),"harness profile must be a JSON object",false); return; }
  }
  const body={ name:$("#peName").value.trim(), role:$("#peRole").value,
    alias:$("#peAlias").value.trim()||null,
    port:$("#pePort").value===""?null:parseInt($("#pePort").value,10),
    remote_host:$("#peRemote").checked?$("#peRemoteHost").value.trim():"",
    api_key_env:$("#peRemote").checked?$("#peApiKeyEnv").value.trim():"",
    backend:$("#peRemote").checked?$("#peBackend").value:"",
    caps:caps, profile:profile,
    gpu:peGpuValue(), binary:$("#peBin").value.trim(),
    served_id:$("#peServed").value.trim()||null,
    vram_gib:$("#peVram").value===""?null:parseFloat($("#peVram").value),
    strengths:peStrengthsValue(),
    archived:$("#peArchived").checked,
    conf:_peAdvanced ? $("#peConf").value : fieldsToConf(peFormFields()) };
  const r=_peTarget
    ? await api("/api/admin/presets/"+encodeURIComponent(_peTarget),{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(body)})
    : await api("/api/admin/presets",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
  const j=await r.json();
  if(!r.ok){ note($("#peNote"),j.detail||"error",false); return; }
  _presets=j; renderPresetRows(); $("#presetEditSec").style.display="none";
  _modelTree=null;   // ★ annotations may have changed
  note($("#presetNote"),"saved",true);
};
// ---- per-preset measure button (model.measure as an admin background job) ----
let _measurePoll=null;
async function presetMeasure(name){
  if(!await dlgConfirm(`Measure ${name}? Hibernates ALL models (brain included) for a few minutes — chat is down meanwhile, everything comes back after.`,
                       {yes:"measure"})) return;
  const r=await api(`/api/admin/presets/${encodeURIComponent(name)}/measure`,{method:"POST"});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#presetNote"),j.detail||"error",false); return; }
  note($("#presetNote"),`measuring ${name} — box hibernated, chat down…`,true);
  if(_measurePoll) clearInterval(_measurePoll);
  _measurePoll=setInterval(async()=>{
    let s; try{ s=await (await api("/api/admin/presets/measure-status")).json(); }catch{ return; }
    if(s.state!=="running"){
      clearInterval(_measurePoll); _measurePoll=null;
      const m=(s.result&&s.result.measured)||s.result||{};
      note($("#presetNote"),
        s.state==="done"
          ? `measured ${s.name}: VRAM ${m.total_vram_gib??"?"} GiB · RAM ${m.ram_gib??"?"} GiB`
          : `measure ${s.name||""} failed: ${s.error||"unknown"}`,
        s.state==="done");
      loadPresets();
    }
  },5000);
}
$("#refreshPresets").onclick=loadPresets;

// ---- prompt schedules ----
let _schedules=null, _schedUsers=null;
const SCHED_WHEN={once:{lbl:"at",ph:"e.g. +30m or 2026-10-20T09:00"},
  every:{lbl:"every",ph:"e.g. 6h"},cron:{lbl:"cron",ph:"e.g. 30 7 * * 1-5"}};
function schedWhenSync(){
  const m=SCHED_WHEN[$("#schedKind").value]||SCHED_WHEN.once;
  $("#schedWhenLbl").textContent=m.lbl; $("#schedWhen").placeholder=m.ph;
}
function _fmtIso(ts){
  if(!ts) return "—";
  const d=new Date(ts*1000), p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes());
}
function schedHuman(s){
  if(s.kind==="cron") return "cron "+(s.cron||"");
  if(s.kind==="every"){
    const n=s.every_s||0;
    for(const [sec,u] of [[604800,"w"],[86400,"d"],[3600,"h"],[60,"m"]])
      if(n&&n%sec===0) return "every "+(n/sec)+u;
    return "every "+n+"s";
  }
  return "once "+_fmtIso(s.next_fire);
}
async function loadSchedules(){
  const j=await (await api("/api/admin/schedules")).json();
  _schedules=j.schedules||[];
  $("#schedRows").innerHTML=_schedules.map(s=>{
    const pr=String(s.prompt||"");
    const last=s.last_status
      ?"<span class='pill st "+(s.last_status==="ok"?"up":"down")+"'>"+esc(s.last_status)+"</span>"
      :"<span class='pill'>—</span>";
    return "<tr>"+
      "<td>"+esc(s.owner)+"</td>"+
      "<td title='"+esc(pr)+"'>"+esc(pr.length>60?pr.slice(0,60)+"…":pr)+"</td>"+
      "<td>"+esc(schedHuman(s))+"</td>"+
      "<td class='muted'>"+esc(s.project_name||"—")+"</td>"+
      "<td><input type='checkbox' class='schedTgl' data-sid='"+esc(s.id)+"'"+(s.enabled?" checked":"")+"></td>"+
      "<td class='muted'>"+_fmtTs(s.next_fire)+"</td>"+
      "<td>"+last+"</td>"+
      "<td>"+(s.fire_count||0)+"</td>"+
      "<td style='text-align:right;white-space:nowrap'>"+
        "<button class='schedHist' data-sid='"+esc(s.id)+"'>history</button> "+
        "<button class='danger schedDel' data-sid='"+esc(s.id)+"'>delete</button></td></tr>";
  }).join("")||"<tr><td colspan='9' class='muted'>no schedules</td></tr>";
  $("#schedRows").querySelectorAll(".schedTgl").forEach(t=>t.onchange=async()=>{
    await api("/api/admin/schedules/"+t.dataset.sid,{method:"PUT",
      headers:{"content-type":"application/json"},body:JSON.stringify({enabled:t.checked})});
  });
  $("#schedRows").querySelectorAll(".schedDel").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete this schedule?",{yes:"delete"})) return;
    await api("/api/admin/schedules/"+b.dataset.sid,{method:"DELETE"});
    loadSchedules();
  });
  $("#schedRows").querySelectorAll(".schedHist").forEach(b=>b.onclick=()=>
    loadSchedHistory(b.dataset.sid,b.closest("tr")));
}
async function loadSchedHistory(id,tr){
  const wasOpen=tr.nextElementSibling&&tr.nextElementSibling.classList.contains("schedHistRow");
  document.querySelectorAll(".schedHistRow").forEach(r=>r.remove());
  if(wasOpen) return;   // clicking an open row's button just closes it
  const j=await (await api("/api/admin/schedules/"+id+"/history")).json();
  const hr=document.createElement("tr"); hr.className="schedHistRow";
  hr.innerHTML="<td colspan='9'>"+((j.runs||[]).map(r=>
    "<div class='schedRun'>"+
    "<span class='pill st "+(r.status==="ok"?"up":"down")+"'>"+esc(r.status||"?")+"</span> "+
    "<span class='muted'>"+_fmtTs(r.fired_at)+"</span>"+
    "<span class='muted'> · "+(r.duration_s!=null?Number(r.duration_s).toFixed(1)+"s":"—")+"</span>"+
    (r.tokens?"<span class='muted'> · "+r.tokens+" tok</span>":"")+
    "<div class='muted' style='font-size:12px'>"+esc(String(r.excerpt||"").slice(0,200))+"</div></div>"
  ).join("")||"<span class='muted'>no runs yet</span>")+"</td>";
  tr.after(hr);
}
async function loadSchedUsers(){
  const d=await (await api("/api/admin/users")).json();
  _schedUsers=d.users||[];
  $("#schedOwner").innerHTML=_schedUsers.map(u=>
    "<option value='"+esc(u.username)+"'>"+esc(u.username)+"</option>").join("");
  loadSchedProjects();
}
async function loadSchedProjects(){
  const sel=$("#schedProject"), owner=$("#schedOwner").value;
  sel.innerHTML="<option value=''>— none —</option>";
  if(!owner) return;
  const j=await (await api("/api/admin/schedules/projects?owner="+encodeURIComponent(owner))).json();
  sel.innerHTML+=(j.projects||[]).map(p=>
    "<option value='"+esc(p.id)+"'>"+esc(p.name)+"</option>").join("");
}
$("#refreshSchedules").onclick=loadSchedules;
$("#schedOwner").onchange=loadSchedProjects;
$("#schedKind").onchange=schedWhenSync;
$("#schedAdd").onclick=async()=>{
  const kind=$("#schedKind").value, sched=$("#schedWhen").value.trim();
  const body={owner:$("#schedOwner").value,prompt:$("#schedPrompt").value.trim(),
    kind:kind,enabled:$("#schedEnabled").checked};
  if(kind==="once") body.when=sched; else if(kind==="every") body.every=sched; else body.cron=sched;
  if($("#schedProject").value) body.project_id=$("#schedProject").value;
  if(!body.owner||!body.prompt||!sched){ note($("#schedNote"),"owner, prompt and schedule required",false); return; }
  const r=await api("/api/admin/schedules",{method:"POST",
    headers:{"content-type":"application/json"},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#schedNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
  note($("#schedNote"),"added",true);
  $("#schedPrompt").value=""; $("#schedWhen").value="";
  loadSchedules();
};

// ---- two-level tabs -------------------------------------------------------
// Seven top tabs group the old 16 flat tabs; each top tab has a subtab pill row
// (or a single page). Pane/subpane element ids are the historical ones —
// render code addresses content by id — so TABMAP is the single source of
// truth for which id lives under which tab.
const TABMAP={
  status:{pane:"pane-status",def:"overview",subs:{overview:"tab-status",usage:"tab-usage",runs:"sub-status-runs"}},
  models:{pane:"pane-models",def:"servers",subs:{servers:"tab-processes",presets:"tab-presets",files:"sub-models-files",cloud:"sub-models-cloud"}},
  harness:{pane:"pane-harness",def:"runtime",subs:{runtime:"tab-config",prompts:"tab-prompt",tools:"tab-tools",mcp:"sub-harness-integrations",data:"sub-harness-data"}},
  studio:{pane:"pane-studio",def:"content",subs:{content:"tab-studio",connectors:"tab-connectors",plugins:"tab-plugins"}},
  eval:{pane:"pane-eval",def:"eval",subs:{eval:"tab-eval",results:"sub-studio-results",details:"sub-studio-details",proposals:"sub-studio-proposals",experiments:"sub-studio-experiments",benchmark:"sub-studio-benchmark"}},
  flags:{pane:"tab-flags"},
  users:{pane:"tab-access"},
  schedules:{pane:"tab-schedules"},
};
// Old flat-tab hashes → new <top>/<sub> locations (backward compat).
const HASH_MAP={status:"status/overview",usage:"status/usage",processes:"models/servers",
  presets:"models/presets",prompt:"harness/prompts",config:"harness/runtime",
  tools:"harness/tools",connectors:"studio/connectors",mcp:"harness/mcp",
  integrations:"harness/mcp",rag:"harness/data",studio:"studio/content",plugins:"studio/plugins",
  eval:"eval/eval",flags:"flags",access:"users",backup:"harness/data"};
function goTab(top,sub){
  if(!TABMAP[top]){ top="status"; sub=null; }
  const t=TABMAP[top];
  if(t.subs){ if(!sub||!t.subs[sub]) sub=t.def; } else sub=null;
  document.querySelectorAll("#tabs .tab").forEach(x=>x.classList.toggle("active",x.dataset.tab===top));
  document.querySelectorAll(".tabpane").forEach(p=>p.classList.toggle("active",p.id===t.pane));
  document.querySelectorAll(".subtabs").forEach(n=>{ n.hidden=n.dataset.for!==top; });
  document.querySelectorAll(".subpane").forEach(p=>
    p.classList.toggle("active",!!sub&&t.subs[sub]===p.id));
  document.querySelectorAll(".subtab").forEach(x=>x.classList.toggle("active",
    x.closest(".subtabs").dataset.for===top&&x.dataset.sub===sub));
  // Keep the location in the URL hash so a browser refresh returns here (the
  // hash never reaches the server; replaceState avoids history spam).
  history.replaceState(null,"","#"+top+(sub?"/"+sub:""));
  tabVisible(top,sub);
  tabbarFades();
}
// Lazy-load data when the subtab containing it becomes visible — the same
// once-per-load guards the flat tabs used.
function tabVisible(top,sub){
  if(top==="status"&&sub==="usage"&&!_usage) loadUsage();
  // GPUs/binaries live on Models→servers, catalog/slots on Models→presets —
  // both render from the same _presets payload.
  if(top==="models"&&(sub==="servers"||sub==="presets")&&!_presets) loadPresets();
  if(top==="models"&&sub==="presets"&&!_strengths) loadStrengths();
  if(top==="models"&&sub==="files"){ hfRefreshJobs(); loadModelTree(false); }
  if(top==="models"&&sub==="cloud"&&!_cloud) loadCloud();
  if(top==="models"&&sub==="servers") startProcPolling(); else stopProcPolling();
  if(top==="harness"&&sub==="runtime"&&!_cfgFlat.length) loadConfig();
  if(top==="harness"&&sub==="tools"){ if(!_toolData.length) loadTools().then(loadToolOv); else loadToolOv(); }
  if(top==="harness"&&sub==="mcp"&&!_mcpLoaded) loadMcp();
  if(top==="studio"&&sub==="content"&&!_studio) loadStudio();
  if(top==="studio"&&sub==="connectors"&&!_conns) loadConnectors();
  if(top==="eval"&&sub==="eval"){ if(!_eval) loadEval(); else evalHandover(); }
  if(top==="eval"&&sub==="results"){ loadEvalResults(); loadEvalStatsBrains(); loadEvalStats(); }
  if(top==="eval"&&sub==="details") loadEvalStats();
  if(top==="eval"&&sub==="proposals"){ loadEvalProposals(); loadBrainOverlays(); }
  if(top==="eval"&&sub==="experiments"){
    _evExpVisible=true;
    refreshEvalExperiments();
  }else stopEvalExperimentPolling();
  if(top==="eval"&&sub==="benchmark") loadBench();
  else if(_evBmPoll){ clearInterval(_evBmPoll); _evBmPoll=null; }
  if(top==="schedules"){
    if(!_schedules) loadSchedules();
    if(!_schedUsers) loadSchedUsers();
  }
}
document.querySelectorAll("#tabs .tab").forEach(b=>b.onclick=()=>goTab(b.dataset.tab,null));
document.querySelectorAll(".subtab").forEach(b=>b.onclick=()=>
  goTab(b.closest(".subtabs").dataset.for,b.dataset.sub));
// Right-edge fade on the tab bars only while they actually overflow.
function tabbarFades(){
  document.querySelectorAll("#tabs,.subtabs").forEach(n=>
    n.classList.toggle("scrollable",!n.hidden&&n.scrollWidth>n.clientWidth+1));
}
window.addEventListener("resize",tabbarFades);

// ---- usage subtab: per-tool / per-skill call counts from the trace log ----
let _usage=null;
function _fmtTs(ts){ return ts? new Date(ts*1000).toLocaleString() : "—"; }
async function loadUsage(){
  const d=await (await api("/api/admin/usage/tools")).json();
  _usage=d;
  $("#usageToolRows").innerHTML=(d.tools||[]).map(t=>
    "<tr><td>"+esc(t.name)+(t.registered===false?" <span class='muted'>(removed)</span>":"")+"</td>"+
    "<td>"+(t.uses||'<span class="muted">0</span>')+"</td>"+
    "<td class='muted'>"+_fmtTs(t.last_used)+"</td></tr>"
  ).join("") || '<tr><td colspan="3" class="muted">No tool calls recorded yet.</td></tr>';
  $("#usageSkillRows").innerHTML=(d.skills||[]).map(s=>
    "<tr><td>"+esc(s.name)+"</td>"+
    "<td class='muted'>"+esc(s.origin||"")+"</td>"+
    "<td>"+(s.uses||'<span class="muted">0</span>')+"</td>"+
    "<td class='muted'>"+_fmtTs(s.last_used)+"</td></tr>"
  ).join("") || '<tr><td colspan="4" class="muted">No skill loads recorded yet.</td></tr>';
}
$("#refreshUsageTools").onclick=loadUsage;
$("#refreshUsageSkills").onclick=loadUsage;

// ---- backup / restore ----
$("#bkDownload").onclick=()=>{
  // Plain navigation: the session cookie authenticates and the browser saves
  // the .tar.gz under the server-sent filename.
  window.location="/api/admin/backup";
};
$("#bkRestore").onclick=async()=>{
  const f=$("#bkFile").files[0], n=$("#bkRestoreNote");
  if(!f){ note(n,"choose a .tar.gz backup first",false); return; }
  if(!await dlgConfirm("Restore from backup?\n\nThis OVERWRITES all current data (chats, users, presets, wiki, uploads, projects) and requires a service restart afterwards.", {yes:"restore"})) return;
  const fd=new FormData(); fd.append("file",f);
  const r=await api("/api/admin/restore",{method:"POST",body:fd});
  const j=await r.json().catch(()=>({}));
  if(r.ok&&j.restart_required){ n.className="note ok"; n.textContent="restored — restart the service now for it to take effect"; }
  else note(n,j.detail||"restore failed",false);
};

const BUDGET_FIELDS=[
  {on:"#dIterOn",inp:"#dIter",key:"max_iterations",def:60,isInt:true},
  {on:"#dWallOn",inp:"#dWall",key:"max_wall_clock_s",def:600,isInt:true},
  {on:"#dCostOn",inp:"#dCost",key:"max_cost_usd",def:1.0,isInt:false},
  {on:"#dTokOn",inp:"#dTok",key:"max_total_tokens",def:1000000,isInt:true},
];
function budgetSyncRow(f){
  const on=$(f.on).checked, inp=$(f.inp);
  inp.disabled=!on;
  if(on && !inp.value) inp.value=f.def;        // enabling prefills a sane default
  if(!on){ inp.value=""; inp.placeholder="unlimited"; }
}
BUDGET_FIELDS.forEach(f=>{ $(f.on).onchange=()=>budgetSyncRow(f); });
async function loadBudget(){
  const d=await (await api("/api/admin/budget-defaults")).json();
  for(const f of BUDGET_FIELDS){
    const v=d[f.key], on=typeof v==="number"&&v>0;   // 0 = unlimited = toggle off
    $(f.on).checked=on;
    const inp=$(f.inp);
    inp.disabled=!on;
    inp.value=on?v:"";
    inp.placeholder=on?"":"unlimited";
  }
}
$("#saveBudget").onclick=async()=>{
  const body={};
  for(const f of BUDGET_FIELDS){
    if(!$(f.on).checked){ body[f.key]=0; continue; }
    const n=f.isInt?parseInt($(f.inp).value,10):parseFloat($(f.inp).value);
    body[f.key]=Number.isFinite(n)&&n>0?n:f.def;
  }
  const r=await api("/api/admin/budget-defaults",{method:"PUT",
    headers:{"content-type":"application/json"}, body:JSON.stringify(body)});
  if(r.ok){ note($("#budgetNote"),"saved",true); loadBudget(); }
  else { const e=await r.json().catch(()=>({})); note($("#budgetNote"),e.detail||"error",false); }
};
$("#reloadBudget").onclick=loadBudget;

loadPrompt(); loadWorkerPrompts("base"); loadStatus(); loadUpdates(); loadHardware(); loadLogs(); loadUsers(); loadRag(); loadBudget(); loadUsage(); loadUsagePerUser(); loadPlugins();

// ---- managed processes ----
let _procTimer = null;
let _procExpanded = new Set();
async function loadProcs(){
  try {
    const d = await (await api("/api/admin/processes")).json();
    renderProcs(d);
    // Load logs for each process
    for(const name of Object.keys(d)){
      const ld = await (await api("/api/admin/processes/"+name+"/logs?lines=200")).json();
      const logEl = document.querySelector("#proc-log-"+name+" pre");
      if(logEl){
        const wasAtBottom = logEl.parentElement.scrollTop + logEl.parentElement.clientHeight >= logEl.parentElement.scrollHeight - 20;
        logEl.textContent = (ld.lines||[]).join("\n");
        if(wasAtBottom) logEl.parentElement.scrollTop = logEl.parentElement.scrollHeight;
      }
    }
  } catch(e){ console.error("loadProcs:", e); }
}
function renderProcs(data){
  const container = $("#procCards"); if(!container) return;
  const names = Object.keys(data);
  if(!names.length){ container.innerHTML="<p class='muted'>No managed processes configured.</p>"; return; }
  // Only rebuild DOM if structure changed
  if(container.children.length !== names.length){
    container.innerHTML = "";
    for(const name of names){
      const card = document.createElement("div"); card.className="proc-card"; card.id="proc-card-"+name;
      // name via textContent + handler closures, not template interpolation:
      // inline onclick="${name}" would re-decode HTML entities into JS (audit B8).
      card.innerHTML = `
        <div class="proc-head">
          <span class="proc-name"></span>
          <span class="proc-status" id="proc-st-${name}"></span>
        </div>
        <div class="proc-meta" id="proc-meta-${name}"></div>
        <div class="proc-btns">
          <button class="mini" data-act="restart">restart</button>
          <button class="mini" data-act="stop">stop</button>
          <button class="mini" data-act="start">start</button>
          <button class="proc-log-toggle" data-act="log">▸ logs</button>
          <span class="proc-stats" id="proc-stats-${name}"></span>
        </div>
        <div class="proc-log${_procExpanded.has(name)?' expanded':''}" id="proc-log-${name}" ${_procExpanded.has(name)?'':'hidden'}>
          <pre></pre>
        </div>`;
      card.querySelector(".proc-name").textContent = name;
      card.querySelector('[data-act="restart"]').onclick=()=>procAction(name,"restart");
      card.querySelector('[data-act="stop"]').onclick=()=>procAction(name,"stop");
      card.querySelector('[data-act="start"]').onclick=()=>procAction(name,"start");
      card.querySelector('[data-act="log"]').onclick=()=>toggleProcLog(name);
      container.appendChild(card);
    }
  }
  // Update status
  for(const [name, s] of Object.entries(data)){
    const st = document.querySelector("#proc-st-"+name);
    if(st){
      const label = s.disabled ? "disabled (slot empty)"
        : s.remote ? "remote — probe only ("+s.remote+")"
        : s.swap ? "⇄ "+(s.swap.model||s.swap.server)+" (swapped in)"
        : s.stopping ? "stopping" : s.alive ? "alive" : "dead";
      const pid = s.swap ? s.swap.pid : s.pid;
      st.textContent = label + (!s.disabled && !s.remote && pid ? " (pid "+pid+")" : "");
      st.className = "proc-status " + (s.disabled || s.remote ? "stopping"
        : s.swap ? "swap" : s.stopping ? "stopping" : s.alive ? "alive" : "dead");
    }
    const meta = document.querySelector("#proc-meta-"+name);
    if(meta){
      let parts = [];
      if(s.swap) parts.push("strength swap via model.use — stop frees the slot · start restores the boot preset");
      if(s.uptime_s != null){ const m=Math.floor(s.uptime_s/60), sec=Math.floor(s.uptime_s%60); parts.push("uptime: "+m+"m "+sec+"s"); }
      if(s.restarts) parts.push("restarts: "+s.restarts);
      if(s.exit_code != null) parts.push("exit code: "+s.exit_code);
      if(s.command) parts.push("cmd: "+s.command.substring(0,80)+(s.command.length>80?"…":""));
      meta.textContent = parts.join("  ·  ");
    }
    // Lifetime stats right of the logs button: MTP acceptance (log-parsed),
    // avg speeds + cumulative tokens (server /metrics since boot).
    const ps = document.querySelector("#proc-stats-"+name);
    if(ps){
      const st = s.stats || {};
      const p = [];
      if(st.mtp_acceptance != null) p.push("mtp "+Math.round(st.mtp_acceptance*100)+"% ("+st.mtp_drafted+" drafted)");
      if(st.gen_tps_avg != null) p.push("gen "+st.gen_tps_avg+" tok/s");
      if(st.prompt_tps_avg != null) p.push("pp "+st.prompt_tps_avg+" tok/s");
      if(st.gen_tps_now) p.push("now "+st.gen_tps_now+" tok/s");
      if(st.tokens_generated != null) p.push(st.tokens_generated+" tok");
      ps.textContent = p.join("  ·  ");
      ps.title = st.mtp_accepted != null
        ? ("MTP acceptance over the process log buffer: "+st.mtp_accepted+"/"+st.mtp_drafted+
           " draft tokens accepted in "+st.mtp_events+" events. Speeds/token counts are "+
           "cumulative since process start (llama-server /metrics).")
        : "Speeds/token counts are cumulative since process start (llama-server /metrics).";
    }
  }
}
function toggleProcLog(name){
  const el = document.querySelector("#proc-log-"+name);
  const btn = el?.previousElementSibling?.querySelector(".proc-log-toggle");
  if(!el) return;
  if(el.hidden){ el.hidden=false; _procExpanded.add(name); if(btn) btn.textContent="▾ logs";
    el.scrollTop = el.scrollHeight;
  } else { el.hidden=true; _procExpanded.delete(name); if(btn) btn.textContent="▸ logs"; }
}
async function procAction(name, action){
  // FE-4: one stray click on stop/restart kills the model the household is
  // actively using (every run in flight fails) — confirm, name the slot,
  // and don't swallow a failed response.
  if((action==="stop"||action==="restart") &&
     !await dlgConfirm((action==="stop"?"Stop":"Restart")+" model slot '"+name+
                       "'? Runs in flight will fail.", {yes:action})) return;
  let r=null;
  try{ r=await api("/api/admin/processes/"+name+"/"+action, {method:"POST"}); }catch(e){}
  if(!r || !r.ok){ const n=$("#procNote"); if(n){ n.className="note err";
    n.textContent=action+" "+name+" failed"+(r?" ("+r.status+")":""); } }
  setTimeout(loadProcs, 500);
}
function startProcPolling(){
  if(_procTimer) return;
  loadProcs();
  _procTimer = setInterval(loadProcs, 3000);
}
function stopProcPolling(){
  if(_procTimer){ clearInterval(_procTimer); _procTimer=null; }
}
$("#refreshProcs").onclick=loadProcs;

// ---- config editor ----
let _cfgFlat={}, _cfgOverrides={}, _cfgEdits={}, _cfgHelp={};
// One-line help per key: exact dotpath first, then fnmatch-style patterns
// (shipped as config/config-help.yaml; see runtime/config_help.py).
function cfgHelpFor(key){
  if(_cfgHelp.exact && key in _cfgHelp.exact) return _cfgHelp.exact[key];
  for(const [pat,text] of Object.entries(_cfgHelp.patterns||{})){
    const rx=new RegExp("^"+pat.split("*").map(s=>s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join(".*")+"$");
    if(rx.test(key)) return text;
  }
  return "";
}
const CFG_SECTIONS = {
  "Orchestrator": ["orchestrator."],
  "Budgets": ["budgets."],
  "Agent & Verify": ["agent."],
  "Tool Selection": ["tool_selection."],
  "Architect": ["architect."],
  "Compaction": ["compaction."],
  "Parallel Tools": ["parallel_tools."],
  "Privacy & Confirmation": ["privacy.", "confirmation."],
  "Voice": ["voice."],
  "Trace": ["trace."],
  "Web / UI": ["web."],
  "Costs": ["costs."],
  "Verify": ["verify."],
  "Council": ["council."],
  "Skills": ["skills."],
  "Model Swapping": ["models."],   // only live-effective keys reach the editor
  "Tools: ops": ["tools.ops.","tools.call_timeout"],
  "Tools: code & lint": ["tools.code.","tools.lint."],
  "Tools: web & browser": ["tools.web.","tools.browser."],
  "Tools: serve": ["tools.serve."],
  "Tools: rag & research": ["tools.rag.","tools.research."],
  "Tools: test": ["tools.test."],
};
const CFG_SECTION_NOTES = {
  "Model Swapping": "live scheduler flags only — presets, GPUs, binaries and strengths are managed in Admin → Models (seed values in runtime.yaml are not live-editable).",
};
function cfgInput(key, val, isOverridden){
  const id="cfg-"+key.replace(/\./g,"-");
  let inp="";
  if(typeof val==="boolean"){
    inp=`<select id="${id}" data-key="${key}"><option value="true" ${val?"selected":""}>true</option>
      <option value="false" ${!val?"selected":""}>false</option></select>`;
  } else if(typeof val==="number"){
    inp=`<input id="${id}" type="number" step="any" value="${val}" data-key="${key}">`;
  } else if(Array.isArray(val)){
    inp=`<input id="${id}" type="text" value="${esc(JSON.stringify(val))}" data-key="${key}" title="JSON array">`;
  } else {
    const sv=val==null?"":String(val);
    inp=`<input id="${id}" type="text" value="${esc(sv)}" data-key="${key}" placeholder="(null)">`;
  }
  const help=cfgHelpFor(key);
  const lbl=`<div class="cfg-lbl"><label class="${isOverridden?"overridden":""}" title="${key}">${key.split(".").pop()}</label>`
    +(help?`<div class="cfg-help">${esc(help)}</div>`:"")+`</div>`;
  const rst=isOverridden?`<button class="cfg-reset" data-key="${key}" title="Reset to YAML default">↺</button>`:"";
  return `<div class="cfg-row" data-path="${key}">${lbl}${inp}${rst}</div>`;
}
function renderConfig(){
  const container=$("#configSections"); container.innerHTML="";
  const assigned=new Set();
  for(const [section, prefixes] of Object.entries(CFG_SECTIONS)){
    const keys=Object.keys(_cfgFlat).filter(k=>prefixes.some(p=>k.startsWith(p)));
    keys.forEach(k=>assigned.add(k));
    if(!keys.length) continue;
    const sec=document.createElement("div"); sec.className="cfg-section";
    const note=CFG_SECTION_NOTES[section];
    sec.innerHTML=`<div class="cfg-section-head">${section} <span class="muted" style="font-size:11px;margin-left:auto">${keys.length} keys</span></div>
      <div class="cfg-section-body">${note?`<div class="cfg-help" style="padding:4px 0 8px">${esc(note)}</div>`:""}${keys.sort().map(k=>cfgInput(k,_cfgFlat[k],k in _cfgOverrides)).join("")}</div>`;
    sec.querySelector(".cfg-section-head").onclick=()=>sec.classList.toggle("open");
    container.appendChild(sec);
  }
  // Unassigned keys: group by first path segment so "Other" stays small and
  // the filter can open a meaningful group instead of one 40-key bucket.
  const rest=Object.keys(_cfgFlat).filter(k=>!assigned.has(k));
  const groups={};
  for(const k of rest){ const seg=k.split(".")[0]; (groups[seg]||(groups[seg]=[])).push(k); }
  for(const [seg,keys] of Object.entries(groups).sort()){
    const sec=document.createElement("div"); sec.className="cfg-section";
    sec.innerHTML=`<div class="cfg-section-head">Other · ${esc(seg)} <span class="muted" style="font-size:11px;margin-left:auto">${keys.length} keys</span></div>
      <div class="cfg-section-body">${keys.sort().map(k=>cfgInput(k,_cfgFlat[k],k in _cfgOverrides)).join("")}</div>`;
    sec.querySelector(".cfg-section-head").onclick=()=>sec.classList.toggle("open");
    container.appendChild(sec);
  }
  // Wire change listeners
  container.querySelectorAll("input,select").forEach(el=>{
    el.onchange=()=>{ _cfgEdits[el.dataset.key]=parseCfgValue(el); };
  });
  container.querySelectorAll(".cfg-reset").forEach(b=>{
    b.onclick=()=>{ _cfgEdits[b.dataset.key]=null; note($("#configNote"),"will reset "+b.dataset.key,true); };
  });
}
function parseCfgValue(el){
  if(el.tagName==="SELECT") return el.value==="true";
  if(el.type==="number"){ const n=parseFloat(el.value); return Number.isFinite(n)?n:null; }
  const v=el.value.trim();
  if(!v) return null;
  if(v==="true") return true; if(v==="false") return false;
  try{ const j=JSON.parse(v); if(Array.isArray(j)) return j; }catch(_){}
  const n=Number(v); if(v!=="" && Number.isFinite(n) && String(n)===v) return n;
  return v;
}
async function loadConfig(){
  const d=await (await api("/api/admin/config")).json();
  _cfgFlat=d.config; _cfgOverrides=d.overrides||{}; _cfgEdits={}; _cfgHelp=d.help||{};
  renderConfig();
}
$("#saveConfig").onclick=async()=>{
  if(!Object.keys(_cfgEdits).length){ note($("#configNote"),"no changes",true); return; }
  const r=await api("/api/admin/config",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({updates:_cfgEdits})});
  if(r.ok){ note($("#configNote"),"saved "+Object.keys(_cfgEdits).length+" changes",true); loadConfig(); }
  else note($("#configNote"),"error",false);
};
$("#refreshConfig").onclick=loadConfig;
$("#cfgFilter").oninput=function(){
  const q=this.value.toLowerCase();
  document.querySelectorAll(".cfg-row").forEach(r=>{
    r.classList.toggle("hidden", q && !r.dataset.path.toLowerCase().includes(q));
  });
  // Filter the sections too: hide groups without matches, auto-open the
  // rest so hits are visible (and undo the auto-open when clearing).
  document.querySelectorAll(".cfg-section").forEach(sec=>{
    const hits=sec.querySelectorAll(".cfg-row:not(.hidden)").length;
    sec.classList.toggle("hidden", q && !hits);
    if(q && hits && !sec.classList.contains("open")){
      sec.classList.add("open"); sec.dataset.autoOpen="1";
    }else if(!q && sec.dataset.autoOpen){
      sec.classList.remove("open"); delete sec.dataset.autoOpen;
    }
  });
};

// ---- tool toggles (grouped by namespace with descriptions) ----
let _toolData=[];
const TOOL_NS_DESC = {
  "agent":    "Sub-agent spawning and orchestration — spawn child agents, architect complex tasks",
  "archives": "Create and extract archive files — tar.gz, zip, etc.",
  "arxiv":    "Academic paper search — arXiv query and retrieval",
  "ask":      "Clarification — ask the user structured questions when the request is ambiguous",
  "browser":  "Browser automation — take screenshots, save pages as PDF",
  "code":     "Code execution and delegation — sandboxed Python/shell, delegate to the specialist GPU, manage deps, symbol search",
  "context":  "Working memory — pin important results so they survive compaction",
  "council":  "Multi-model deliberation — debate a question across multiple models/personas",
  "deliver":  "File delivery — hand files back to the user as downloads",
  "docs":     "Bulk document processing — summarize entire folder trees via sub-agents",
  "eval":     "Model comparison — run the same prompt across multiple models to compare output/cost",
  "fs":       "File system — read, write, edit, grep, list, and find files in the workspace",
  "git":      "Git operations — the full lifecycle: status, add, commit, branch, push, pull, stash, diff, log, etc.",
  "gpu":      "GPU monitoring — check VRAM usage, temperatures, and utilization",
  "job":      "Background jobs — launch, monitor, and manage long-running detached processes",
  "kg":       "Knowledge graph — track relationships between entities (files, models, concepts)",
  "lint":     "Linting and formatting — run ruff, mypy, and other code quality tools",
  "llm":      "Cloud LLM calls — delegate to external models (glm, gemini, qwen) via LiteLLM",
  "memory":   "Long-term memory — store and retrieve durable notes, preferences, and fixes",
  "mcp":      "MCP bridge — list and call tools on external MCP servers (configure servers in Harness → MCP)",
  "model":    "Model catalog — list available presets, switch active models on GPU slots",
  "note":     "Run-level scratchpad — ephemeral notes that survive context compaction",
  "ops":      "Host operations — run allowlisted commands, check systemd services, stack health",
  "pdf":      "PDF creation — generate PDFs from Markdown, HTML, or text",
  "rag":      "RAG retrieval — index documents and search across embedded collections",
  "research": "Deep research — multi-source, frontier-based investigation with citations",
  "serve":    "Model serving — start/stop/manage local llama.cpp model servers on GPU slots",
  "skill":    "Skill playbooks — list and load procedural skill guides for complex workflows",
  "test":     "Test runner — pytest in the project venv with network access",
  "trace":    "Run history — query past runs and mine recurring patterns",
  "verify":   "Quality verification — LLM-as-judge scoring, ranking, and probing",
  "web":      "Web access — search, fetch pages, extract structured data, crawl paginated sites",
};
async function loadTools(){
  const d=await (await api("/api/admin/disabled-tools")).json();
  _toolData=d.tools||[];
  renderTools();
}
function renderTools(){
  const grid=$("#toolGrid"); grid.innerHTML="";
  const q=($("#toolFilter")||{}).value||"";
  const byNs={}; const nsOrder=[];
  for(const t of _toolData){
    if(!byNs[t.name.split(".")[0]]){ byNs[t.name.split(".")[0]]=[]; nsOrder.push(t.name.split(".")[0]); }
    byNs[t.name.split(".")[0]].push(t);
  }
  let shown=0;
  for(const ns of nsOrder){
    const tools=byNs[ns];
    const visible=q ? tools.filter(t=>(t.name+" "+(t.description||"")).toLowerCase().includes(q.toLowerCase())) : tools;
    if(!visible.length) continue;
    const enabledCount=visible.filter(t=>!t.disabled).length;
    // Namespace group
    const grp=document.createElement("div"); grp.className="tool-ns-group";
    const head=document.createElement("div"); head.className="tool-ns-head";
    const nsCb=document.createElement("input"); nsCb.type="checkbox";
    nsCb.checked=enabledCount===visible.length;
    nsCb.indeterminate=enabledCount>0 && enabledCount<visible.length;
    nsCb.onchange=()=>{ visible.forEach(t=>t.disabled=!nsCb.checked); renderTools(); };
    const label=document.createElement("span"); label.className="tool-ns-name";
    label.innerHTML="<b>"+ns+"</b> <span class='muted' style='font-size:11px'>("+enabledCount+"/"+visible.length+")</span>";
    const desc=document.createElement("div"); desc.className="tool-ns-desc";
    desc.textContent=TOOL_NS_DESC[ns]||"";
    head.append(nsCb, label);
    grp.append(head, desc);
    // Individual tools
    const body=document.createElement("div"); body.className="tool-ns-body";
    for(const t of visible){
      shown++;
      const row=document.createElement("label"); row.className="tool-item"+(t.disabled?" disabled-tool":"");
      const verb=t.name.split(".").slice(1).join(".")||t.name;
      const cb=document.createElement("input"); cb.type="checkbox"; cb.checked=!t.disabled;
      cb.onchange=()=>{ t.disabled=!cb.checked; renderTools(); };
      const nm=document.createElement("span"); nm.className="tname"; nm.textContent=verb;
      nm.title=t.name;
      const wrap=document.createElement("span"); wrap.appendChild(nm);
      if(t.description){
        const td=document.createElement("div"); td.className="tool-desc";
        td.textContent=t.description; td.title=t.description;
        wrap.appendChild(td);
      }
      row.append(cb, wrap);
      body.appendChild(row);
    }
    grp.appendChild(body);
    grid.appendChild(grp);
  }
  $("#toolCount").textContent=shown+" / "+_toolData.length+" tools";
  const all=_toolData.every(t=>!t.disabled);
  const sa=$("#toolSelAll"); if(sa){ sa.checked=all; sa.indeterminate=!all && _toolData.some(t=>!t.disabled); }
}
$("#saveTools").onclick=async()=>{
  const disabled=_toolData.filter(t=>t.disabled).map(t=>t.name);
  const r=await api("/api/admin/disabled-tools",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({disabled})});
  if(r.ok) note($("#toolNote"),"saved — "+disabled.length+" disabled",true);
  else note($("#toolNote"),"error",false);
};
$("#refreshTools").onclick=loadTools;
// ---- tool-description overrides ----
async function loadToolOv(){
  const d=await (await api("/api/admin/tool-overrides")).json();
  const list=$("#toolOvList"); list.innerHTML="";
  const ov=d.overrides||[];
  if(!ov.length){
    list.innerHTML="<div class='muted' style='font-size:12px'>none — all tools show their shipped descriptions</div>";
  }
  for(const o of ov){
    const row=document.createElement("div"); row.className="row";
    row.style.cssText="gap:8px;align-items:flex-start;margin:4px 0";
    const nm=document.createElement("code"); nm.textContent=o.name; nm.style.minWidth="180px";
    const desc=document.createElement("div"); desc.style.cssText="flex:1;font-size:12px";
    desc.textContent=o.description;
    if(!o.known){
      const b=document.createElement("span"); b.className="pill bad"; b.textContent="unknown tool";
      nm.append(" ",b);
    }
    const del=document.createElement("button"); del.textContent="✕"; del.title="remove override (restores shipped description)";
    del.onclick=async()=>{
      const r=await api("/api/admin/tool-overrides/"+encodeURIComponent(o.name),{method:"DELETE"});
      if(r.ok){ note($("#toolOvNote"),"removed "+o.name,true); loadToolOv(); loadTools(); }
      else note($("#toolOvNote"),"error removing "+o.name,false);
    };
    row.append(nm,desc,del);
    list.appendChild(row);
  }
  // datalist for the add form: all known tools
  const dl=$("#toolOvNames"); dl.innerHTML="";
  for(const t of _toolData){ const op=document.createElement("option"); op.value=t.name; dl.appendChild(op); }
}
$("#toolOvAdd").onclick=async()=>{
  const name=$("#toolOvName").value.trim(), desc=$("#toolOvDesc").value.trim();
  if(!name||!desc){ note($("#toolOvNote"),"tool name and description required",false); return; }
  const r=await api("/api/admin/tool-overrides",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({name,description:desc})});
  if(r.ok){ note($("#toolOvNote"),"override saved for "+name,true);
    $("#toolOvName").value=""; $("#toolOvDesc").value=""; loadToolOv(); loadTools(); }
  else{ const e=await r.json().catch(()=>({})); note($("#toolOvNote"),"error: "+(e.detail||r.status),false); }
};
$("#refreshToolOv").onclick=loadToolOv;
// ---- MCP servers ----
let _mcpServers={}, _mcpLoaded=false, _mcpAvailable=true, _mcpInvalid=[], _mcpOverridden=true;
async function loadMcp(){
  const d=await (await api("/api/admin/mcp-servers")).json();
  _mcpServers=d.servers||{}; _mcpLoaded=true;
  _mcpAvailable=d.available!==false;
  _mcpInvalid=d.invalid_names||[];
  _mcpOverridden=d.overridden!==false;
  renderMcp();
}
function renderMcp(){
  const list=$("#mcpList"); list.innerHTML="";
  if(!_mcpAvailable){
    const w=document.createElement("div"); w.className="muted"; w.style.fontSize="12px";
    w.textContent="Note: the 'mcp' package is not installed in this venv — servers can be configured but not called. Install: uv pip install --python .venv/bin/python -r requirements-tools.txt";
    list.appendChild(w);
  }
  if(_mcpInvalid.length){
    const w=document.createElement("div"); w.style.fontSize="12px"; w.style.color="var(--warn,#d8a)";
    w.textContent="These YAML-defined names don't fit the UI's naming rules (lowercase, digits, - _) and block every save here — rename them in runtime.yaml: "+_mcpInvalid.join(", ");
    list.appendChild(w);
  }
  if(!_mcpOverridden && Object.keys(_mcpServers).length){
    const w=document.createElement("div"); w.className="muted"; w.style.fontSize="12px";
    w.textContent="Currently defined in runtime.yaml — the first save here moves management into this UI (stored as a config override; YAML entries then no longer apply).";
    list.appendChild(w);
  }
  const names=Object.keys(_mcpServers).sort();
  if(!names.length){
    const e=document.createElement("div"); e.className="muted"; e.style.fontSize="12px";
    e.textContent="No MCP servers configured yet — add one to give the agent access to external tools.";
    list.appendChild(e); return;
  }
  const tbl=document.createElement("table");
  tbl.innerHTML="<thead><tr><th>Name</th><th>Type</th><th>Target</th><th>Confirm</th><th></th></tr></thead>";
  const tb=document.createElement("tbody");
  for(const n of names){
    const s=_mcpServers[n];
    const tr=document.createElement("tr");
    const type=s.url?"http":"stdio";
    const target=s.url||(s.command+" "+(s.args||[]).join(" "));
    tr.innerHTML="<td><b>"+esc(n)+"</b></td><td>"+type+"</td>"+
      "<td style='font-size:12px;word-break:break-all'>"+esc(target)+"</td>"+
      "<td>"+(s.confirm===false?"off":"on")+"</td>";
    const act=document.createElement("td"); act.style.whiteSpace="nowrap";
    const bTest=document.createElement("button"); bTest.textContent="Test";
    bTest.onclick=async()=>{
      note($("#mcpNote"),"testing "+n+" …",true);
      const r=await api("/api/admin/mcp-servers/test",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({name:n})});
      const d=await r.json();
      if(d.ok) note($("#mcpNote"),n+": OK — "+(d.tools||[]).length+" tools ("+(d.tools||[]).slice(0,5).map(t=>t.name).join(", ")+((d.tools||[]).length>5?", …":"")+")",true);
      else note($("#mcpNote"),n+": "+(d.error||d.detail||"failed"),false);
    };
    const bEdit=document.createElement("button"); bEdit.textContent="Edit";
    bEdit.onclick=()=>openMcpForm(n);
    const bDel=document.createElement("button"); bDel.textContent="Delete";
    bDel.onclick=async()=>{
      let msg="Delete MCP server '"+n+"'?";
      if(Object.keys(_mcpServers).length===1 && !_mcpOverridden)
        msg+=" It is the last one — deleting all falls back to the runtime.yaml definitions, which will reappear.";
      if(!confirm(msg)) return;
      delete _mcpServers[n];
      await saveMcp();
    };
    act.append(bTest," ",bEdit," ",bDel);
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  list.appendChild(tbl);
}
function openMcpForm(name){
  const s=name?_mcpServers[name]:{};
  $("#mcpName").value=name||""; $("#mcpName").disabled=!!name;
  $("#mcpUrl").value=s.url||"";
  $("#mcpCommand").value=s.command||"";
  $("#mcpArgs").value=(s.args||[]).join(" ");
  $("#mcpEnv").value=Object.entries(s.env||{}).map(([k,v])=>k+"="+v).join(", ");
  $("#mcpConfirm").checked=s.confirm!==false;
  $("#mcpTimeout").value=s.timeout_s||"";
  $("#mcpForm").style.display="block";
}
async function saveMcp(){
  const r=await api("/api/admin/mcp-servers",{method:"PUT",headers:{"content-type":"application/json"},
    body:JSON.stringify({servers:_mcpServers})});
  const d=await r.json();
  if(r.ok){ note($("#mcpNote"),"saved — active immediately",true); await loadMcp(); }
  else note($("#mcpNote"),d.detail||"error",false);
  return r.ok;
}
$("#mcpAdd").onclick=()=>openMcpForm(null);
$("#mcpRefresh").onclick=loadMcp;
$("#mcpCancel").onclick=()=>{ $("#mcpForm").style.display="none"; };
$("#mcpSave").onclick=async()=>{
  const name=$("#mcpName").value.trim();
  if(!name){ note($("#mcpNote"),"name required",false); return; }
  const cfg={confirm:$("#mcpConfirm").checked};
  const url=$("#mcpUrl").value.trim();
  if(url) cfg.url=url;
  else{
    const cmd=$("#mcpCommand").value.trim();
    if(!cmd){ note($("#mcpNote"),"either a HTTP url or a stdio command is required",false); return; }
    cfg.command=cmd;
    const args=$("#mcpArgs").value.trim();
    if(args) cfg.args=args.split(/\s+/);
  }
  const envRaw=$("#mcpEnv").value.trim();
  if(envRaw){
    const env={};
    for(const pair of envRaw.split(",")){
      const i=pair.indexOf("=");
      if(i<1){ note($("#mcpNote"),"env entries must be KEY=VALUE",false); return; }
      env[pair.slice(0,i).trim()]=pair.slice(i+1).trim();
    }
    cfg.env=env;
  }
  const to=parseInt($("#mcpTimeout").value,10);
  if(to>0) cfg.timeout_s=to;
  _mcpServers[name]=cfg;
  if(await saveMcp()){ $("#mcpForm").style.display="none"; }
};

// ---- Connectors (declarative external systems) ----
let _conns=null, _connErrors=[];
$("#connImport").onclick=()=>$("#connImportFile").click();
$("#connImportFile").onchange=function(){ if(this.files.length) connImport(this.files[0],false); this.value=""; };
async function connImport(file, overwrite){
  const fd=new FormData(); fd.append("file",file);
  const r=await api("/api/admin/studio/import"+(overwrite?"?overwrite=true":""),{method:"POST",body:fd});
  const j=await r.json().catch(()=>({}));
  if(r.status===409 && j.detail && j.detail.manifest){
    const m=j.detail.manifest;
    if(await dlgConfirm("A connector named '"+m.name+"' already exists.\n\nOverwrite?",{yes:"overwrite"}))
      return connImport(file,true);
    return;
  }
  if(!r.ok){ note($("#connNote"),j.detail||"import failed",false); return; }
  note($("#connNote"),"imported connector '"+j.name+"' — live now",true);
  loadConnectors();
}
async function loadConnectors(){
  const d=await (await api("/api/admin/connectors")).json();
  _conns=d.connectors||[]; _connErrors=d.errors||[];
  renderConnectors();
}
function renderConnectors(){
  const list=$("#connList"); list.innerHTML="";
  if(_connErrors.length){
    const w=document.createElement("div"); w.style.cssText="font-size:12px;color:var(--err,#e06c75);white-space:pre-line";
    w.textContent="Skipped bad connector sources:\n"+_connErrors.join("\n");
    list.appendChild(w);
  }
  if(!_conns.length){
    const e=document.createElement("div"); e.className="muted"; e.style.fontSize="12px";
    e.textContent="No connectors yet — build one in the Studio or import a .jayconn.";
    list.appendChild(e); return;
  }
  for(const c of _conns){
    const card=document.createElement("div");
    card.style.cssText="border:1px solid var(--line,#333);border-radius:8px;padding:10px 12px;margin-bottom:10px";
    const head=document.createElement("div");
    head.style.cssText="display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    head.innerHTML="<b>"+esc(c.id)+"</b>"+
      "<span class='muted' style='font-size:12px'>"+esc(c.description||"")+"</span>"+
      "<span class='muted' style='font-size:11px'>"+(c.legacy?"single-file":"package")+" · "+
        c.tools_live+"/"+c.tools_total+" tools live"+(c.writes_live?" · "+c.writes_live+" write":"")+"</span>";
    // enabled toggle
    const enLab=document.createElement("label"); enLab.style.cssText="font-size:12px;margin-left:auto";
    const en=document.createElement("input"); en.type="checkbox"; en.checked=!!c.enabled;
    en.onchange=async()=>{
      const r=await api("/api/admin/connectors/"+encodeURIComponent(c.id),
        {method:"PUT",body:JSON.stringify({enabled:en.checked})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){ note($("#connNote"),j.detail||"toggle failed",false); en.checked=!en.checked; return; }
      note($("#connNote"),c.id+(en.checked?" enabled":" disabled")+" — live now",true); loadConnectors();
    };
    enLab.appendChild(en); enLab.appendChild(document.createTextNode(" enabled"));
    head.appendChild(enLab);
    // RO/RW
    const modeLab=document.createElement("label"); modeLab.style.cssText="font-size:12px";
    const sel=document.createElement("select");
    sel.title="read-only: only the connector's read tools are live (write tools are unregistered, not just gated) · read-write: all tools live, write tools still ask for confirmation in chat";
    for(const m of ["ro","rw"]){
      const o=document.createElement("option"); o.value=m; o.textContent=m==="ro"?"read-only · write tools off":"read-write · writes ask in chat";
      if(m===c.mode) o.selected=true;
      if(m==="rw"&&c.allows!=="rw") o.disabled=true;
      sel.appendChild(o);
    }
    sel.onchange=async()=>{
      const r=await api("/api/admin/connectors/"+encodeURIComponent(c.id),
        {method:"PUT",body:JSON.stringify({mode:sel.value})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){ note($("#connNote"),j.detail||"mode change failed",false); sel.value=c.mode; return; }
      note($("#connNote"),c.id+" → "+sel.value+" — live now",true); loadConnectors();
    };
    modeLab.appendChild(sel);
    head.appendChild(modeLab);
    card.appendChild(head);
    // settings (from the package's schema)
    const keys=Object.keys(c.settings_schema||{});
    if(keys.length){
      const fs=document.createElement("div");
      fs.style.cssText="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:center";
      const inputs={};
      for(const k of keys){
        const sch=c.settings_schema[k];
        const inp=document.createElement("input");
        inp.value=(c.settings||{})[k]||"";
        inp.placeholder=k+(sch.secret?" (env var name)":"");
        inp.title=sch.description||k;
        inp.style.cssText="font-size:12px;min-width:180px;flex:1";
        inputs[k]=inp; fs.appendChild(inp);
      }
      const save=document.createElement("button"); save.textContent="save settings";
      save.style.fontSize="12px";
      save.onclick=async()=>{
        const settings={}; for(const k of keys) settings[k]=inputs[k].value;
        const r=await api("/api/admin/connectors/"+encodeURIComponent(c.id),
          {method:"PUT",body:JSON.stringify({settings})});
        const j=await r.json().catch(()=>({}));
        note($("#connNote"),r.ok?"settings saved — live now":(j.detail||"save failed"),r.ok);
        if(r.ok) loadConnectors();
      };
      fs.appendChild(save); card.appendChild(fs);
    }
    // actions
    const act=document.createElement("div"); act.style.cssText="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap";
    const mk=(label,fn,primary)=>{ const b=document.createElement("button"); b.textContent=label;
      b.style.fontSize="12px"; if(primary) b.className="primary"; b.onclick=fn; act.appendChild(b); return b; };
    mk("test",async()=>{
      const r=await api("/api/admin/connectors/"+encodeURIComponent(c.id)+"/test",{method:"POST"});
      const j=await r.json().catch(()=>({}));
      if(j.ok) note($("#connNote"),c.id+" OK via "+j.tool+(j.result?": "+j.result.slice(0,120):""),true);
      else note($("#connNote"),c.id+" test failed"+(j.tool?" ("+j.tool+")":"")+": "+(j.error||j.detail||"?"),false);
    });
    mk("export .jayconn",()=>{ window.location="/api/admin/connectors/"+encodeURIComponent(c.id)+"/export"; });
    mk("delete",async()=>{
      if(!await dlgConfirm("Delete connector '"+c.id+"' and its state? Tools vanish immediately.")) return;
      const r=await api("/api/admin/connectors/"+encodeURIComponent(c.id),{method:"DELETE"});
      note($("#connNote"),r.ok?"deleted":"delete failed",r.ok); loadConnectors();
    });
    if(c.readme){
      const det=document.createElement("details"); det.style.cssText="font-size:12px;margin-top:6px";
      const sum=document.createElement("summary"); sum.textContent="README"; det.appendChild(sum);
      const pre=document.createElement("pre"); pre.style.cssText="white-space:pre-wrap;font-size:11px";
      pre.textContent=c.readme; det.appendChild(pre); act.appendChild(det);
    }
    card.appendChild(act);
    list.appendChild(card);
  }
}
$("#connRefresh").onclick=loadConnectors;
(function(){
  const f=$("#toolFilter"); if(f) f.oninput=renderTools;
  const sa=$("#toolSelAll"); if(sa) sa.onchange=function(){
    _toolData.forEach(t=>t.disabled=!this.checked); renderTools();
  };
})();
async function loadUsagePerUser(){
  const d=await (await api("/api/admin/usage")).json();
  const t=d.totals||{};
  const fmtN=n=>(n||0).toLocaleString();
  const ago=ts=>{ if(!ts) return "—"; const s=Date.now()/1000-ts;
    if(s<3600) return Math.round(s/60)+"m ago"; if(s<86400) return Math.round(s/3600)+"h ago";
    return Math.round(s/86400)+"d ago"; };
  $("#usageTotals").innerHTML=
    '<div class="stat"><div class="k">Users</div><div class="v">'+fmtN(t.users)+'</div></div>'+
    '<div class="stat"><div class="k">Total runs</div><div class="v">'+fmtN(t.runs)+'</div></div>'+
    '<div class="stat"><div class="k">Total tokens</div><div class="v">'+fmtN(t.tokens)+'</div></div>'+
    '<div class="stat"><div class="k">Total cost</div><div class="v">$'+(t.cost||0).toFixed(4)+'</div></div>';
  $("#usageRows").innerHTML=(d.users||[]).map(u=>
    "<tr><td>"+esc(u.user)+"</td><td>"+fmtN(u.runs)+"</td><td>"+fmtN(u.ok)+"</td>"+
    "<td>"+(u.errors?('<span style="color:#e5679a">'+fmtN(u.errors)+'</span>'):"0")+"</td>"+
    "<td>"+fmtN(u.tokens)+"</td><td>$"+(u.cost||0).toFixed(4)+"</td><td class='muted'>"+ago(u.last_active)+"</td></tr>"
  ).join("") || '<tr><td colspan="7" class="muted">No runs recorded yet.</td></tr>';
}
$("#refreshUsage").onclick=loadUsagePerUser;

// ---- flagged sessions (privacy-safe debugging) ----
// -- procedure miner: distill a SKILL.md draft from run history (eval case
// or flagged session). Nothing auto-applies — the draft opens for review
// and saving writes it via the Studio with draft: true (model-invisible
// until a human clears the flag there).
async function mineProcedure(url, body, noteEl){
  note(noteEl,"⛏ mining procedure — the judge is reading the runs (can take a minute)…",true);
  const r=await api(url,{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify(body||{})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note(noteEl,typeof j.detail==="string"?j.detail:"mining failed",false); return; }
  note(noteEl,"procedure draft ready ("+(j.judge_model||"judge")+(j.cost_usd?", $"+Number(j.cost_usd).toFixed(4):"")+") — review below",true);
  showProcDraft(j,noteEl);
}
function showProcDraft(j,noteEl){
  const overlay=document.createElement("div"); overlay.className="dlg-overlay";
  const box=document.createElement("div"); box.className="dlg-box";
  box.style.maxWidth="720px"; box.style.width="calc(100vw - 48px)";
  const head=document.createElement("p"); head.className="dlg-text";
  head.innerHTML="<b>⛏ procedure draft</b> · "+esc(j.source||"")+" · judge: "+esc(j.judge_model||"")+
    (j.shape?" · shape: <b>"+esc(j.shape)+"</b>":"")+
    "<br><span class='muted'>Review and edit — saving creates a Studio skill with <code>draft: true</code>, "+
    "invisible to the model until you clear the flag in the Studio.</span>";
  box.appendChild(head);
  if(j.errors&&j.errors.length){
    const w=document.createElement("p"); w.className="dlg-text"; w.style.color="var(--err,#e06c75)";
    w.textContent="validation warnings: "+j.errors.join("; "); box.appendChild(w);
  }
  const ta=document.createElement("textarea"); ta.value=j.draft||"";
  ta.style.cssText="width:100%;box-sizing:border-box;height:46vh;font:12px/1.45 monospace;"+
    "background:var(--panel2,#1c212a);color:var(--fg,#e6e9ee);border:1px solid var(--border,#2a323d);"+
    "border-radius:6px;padding:10px";
  box.appendChild(ta);
  const row=document.createElement("div"); row.className="dlg-row"; row.style.marginTop="12px";
  const close=( )=>overlay.remove();
  const no=document.createElement("button"); no.className="dlg-no"; no.textContent="discard"; no.onclick=close;
  const yes=document.createElement("button"); yes.className="dlg-yes";
  yes.textContent="save as Studio draft"; yes.disabled=!j.name;
  yes.onclick=async()=>{
    yes.disabled=true;
    const r=await api("/api/admin/studio/skill/"+encodeURIComponent(j.name),{method:"PUT",
      headers:{"content-type":"application/json"},body:JSON.stringify({content:ta.value})});
    const resp=await r.json().catch(()=>({}));
    if(!r.ok){ note(noteEl,typeof resp.detail==="string"?resp.detail:"save failed",false); yes.disabled=false; return; }
    close();
    note(noteEl,"saved draft skill '"+j.name+"' — review it in Studio → Build and clear draft: true to activate",true);
    loadStudio();
  };
  row.appendChild(no); row.appendChild(yes); box.appendChild(row);
  overlay.addEventListener("mousedown",e=>{ if(e.target===overlay) close(); });
  overlay.appendChild(box); document.body.appendChild(overlay);
}

async function loadFlags(){
  const d=await (await api("/api/admin/flags")).json();
  $("#flagRows").innerHTML=(d.flags||[]).map(f=>
    "<tr><td class='muted'>"+esc((f.created_at||"").replace("T"," ").replace("Z",""))+"</td>"+
    "<td>"+esc(f.owner)+"</td><td>"+esc(f.chat_title||"—")+"</td>"+
    "<td class='muted' style='max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>"+esc(f.comment||"")+"</td>"+
    "<td>"+(f.run_ids||[]).length+"</td>"+
    "<td>"+(f.resolved?"<span class='pill'>resolved</span>":"<span class='pill st up'>open</span>")+
      (f.include_private?" <span class='pill up'>private</span>":"")+"</td>"+
    "<td style='text-align:right;white-space:nowrap'>"+
      "<button data-view='"+f.id+"'>view</button> "+
      "<button data-mk='"+f.id+"' title='Draft an eval test case from this flag'>make test</button> "+
      "<button data-mineproc='"+f.id+"' title='Distill a procedure draft from this flagged session (judge model; review before saving — never auto-live)'>⛏ mine</button> "+
      "<button data-res='"+f.id+"' data-is='"+(f.resolved?1:0)+"'>"+(f.resolved?"reopen":"resolve")+"</button> "+
      "<button class='danger' data-del='"+f.id+"'>delete</button></td></tr>"
  ).join("") || '<tr><td colspan="7" class="muted">No flagged sessions.</td></tr>';
  $("#flagRows").querySelectorAll("[data-view]").forEach(b=>b.onclick=()=>showFlag(b.dataset.view));
  $("#flagRows").querySelectorAll("[data-mk]").forEach(b=>b.onclick=()=>flagMakeTest(b));
  $("#flagRows").querySelectorAll("[data-mineproc]").forEach(b=>b.onclick=()=>
    mineProcedure("/api/admin/flags/"+encodeURIComponent(b.dataset.mineproc)+"/mine-procedure",null,$("#flagNote")));
  $("#flagRows").querySelectorAll("[data-res]").forEach(b=>b.onclick=async()=>{
    await api("/api/admin/flags/"+b.dataset.res+"/resolve",{method:"POST",
      headers:{"content-type":"application/json"},body:JSON.stringify({resolved:b.dataset.is!=="1"})});
    loadFlags();
  });
  $("#flagRows").querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete this flag?", {yes:"delete"})) return;
    await api("/api/admin/flags/"+b.dataset.del,{method:"DELETE"});
    $("#flagDetail").style.display="none"; loadFlags();
  });
}
async function showFlag(id){
  const d=await (await api("/api/admin/flags/"+id)).json();
  const f=d.flag||{};
  const lines=["FLAGGED by "+f.owner+" · "+(f.created_at||"")+(f.chat_title?" · "+f.chat_title:""),
               f.comment?("comment: "+f.comment):"(no comment)", ""];
  for(const r of (d.runs||[])){
    lines.push("=== run "+r.id+" · "+(r.status||"?")+(r.error?(" · error: "+r.error):"")+
      " · "+(r.duration_s!=null?r.duration_s+"s":"—")+" · "+(r.total_tokens||0)+" tok · $"+(r.cost_usd||0).toFixed(4));
    for(const e of (r.events||[])){
      const t=new Date(e.ts*1000).toLocaleTimeString();
      lines.push("  ["+t+"] ("+e.iteration+") "+e.kind+": "+JSON.stringify(e.payload).slice(0,300));
    }
    lines.push("");
  }
  for(const m of (d.missing_runs||[])) lines.push("=== run "+m+" · pruned by trace retention");
  for(const r of (d.reports||[])){
    lines.push("=== coroner report · "+r.trigger+" · "+(r.created_at||""));
    lines.push(r.report||"");
    lines.push("");
  }
  const pre=$("#flagDetail"); pre.style.display="block";
  pre.textContent=lines.join("\n")||"(no runs available)";
  pre.scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#refreshFlags").onclick=loadFlags;
loadFlags();

// "make test" — the eval harness drafts a regression case from the flag's
// coroner report(s); the draft opens prefilled (unsaved) in Eval → Cases.
async function flagMakeTest(btn){
  btn.disabled=true;
  const label=btn.textContent; btn.textContent="drafting…";
  try{
    const r=await api("/api/admin/flags/"+btn.dataset.mk+"/make-test",{method:"POST"});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ await dlgAlert(typeof j.detail==="string"?j.detail:"make-test failed"); return; }
    sessionStorage.setItem("evalDraft", JSON.stringify({
      yaml:j.yaml||"", suggested_id:j.suggested_id||"", errors:j.errors||[]}));
    goTab("eval","eval");
  } finally { btn.disabled=false; btn.textContent=label; }
}

// ---- watchdog reports (run coroner) ----
async function loadReports(){
  const d=await (await api("/api/admin/reports")).json();
  $("#reportRows").innerHTML=(d.reports||[]).map(r=>
    "<tr><td class='muted'>"+esc((r.created_at||"").replace("T"," ").replace("Z",""))+"</td>"+
    "<td>"+esc(r.owner)+"</td><td>"+esc(r.trigger)+"</td>"+
    "<td><span class='pill'>"+esc(r.status)+"</span></td>"+
    "<td class='muted' style='max-width:520px;white-space:pre-wrap;font-size:12px'>"+esc(r.report||"")+"</td>"+
    "<td style='text-align:right'><button class='danger' data-del='"+r.id+"'>delete</button></td></tr>"
  ).join("") || '<tr><td colspan="6" class="muted">No reports yet — the watchdog only writes on distressed runs.</td></tr>';
  $("#reportRows").querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete this report?", {yes:"delete"})) return;
    await api("/api/admin/reports/"+b.dataset.del,{method:"DELETE"});
    loadReports();
  });
}
$("#refreshReports").onclick=loadReports;
loadReports();

// ---- Studio (custom skills, chains, connectors, python tools) ----
let _studio=null;
const STUDIO_EDITORS=["#stSkillEdit","#stChainEdit","#stConnEdit","#stToolEdit"];
function studioCloseEditors(){ STUDIO_EDITORS.forEach(id=>$(id).style.display="none"); }
function originBadge(o){ return o==="custom"?"<span class='pill up'>custom</span>":"<span class='pill'>builtin</span>"; }
function restartSuffix(kind){ return (kind==="connector"||kind==="tool")?" — restart services to activate":""; }
function studioDetail(j){ return typeof j.detail==="string"?j.detail:((j.detail&&j.detail.error)||"error"); }
function showErrors(el, errors){
  el.innerHTML=(errors||[]).map(e=>e.startsWith("warning:")
    ?"<div class='wl'>"+esc(e)+"</div>":"<div>"+esc(e)+"</div>").join("");
}
async function studioValidate(kind, name, content, errEl, noteEl){
  const r=await api("/api/admin/studio/validate",{method:"POST",
    headers:{"content-type":"application/json"},
    body:JSON.stringify({kind,name,content})});
  const j=await r.json();
  const errors=j.errors||[];
  showErrors(errEl, errors);
  const hard=errors.filter(e=>!e.startsWith("warning:"));
  if(noteEl) note(noteEl, hard.length?"invalid":(errors.length?"ok (warnings)":"ok"), !hard.length);
  return hard;
}
async function studioPut(kind, name, content, errEl, noteEl){
  const r=await api("/api/admin/studio/"+kind+"/"+encodeURIComponent(name),{method:"PUT",
    headers:{"content-type":"application/json"}, body:JSON.stringify({content})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ showErrors(errEl,[studioDetail(j)]); return false; }
  note(noteEl,"saved"+restartSuffix(kind),true);
  return true;
}
async function studioDelete(kind, name){
  if(!await dlgConfirm("Delete custom "+kind+" '"+name+"'?", {yes:"delete"})) return;
  const r=await api("/api/admin/studio/"+kind+"/"+encodeURIComponent(name),{method:"DELETE"});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ await dlgAlert(studioDetail(j)); return; }
  note($("#stNote"),"deleted "+kind+" '"+name+"'"+restartSuffix(kind),true);
  loadStudio();
}
function studioExport(kind, name){
  window.location="/api/admin/studio/export/"+kind+"/"+encodeURIComponent(name);
}

async function loadStudio(){
  _studio=await (await api("/api/admin/studio")).json();
  renderStudio();
}
function renderStudio(){
  // skills — builtin rows: view / duplicate / export; custom rows: edit / export / delete
  $("#stSkillRows").innerHTML=(_studio.skills||[]).map(s=>{
    const cust=s.origin==="custom";
    return `<tr><td><b>${esc(s.name)}</b>${s.draft?" <span class='pill' style='border-color:goldenrod;color:goldenrod' title='draft: true — invisible to the model; clear the flag in the editor to activate'>draft</span>":""}${s.shape?" <span class='pill' title='procedure — shape tag for the selector/autoload'>⛏ ${esc(s.shape)}</span>":""}</td><td>${originBadge(s.origin)}</td>
      <td class="muted" style="font-size:12px">${esc(s.description||"")}</td>
      <td style="text-align:right;white-space:nowrap">
        ${cust?`<button data-sk-edit="${esc(s.name)}">edit</button>`:
                `<button data-sk-view="${esc(s.name)}">view</button>
                 <button data-sk-fork="${esc(s.name)}">duplicate to custom</button>`}
        <button data-sk-exp="${esc(s.name)}">export</button>
        ${cust?`<button class="danger" data-sk-del="${esc(s.name)}">delete</button>`:""}
      </td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">no skills found</td></tr>`;
  $("#stChainRows").innerHTML=(_studio.chains||[]).map(c=>{
    const cust=c.origin==="custom";
    const desc=c.error?`<span style="color:var(--err)">${esc(c.error)}</span>`:esc(c.description||"");
    return `<tr><td><b>${esc(c.name)}</b></td><td>${originBadge(c.origin)}</td>
      <td>${c.steps??"—"}</td>
      <td class="muted" style="font-size:12px">${desc}</td>
      <td style="text-align:right;white-space:nowrap">
        ${cust?`<button data-ch-edit="${esc(c.name)}">edit</button>`:
                `<button data-ch-view="${esc(c.name)}">view</button>
                 <button data-ch-fork="${esc(c.name)}">duplicate to custom</button>`}
        <button data-ch-exp="${esc(c.name)}">export</button>
        ${cust?`<button class="danger" data-ch-del="${esc(c.name)}">delete</button>`:""}
      </td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">no chains found</td></tr>`;
  $("#stConnRows").innerHTML=(_studio.connectors||[]).map(c=>
    `<tr><td><b>${esc(c.name)}</b><div class="muted" style="font-size:11px">custom.${esc(c.name)}</div></td>
      <td>${originBadge(c.origin)}</td>
      <td class="muted" style="font-size:12px">${esc(c.description||"")}</td>
      <td style="text-align:right;white-space:nowrap">
        <button data-co-edit="${esc(c.name)}">edit</button>
        <button data-co-exp="${esc(c.name)}">export</button>
        <button class="danger" data-co-del="${esc(c.name)}">delete</button></td></tr>`
  ).join("") || `<tr><td colspan="4" class="muted">no connectors yet</td></tr>`;
  $("#stToolRows").innerHTML=(_studio.tools||[]).map(t=>
    `<tr><td><b>${esc(t.name)}</b></td><td>${originBadge(t.origin)}</td>
      <td class="muted" style="font-size:12px">${esc(t.path||"")}</td>
      <td style="text-align:right;white-space:nowrap">
        <button data-tl-edit="${esc(t.name)}">edit</button>
        <button data-tl-exp="${esc(t.name)}">export</button>
        <button class="danger" data-tl-del="${esc(t.name)}">delete</button></td></tr>`
  ).join("") || `<tr><td colspan="4" class="muted">no python tools yet</td></tr>`;
  const wire=(attr,fn)=>document.querySelectorAll("#tab-studio ["+attr+"]").forEach(b=>b.onclick=()=>fn(b.getAttribute(attr)));
  wire("data-sk-edit",n=>editSkill(n,"edit")); wire("data-sk-view",n=>editSkill(n,"view"));
  wire("data-sk-fork",n=>editSkill(n,"fork")); wire("data-sk-exp",n=>studioExport("skill",n));
  wire("data-sk-del",n=>studioDelete("skill",n));
  wire("data-ch-edit",n=>editChain(n,"edit")); wire("data-ch-view",n=>editChain(n,"view"));
  wire("data-ch-fork",n=>editChain(n,"fork")); wire("data-ch-exp",n=>studioExport("chain",n));
  wire("data-ch-del",n=>studioDelete("chain",n));
  wire("data-co-edit",n=>editConnector(n)); wire("data-co-exp",n=>studioExport("connector",n));
  wire("data-co-del",n=>studioDelete("connector",n));
  wire("data-tl-edit",n=>editTool(n)); wire("data-tl-exp",n=>studioExport("tool",n));
  wire("data-tl-del",n=>studioDelete("tool",n));
}
$("#stRefresh").onclick=loadStudio;

// -- import (.jaypack) --
async function studioImport(file, overwrite){
  const fd=new FormData(); fd.append("file",file);
  const r=await api("/api/admin/studio/import"+(overwrite?"?overwrite=true":""),{method:"POST",body:fd});
  const j=await r.json().catch(()=>({}));
  if(r.status===409 && j.detail && j.detail.manifest){
    const m=j.detail.manifest;
    if(await dlgConfirm("A "+m.kind+" named '"+m.name+"' already exists.\n\nPack: "+m.kind+
      " '"+m.name+"' v"+(m.version||"?")+(m.description?"\n"+m.description:"")+
      (m.author?"\nby "+m.author:"")+
      (m.shape?"\n\n⚠ PROCEDURE (shape: "+m.shape+") — auto-loads at run start on matching requests. Review its checkpoints before installing.":"")+
      "\n\nOverwrite?", {yes:"overwrite"}))
      return studioImport(file,true);
    return;
  }
  if(!r.ok){ note($("#stNote"),studioDetail(j),false); return; }
  note($("#stNote"),"imported "+j.kind+" '"+j.name+"'"+
    (j.shape?" — PROCEDURE (shape: "+j.shape+"): auto-loads on matching requests; review its checkpoints in the Studio":"")+
    (j.needs_restart?" — restart services to activate":""),true);
  loadStudio();
}
$("#stImport").onclick=()=>$("#stImportFile").click();
$("#stImportFile").onchange=function(){ if(this.files.length) studioImport(this.files[0],false); this.value=""; };

// -- skill editor --
let _skMode="new";   // new | edit | view | fork
function skillCompose(){
  return "---\nname: "+yq($("#stSkName").value.trim())+
    "\ndescription: "+yq($("#stSkDesc").value.replace(/\s*\n\s*/g," ").trim())+
    "\n---\n\n"+$("#stSkBody").value.replace(/^\n+/,"");
}
function parseSkillMd(text){
  const m=String(text||"").match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if(!m) return {desc:"",body:String(text||"")};
  const dm=m[1].match(/^description:\s*(.*)$/m);
  const raw=dm?dm[1].trim():"";
  let desc=raw;
  if(raw.length>1&&raw[0]==='"'){ try{ desc=JSON.parse(raw); }catch(e){ desc=raw.slice(1,-1); } }
  else desc=raw.replace(/^'(.*)'$/,"$1");
  return {desc, body:m[2].replace(/^\n+/,"")};
}
function skShowSource(){ $("#stSkView").hidden=true; $("#stSkBody").hidden=false;
  $("#stSkToggle").textContent="view rendered"; $("#stSkBody").focus(); }
function skShowRendered(){ $("#stSkView").innerHTML=renderMarkdown($("#stSkBody").value);
  $("#stSkView").hidden=false; $("#stSkBody").hidden=true; $("#stSkToggle").textContent="edit source"; }
$("#stSkToggle").onclick=()=>{ $("#stSkBody").hidden?skShowSource():skShowRendered(); };
async function editSkill(name, mode){
  _skMode=mode;
  let content="", desc="";
  if(mode!=="new"){
    const r=await api("/api/admin/studio/skill/"+encodeURIComponent(name));
    if(!r.ok){ note($("#stNote"),studioDetail(await r.json().catch(()=>({}))),false); return; }
    const d=await r.json(); content=d.content; desc=d.description||"";
  }
  studioCloseEditors();
  $("#stSkillTitle").textContent=mode==="new"?"New skill":
    mode==="view"?("View skill: "+name+" (builtin, read-only)"):
    mode==="fork"?("Duplicate to custom: "+name):("Edit skill: "+name);
  $("#stSkName").value=name||""; $("#stSkName").disabled=mode!=="new";
  const parsed=parseSkillMd(content);
  $("#stSkDesc").value=mode==="new"?"":(parsed.desc||desc);
  $("#stSkBody").value=mode==="new"?"":parsed.body;
  $("#stSkErr").innerHTML="";
  const ro=mode==="view";
  $("#stSkDesc").disabled=ro; $("#stSkBody").disabled=ro;
  ["#stSkDraft","#stSkValidate","#stSkSave"].forEach(id=>$(id).style.display=ro?"none":"");
  skShowRendered();
  $("#stSkillEdit").style.display="block";
  $("#stSkillEdit").scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#stNewSkill").onclick=()=>editSkill(null,"new");
$("#stSkCancel").onclick=()=>{ $("#stSkillEdit").style.display="none"; };
$("#stSkValidate").onclick=async()=>{
  await studioValidate("skill",$("#stSkName").value.trim(),skillCompose(),$("#stSkErr"),$("#stSkNote"));
};
$("#stSkSave").onclick=async()=>{
  const name=$("#stSkName").value.trim();
  if(!name){ showErrors($("#stSkErr"),["name is required"]); return; }
  if(await studioPut("skill",name,skillCompose(),$("#stSkErr"),$("#stSkNote"))){
    $("#stSkillEdit").style.display="none"; loadStudio();
  }
};
$("#stSkDraft").onclick=()=>openDraft("skill");

// -- chain editor --
let _chMode="new";
const CHAIN_SCAFFOLD=`description: what this chain does
steps:
  - id: first
    agent: "Do something about {{input}}"
    tools: [web.search]
  - id: distill
    prompt: "Distill into bullets:\\n\\n{{steps.first.output}}"
`;
async function editChain(name, mode){
  _chMode=mode;
  let content="";
  if(mode!=="new"){
    const r=await api("/api/admin/studio/chain/"+encodeURIComponent(name));
    if(!r.ok){ note($("#stNote"),studioDetail(await r.json().catch(()=>({}))),false); return; }
    content=(await r.json()).content;
  }
  studioCloseEditors();
  $("#stChainTitle").textContent=mode==="new"?"New chain":
    mode==="view"?("View chain: "+name+" (builtin, read-only)"):
    mode==="fork"?("Duplicate to custom: "+name):("Edit chain: "+name);
  $("#stChName").value=name||""; $("#stChName").disabled=mode!=="new";
  $("#stChYaml").value=mode==="new"?CHAIN_SCAFFOLD:content;
  $("#stChYaml").disabled=mode==="view";
  $("#stChErr").innerHTML="";
  const ro=mode==="view";
  ["#stChDraft","#stChValidate","#stChSave"].forEach(id=>$(id).style.display=ro?"none":"");
  $("#stChainEdit").style.display="block";
  $("#stChainEdit").scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#stNewChain").onclick=()=>editChain(null,"new");
$("#stChCancel").onclick=()=>{ $("#stChainEdit").style.display="none"; };
$("#stChValidate").onclick=async()=>{
  await studioValidate("chain",$("#stChName").value.trim(),$("#stChYaml").value,$("#stChErr"),$("#stChNote"));
};
$("#stChSave").onclick=async()=>{
  const name=$("#stChName").value.trim();
  if(!name){ showErrors($("#stChErr"),["name is required"]); return; }
  const hard=await studioValidate("chain",name,$("#stChYaml").value,$("#stChErr"),$("#stChNote"));
  if(hard.length) return;   // invalid — show the errors, don't save
  if(await studioPut("chain",name,$("#stChYaml").value,$("#stChErr"),$("#stChNote"))){
    $("#stChainEdit").style.display="none"; loadStudio();
  }
};
$("#stChDraft").onclick=()=>openDraft("chain");

// -- connector editor (guided form, YAML rendered client-side) --
let _coTarget=null;   // name being edited, null = new
function yq(s){   // conservative YAML scalar: bare when plainly safe, else double-quoted
  s=String(s==null?"":s);
  if(s!=="" && /^[A-Za-z_./][A-Za-z0-9_ ./\-]*$/.test(s)
     && !/^(true|false|null|yes|no|on|off)$/i.test(s) && s.indexOf(": ")===-1
     && s.indexOf(" #")===-1) return s;
  return JSON.stringify(s);
}
function connParams(){
  return [...$("#stCoParams").querySelectorAll(".param-row")].map(r=>({
    name:r.querySelector("[data-p=name]").value.trim(),
    type:r.querySelector("[data-p=type]").value,
    required:r.querySelector("[data-p=req]").checked,
    desc:r.querySelector("[data-p=desc]").value.trim(),
  })).filter(p=>p.name);
}
function connFormToYaml(){
  const name=$("#stCoName").value.trim()||"<name>";
  const L=["name: "+yq("custom."+name),
           "description: "+yq($("#stCoDesc").value.trim()),
           "base_url: "+yq($("#stCoBase").value.trim())];
  const env=$("#stCoAuthEnv").value.trim();
  if(env){ L.push("auth:","  env: "+yq(env),
    "  header: "+yq($("#stCoAuthHeader").value.trim()||"Authorization: Bearer {value}")); }
  L.push("request:","  method: "+$("#stCoMethod").value,
         "  path: "+yq($("#stCoPath").value.trim()||"/"));
  const ps=connParams();
  if(ps.length){
    L.push("params:");
    for(const p of ps){
      let inner="type: "+p.type;
      if(p.required) inner+=", required: true";
      if(p.desc) inner+=", description: "+yq(p.desc);
      L.push("  "+yq(p.name)+": {"+inner+"}");
    }
  }
  L.push("private: "+($("#stCoPrivate").checked?"true":"false"),
         "confirm: "+$("#stCoConfirm").value);
  return L.join("\n")+"\n";
}
function connPreview(){
  $("#stCoYaml").textContent=connFormToYaml();
  const noEnv=!$("#stCoAuthEnv").value.trim();
  $("#stCoAuthHeader").disabled=noEnv;
  if(noEnv) $("#stCoAuthHeader").value="Authorization: Bearer {value}";
}
function connParamRow(p){
  p=p||{};
  const div=document.createElement("div"); div.className="param-row";
  div.innerHTML=`<input type="text" data-p="name" placeholder="name" value="${esc(p.name||"")}">
    <select data-p="type">${["string","number","boolean"].map(t=>
      `<option ${p.type===t?"selected":""}>${t}</option>`).join("")}</select>
    <label class="muted" style="font-size:12px"><input type="checkbox" data-p="req" ${p.required?"checked":""}> required</label>
    <input type="text" data-p="desc" class="pdesc" placeholder="description" value="${esc(p.desc||"")}">
    <button class="danger">remove</button>`;
  div.querySelector("button").onclick=()=>{ div.remove(); connPreview(); };
  div.querySelectorAll("input,select").forEach(el=>el.oninput=connPreview);
  return div;
}
// Tolerant parser for the connector YAML subset (ours + the draft format spec):
// top-level scalars, `auth:`/`request:` blocks, `params:` with flow maps
// ({type: number, required: true}) or nested blocks. Returns null when the
// shape isn't recognizably a connector.
function connYamlToForm(text){
  const unq=s=>{
    s=String(s||"").trim();
    if(s.length>1 && s[0]==='"' && s.endsWith('"')){ try{ return JSON.parse(s); }catch(e){ return s.slice(1,-1); } }
    if(s.length>1 && s[0]==="'" && s.endsWith("'")) return s.slice(1,-1).replace(/''/g,"'");
    return s;
  };
  const splitFlow=s=>{   // split a flow-map body on top-level commas (quote-aware)
    const out=[]; let cur="", q=null;
    for(const ch of s){
      if(q){ cur+=ch; if(ch===q) q=null; }
      else if(ch==='"'||ch==="'"){ q=ch; cur+=ch; }
      else if(ch===","){ out.push(cur); cur=""; }
      else cur+=ch;
    }
    if(cur.trim()) out.push(cur);
    return out;
  };
  const kv=s=>{ const m=s.match(/^([^:]+):\s*([\s\S]*)$/); return m?[m[1].trim(),unq(m[2])]:null; };
  const flow=s=>{
    s=String(s||"").trim();
    if(!s.startsWith("{")||!s.endsWith("}")) return null;
    const out={};
    for(const part of splitFlow(s.slice(1,-1))){ const p=kv(part); if(p) out[p[0]]=p[1]; }
    return out;
  };
  const root={}, params={}; let section=null, curParam=null, paramIndent=null;
  for(const ln of String(text||"").split(/\r?\n/)){
    if(!ln.trim() || ln.trim().startsWith("#")) continue;
    const m=ln.match(/^(\s*)([A-Za-z0-9_.-]+):\s*(.*)$/);
    if(!m) continue;
    const indent=m[1].length, key=m[2], val=m[3];
    if(indent===0){
      section=null; curParam=null; paramIndent=null;
      if(!val.trim()){ section=key; if(key!=="params") root[key]={}; }
      else root[key]=val.trim().startsWith("{")?(flow(val)||unq(val)):unq(val);
    } else if(section==="params"){
      if(paramIndent===null) paramIndent=indent;   // first entry sets the entry level
      if(indent<=paramIndent){                     // param name (flow map or nested block)
        curParam=key; params[key]=val.trim()?(flow(val)||{}):{};
      } else if(curParam){                         // nested param attribute
        params[curParam][key]=unq(val);
      }
    } else if(section && typeof root[section]==="object"){
      root[section][key]=unq(val);
    }
  }
  if(typeof root.base_url!=="string" || !root.base_url) return null;
  const req=(typeof root.request==="object"&&root.request)||{};
  const auth=(typeof root.auth==="object"&&root.auth)||{};
  return {
    desc: typeof root.description==="string"?root.description:"",
    base: root.base_url,
    env: auth.env||"", header: auth.header||"Authorization: Bearer {value}",
    method: String(req.method||"GET").toUpperCase(),
    path: req.path||"/",
    params: Object.entries(params).map(([name,p])=>({
      name,
      type: (p.type==="integer"||p.type==="number")?"number":(p.type==="boolean"?"boolean":"string"),
      required: String(p.required)==="true",
      desc: p.description||"",
    })),
    priv: String(root.private)!=="false",
    confirm: ["auto","always","never"].includes(String(root.confirm))?String(root.confirm):"auto",
  };
}
function connFillForm(f){
  $("#stCoDesc").value=f.desc; $("#stCoBase").value=f.base;
  $("#stCoAuthEnv").value=f.env; $("#stCoAuthHeader").value=f.header;
  $("#stCoMethod").value=["GET","POST","PUT","DELETE"].includes(f.method)?f.method:"GET";
  $("#stCoPath").value=f.path;
  $("#stCoParams").innerHTML="";
  f.params.forEach(p=>$("#stCoParams").appendChild(connParamRow(p)));
  $("#stCoPrivate").checked=f.priv; $("#stCoConfirm").value=f.confirm;
  connPreview();
}
async function editConnector(name){
  _coTarget=name||null;
  studioCloseEditors();
  $("#stConnTitle").textContent=name?("Edit connector: "+name):"New connector";
  $("#stCoName").value=name||""; $("#stCoName").disabled=!!name;
  $("#stCoErr").innerHTML="";
  if(name){
    const r=await api("/api/admin/studio/connector/"+encodeURIComponent(name));
    if(!r.ok){ note($("#stNote"),studioDetail(await r.json().catch(()=>({}))),false); return; }
    const d=await r.json();
    const f=connYamlToForm(d.content);
    if(!f){ showErrors($("#stCoErr"),["could not parse the stored YAML into the form — edit on disk"]); connFillForm({desc:"",base:"",env:"",header:"",method:"GET",path:"/",params:[],priv:true,confirm:"auto"}); }
    else connFillForm(f);
  } else {
    connFillForm({desc:"",base:"",env:"",header:"Authorization: Bearer {value}",method:"GET",path:"/",params:[],priv:true,confirm:"auto"});
  }
  $("#stConnEdit").style.display="block";
  $("#stConnEdit").scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#stNewConn").onclick=()=>editConnector(null);
$("#stCoCancel").onclick=()=>{ $("#stConnEdit").style.display="none"; };
$("#stCoParamAdd").onclick=()=>{ $("#stCoParams").appendChild(connParamRow()); connPreview(); };
["#stCoName","#stCoDesc","#stCoBase","#stCoAuthEnv","#stCoAuthHeader","#stCoMethod",
 "#stCoPath","#stCoPrivate","#stCoConfirm"].forEach(id=>{ $(id).oninput=connPreview; $(id).onchange=connPreview; });
$("#stCoValidate").onclick=async()=>{
  await studioValidate("connector",$("#stCoName").value.trim(),connFormToYaml(),$("#stCoErr"),$("#stCoNote"));
};
$("#stCoSave").onclick=async()=>{
  const name=$("#stCoName").value.trim();
  if(!name){ showErrors($("#stCoErr"),["name is required"]); return; }
  const hard=await studioValidate("connector",name,connFormToYaml(),$("#stCoErr"),$("#stCoNote"));
  if(hard.length) return;
  if(await studioPut("connector",name,connFormToYaml(),$("#stCoErr"),$("#stCoNote"))){
    $("#stConnEdit").style.display="none"; loadStudio();
  }
};
$("#stCoDraft").onclick=()=>openDraft("connector");

// -- python tool editor --
let _tlTarget=null;
function toolScaffold(name){
  return `"""${name} — custom Studio tool. Runs with orchestrator privileges."""

from typing import Any

from runtime.tool_base import Tool, ToolContext, ToolResult


class CustomThing(Tool):
    name = "custom.${name}"
    description = "TODO: what this tool does and when to use it"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "TODO: argument"},
        },
        "required": ["q"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(status="ok", result={"echo": args.get("q")})
`;
}
async function editTool(name){
  _tlTarget=name||null;
  studioCloseEditors();
  $("#stToolTitle").textContent=name?("Edit python tool: "+name):"New python tool";
  $("#stTlName").value=name||""; $("#stTlName").disabled=!!name;
  $("#stTlErr").innerHTML="";
  if(name){
    const r=await api("/api/admin/studio/tool/"+encodeURIComponent(name));
    if(!r.ok){ note($("#stNote"),studioDetail(await r.json().catch(()=>({}))),false); return; }
    $("#stTlCode").value=(await r.json()).content;
  } else {
    $("#stTlCode").value=toolScaffold("my_tool");
  }
  $("#stToolEdit").style.display="block";
  $("#stToolEdit").scrollIntoView({behavior:"smooth",block:"nearest"});
}
$("#stNewTool").onclick=()=>editTool(null);
$("#stTlCancel").onclick=()=>{ $("#stToolEdit").style.display="none"; };
$("#stTlScaffold").onclick=async()=>{
  if($("#stTlCode").value.trim() && !await dlgConfirm("Replace the current code with the scaffold?", {yes:"replace"})) return;
  $("#stTlCode").value=toolScaffold($("#stTlName").value.trim()||"my_tool");
};
$("#stTlValidate").onclick=async()=>{
  await studioValidate("tool",$("#stTlName").value.trim(),$("#stTlCode").value,$("#stTlErr"),$("#stTlNote"));
};
$("#stTlSave").onclick=async()=>{
  const name=$("#stTlName").value.trim();
  if(!name){ showErrors($("#stTlErr"),["name is required"]); return; }
  const hard=await studioValidate("tool",name,$("#stTlCode").value,$("#stTlErr"),$("#stTlNote"));
  if(hard.length) return;
  if(await studioPut("tool",name,$("#stTlCode").value,$("#stTlErr"),$("#stTlNote"))){
    $("#stToolEdit").style.display="none"; loadStudio();
  }
};
$("#stTlDraft").onclick=()=>openDraft("tool");

// -- Draft with AI (local model; fills the editor, never saves) --
let _draftKind=null, _draftNoteTimer=null;
function draftNote(msg, ok, hold){   // like note(), but "drafting…" isn't auto-cleared
  const el=$("#stDraftNote");
  if(_draftNoteTimer){ clearTimeout(_draftNoteTimer); _draftNoteTimer=null; }
  el.textContent=msg; el.className="note "+(ok?"ok":"err");
  if(msg && !hold) _draftNoteTimer=setTimeout(()=>{ el.textContent=""; },4000);
}
function openDraft(kind){
  _draftKind=kind;
  $("#stDraftTitle").textContent="Draft with AI — "+kind;
  $("#stDraftBrief").value="";
  $("#stDraftNote").textContent="";
  $("#stDraftRawWrap").style.display="none";
  $("#stDraftGo").disabled=false;
  $("#stDraftOvl").hidden=false;
  $("#stDraftBrief").focus();
}
$("#stDraftCancel").onclick=()=>{ $("#stDraftOvl").hidden=true; };
$("#stDraftOvl").onclick=e=>{ if(e.target.id==="stDraftOvl") $("#stDraftOvl").hidden=true; };
$("#stDraftGo").onclick=async()=>{
  const desc=$("#stDraftBrief").value.trim();
  if(!desc){ draftNote("describe it first",false); return; }
  $("#stDraftGo").disabled=true;
  $("#stDraftRawWrap").style.display="none";
  draftNote("drafting… (local model, a few seconds)",true,true);
  try{
    if(_draftKind==="eval"){
      const r=await api("/api/admin/evals/draft",{method:"POST",
        headers:{"content-type":"application/json"},
        body:JSON.stringify({prompt:desc})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){ draftNote(studioDetail(j),false); return; }
      $("#evYaml").value=j.yaml||"";
      showErrors($("#evErr"),j.errors||[]);
      if(j.ok===false) note($("#evNote"),"draft needs fixes — see errors",false);
      $("#stDraftOvl").hidden=true;
      return;
    }
    const r=await api("/api/admin/studio/draft",{method:"POST",
      headers:{"content-type":"application/json"},
      body:JSON.stringify({kind:_draftKind,description:desc})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ draftNote(studioDetail(j),false); return; }
    const draft=j.draft||"";
    if(_draftKind==="skill"){
      const p=parseSkillMd(draft);
      if(!$("#stSkDesc").value.trim() && p.desc) $("#stSkDesc").value=p.desc;
      $("#stSkBody").value=p.body||draft; skShowRendered();
    } else if(_draftKind==="chain"){
      $("#stChYaml").value=draft;
    } else if(_draftKind==="tool"){
      $("#stTlCode").value=draft;
    } else if(_draftKind==="connector"){
      const f=connYamlToForm(draft);
      if(!f){   // unparseable — keep the raw draft visible so nothing is lost
        $("#stDraftRaw").textContent=draft;
        $("#stDraftRawWrap").style.display="block";
        draftNote("draft ready — copy it manually",true,true);
        return;
      }
      connFillForm(f);
    }
    $("#stDraftOvl").hidden=true;
  } finally {
    $("#stDraftGo").disabled=false;
  }
};

// ---- Eval harness (cases, runs, results, proposals) ----
let _eval=null, _evSel=null, _evMode="new", _evTimer=null, _evChecked=new Set();
const EV_SCAFFOLD=`id: my-case
name: short human title
tags: [smoke]
driver: scripted
turns:
  - user: "hello"
judge_rubric: |
  Pass if the answer is friendly and correct.
`;
async function loadEval(){
  _eval=await (await api("/api/admin/evals")).json();
  renderEval(); loadEvalResults(); loadEvalProposals(); pollEvalStatus();
  loadEvalSchedules();
  evalHandover();
}
// -- scheduled runs --
async function loadEvalSchedules(){
  const j=await (await api("/api/admin/evals/schedules")).json();
  $("#evSchedRows").innerHTML=(j.schedules||[]).map(s=>{
    const last=s.last_fired?new Date(s.last_fired*1000).toLocaleString():"never";
    return `<tr><td>${esc(s.selector)}</td><td>${(s.every_s/3600)}h</td><td class="muted">${esc(last)}</td>
      <td><input type="checkbox" data-sid="${s.id}" class="evSchedTgl" ${s.enabled?"checked":""}></td>
      <td><button class="danger evSchedDel" data-sid="${s.id}">delete</button></td></tr>`;
  }).join("")||`<tr><td colspan="5" class="muted">no schedules</td></tr>`;
  $("#evSchedRows").querySelectorAll(".evSchedTgl").forEach(t=>t.onchange=async()=>{
    await api("/api/admin/evals/schedules/"+t.dataset.sid,{method:"PUT",
      headers:{"content-type":"application/json"},body:JSON.stringify({enabled:t.checked})});
  });
  $("#evSchedRows").querySelectorAll(".evSchedDel").forEach(b=>b.onclick=async()=>{
    if(!await dlgConfirm("Delete this eval schedule?",{yes:"delete"})) return;
    await api("/api/admin/evals/schedules/"+b.dataset.sid,{method:"DELETE"});
    loadEvalSchedules();
  });
}
$("#evSchedAdd").onclick=async()=>{
  const sel=$("#evSchedSel").value.trim(), h=parseFloat($("#evSchedHours").value);
  if(!sel||!(h>=1&&h<=720)){ note($("#evSchedNote"),"selector + 1-720 hours",false); return; }
  const r=await api("/api/admin/evals/schedules",{method:"POST",
    headers:{"content-type":"application/json"},
    body:JSON.stringify({selector:sel,every_hours:h})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evSchedNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
  note($("#evSchedNote"),"scheduled "+j.cases+" case(s) every "+h+"h",true);
  $("#evSchedSel").value="";
  loadEvalSchedules();
};
// A flag→test draft handed over from Flagged Chats (prefilled, unsaved).
function evalHandover(){
  const hand=sessionStorage.getItem("evalDraft");
  if(!hand) return;
  sessionStorage.removeItem("evalDraft");
  try{
    const d=JSON.parse(hand);
    _evSel=null;
    evalFill(d.suggested_id||"new-case","new",d.yaml||"");
    showErrors($("#evErr"),d.errors||[]);
    if((d.errors||[]).length) note($("#evNote"),"draft needs fixes — see errors",false);
  }catch(e){}
}
function renderEval(){
  const ids=new Set((_eval.cases||[]).map(c=>c.id));
  _evChecked.forEach(id=>{ if(!ids.has(id)) _evChecked.delete(id); });
  $("#evCaseRows").innerHTML=(_eval.cases||[]).map(c=>{
    const cust=c.origin==="custom";
    const sel=c.id===_evSel;
    const off=c.enabled===false;
    const last=c.last
      ?("<span class='pill "+(c.last.passed?"up":"down")+"'>"+(c.last.passed?"✓":"✗")+
        (c.last.score!=null?" "+c.last.score:"")+"</span>"):"—";
    return "<tr data-ev-sel='"+esc(c.id)+"' title='click to inspect; tick the checkbox to run' style='cursor:pointer"+
      (sel?";background:var(--panel2)":"")+(off?";opacity:.45":"")+"'>"+
      "<td><input type='checkbox' data-ev-chk='"+esc(c.id)+"' "+(_evChecked.has(c.id)?"checked":"")+"></td>"+
      "<td><b>"+esc(c.id)+"</b>"+
      "<div class='muted' style='font-size:11px'>"+esc(c.name||"")+"</div></td>"+
      "<td class='muted' style='font-size:12px'>"+esc((c.tags||[]).join(", "))+"</td>"+
      "<td class='muted' style='font-size:12px'>"+esc(c.driver)+"</td>"+
      "<td>"+originBadge(c.origin)+"</td><td style='white-space:nowrap'>"+last+"</td>"+
      "<td><input type='checkbox' data-ev-on='"+esc(c.id)+"' "+(off?"":"checked")+
        " title='included in run-all / tag / scheduled runs'></td>"+
      "<td style='text-align:right;white-space:nowrap'>"+
        "<button data-ev-experiment='"+esc(c.id)+"' title='Run baseline, invariant, domain and composed arms with isolated overlays; real eval cost'>experiment</button> "+
        "<button data-ev-mine='"+esc(c.id)+"' title='Distill a procedure draft from this case\u2019s pass/fail history (judge model; review before saving — never auto-live)'>⛏</button> "+
        "<button data-ev-open='"+esc(c.id)+"'>"+(cust?"edit":"view")+"</button> "+
        (cust?"<button class='danger' data-ev-del='"+esc(c.id)+"'>delete</button>"
             :"<button data-ev-fork='"+esc(c.id)+"'>duplicate</button>")+
      "</td></tr>";
  }).join("") || '<tr><td colspan="8" class="muted">no eval cases found</td></tr>';
  const wire=(attr,fn)=>document.querySelectorAll("#tab-eval ["+attr+"]").forEach(b=>b.onclick=()=>fn(b.getAttribute(attr)));
  wire("data-ev-experiment",startEvalExperiment);
  wire("data-ev-mine",n=>mineProcedure("/api/admin/evals/mine-procedure",{id:n},$("#evTabNote")));
  wire("data-ev-open",n=>openEval(n,"edit"));
  wire("data-ev-fork",n=>openEval(n,"fork"));
  wire("data-ev-del",async n=>{
    if(!await dlgConfirm("Delete custom eval case '"+n+"'?",{yes:"delete"})) return;
    const r=await api("/api/admin/evals/"+encodeURIComponent(n),{method:"DELETE"});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ note($("#evTabNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
    _evChecked.delete(n); if(_evSel===n) _evSel=null;
    note($("#evTabNote"),"deleted '"+n+"'",true); loadEval();
  });
  document.querySelectorAll("#tab-eval [data-ev-chk]").forEach(b=>b.onchange=()=>{
    const id=b.getAttribute("data-ev-chk");
    if(b.checked) _evChecked.add(id); else _evChecked.delete(id);
    evRunSelLabel();
  });
  document.querySelectorAll("#tab-eval [data-ev-on]").forEach(b=>b.onchange=async()=>{
    const id=b.getAttribute("data-ev-on");
    const r=await api("/api/admin/evals/"+encodeURIComponent(id)+"/enabled",{method:"PUT",
      headers:{"content-type":"application/json"},body:JSON.stringify({enabled:b.checked})});
    if(!r.ok){ b.checked=!b.checked; note($("#evTabNote"),"toggle failed",false); return; }
    const c=(_eval.cases||[]).find(x=>x.id===id); if(c) c.enabled=b.checked;
    renderEval();
    note($("#evTabNote"),(b.checked?"enabled ":"disabled ")+"'"+id+"'"+
      (b.checked?"":" — skipped by run-all / tag / scheduled runs"),true);
  });
  // Row click (outside the buttons/checkboxes) toggles the inspected case.
  document.querySelectorAll("#tab-eval [data-ev-sel]").forEach(r=>r.onclick=(e)=>{
    if(e.target.closest("button,input")) return;
    const id=r.getAttribute("data-ev-sel");
    _evSel=(_evSel===id)?null:id; renderEval();
  });
  evRunSelLabel();
}
function evRunSelLabel(){
  const n=_evChecked.size;
  $("#evRunSel").textContent=n?("Run selected ("+n+")"):"Run selected";
}
$("#evChkAll").onclick=()=>{
  const on=$("#evChkAll").checked;
  _evChecked=on?new Set((_eval.cases||[]).map(c=>c.id)):new Set();
  renderEval();
};
$("#evRefresh").onclick=loadEval;

// -- case editor --
function evShowEdit(show){
  $("#evEdit").style.display=show?"block":"none";
  $("#evCasesCols").classList.toggle("editing",!!show);
}
function evalFill(id, mode, y){
  _evMode=mode;
  $("#evEditTitle").textContent=mode==="new"?"New eval case":
    mode==="view"?("View case: "+id+" (builtin, read-only)"):
    mode==="fork"?("Duplicate to custom: "+id):("Edit case: "+id);
  $("#evId").value=id||"";
  $("#evId").disabled=mode!=="new";
  $("#evYaml").value=y;
  $("#evYaml").disabled=mode==="view";
  $("#evErr").innerHTML="";
  const ro=mode==="view";
  ["#evDraft","#evValidate","#evSave"].forEach(x=>$(x).style.display=ro?"none":"");
  $("#evDelete").style.display=mode==="edit"?"":"none";
  $("#evExport").style.display=mode==="new"?"none":"";
  evShowEdit(true);
  $("#evEdit").scrollIntoView({behavior:"smooth",block:"nearest"});
}
async function openEval(id, mode){
  _evSel=id; renderEval();
  const r=await api("/api/admin/evals/"+encodeURIComponent(id));
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evTabNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
  const m=(mode==="edit"&&j.origin==="builtin")?"view":mode;
  evalFill(j.id||id, m, j.yaml||"");
}
$("#evNew").onclick=()=>{ _evSel=null; renderEval(); evalFill("","new",EV_SCAFFOLD); };
$("#evCancel").onclick=()=>{ evShowEdit(false); };
$("#evValidate").onclick=async()=>{
  const r=await api("/api/admin/evals/validate",{method:"POST",
    headers:{"content-type":"application/json"},
    body:JSON.stringify({yaml:$("#evYaml").value})});
  const j=await r.json();
  showErrors($("#evErr"),j.errors||[]);
  note($("#evNote"),j.ok?"ok":"invalid",!!j.ok);
};
$("#evSave").onclick=async()=>{
  const id=$("#evId").value.trim();
  if(!id){ showErrors($("#evErr"),["id is required"]); return; }
  const r=await api("/api/admin/evals/"+encodeURIComponent(id),{method:"PUT",
    headers:{"content-type":"application/json"},body:JSON.stringify({yaml:$("#evYaml").value})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ showErrors($("#evErr"),(j.detail&&j.detail.errors)||[studioDetail(j)]); return; }
  _evSel=id;
  evShowEdit(false);
  note($("#evTabNote"),"saved '"+id+"'",true);
  loadEval();
};
$("#evDelete").onclick=async()=>{
  const id=$("#evId").value.trim();
  if(!await dlgConfirm("Delete custom eval case '"+id+"'?", {yes:"delete"})) return;
  const r=await api("/api/admin/evals/"+encodeURIComponent(id),{method:"DELETE"});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ await dlgAlert(typeof j.detail==="string"?j.detail:"error"); return; }
  if(_evSel===id) _evSel=null;
  evShowEdit(false);
  note($("#evTabNote"),"deleted '"+id+"'",true);
  loadEval();
};
$("#evExport").onclick=()=>{
  const id=$("#evId").value.trim();
  if(id) window.location="/api/admin/studio/export/eval/"+encodeURIComponent(id);
};
$("#evDraft").onclick=()=>openDraft("eval");

// -- run bar + status poll --
async function evalStart(body){
  const r=await api("/api/admin/evals/run",{method:"POST",
    headers:{"content-type":"application/json"},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evRunNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
  note($("#evRunNote"),"started "+j.cases+" case(s)",true);
  pollEvalStatus();
}
$("#evRunSel").onclick=()=>{
  if(_evChecked.size){ evalStart({ids:[..._evChecked]}); return; }
  if(!_evSel){ note($("#evRunNote"),"tick one or more cases first",false); return; }
  evalStart({id:_evSel});
};
$("#evRunTagBtn").onclick=()=>{
  const t=$("#evRunTag").value.trim();
  if(!t){ note($("#evRunNote"),"enter a tag",false); return; }
  evalStart({tag:t});
};
$("#evRunDelta").onclick=async()=>{
  const n=((_eval&&_eval.cases)||[]).filter(c=>c.enabled!==false).length;
  if(!n){ note($("#evRunNote"),"no enabled eval cases",false); return; }
  if(!await dlgConfirm("Run the delta suite? Cases that passed their last 3 runs "+
      "are skipped, except a random 10% spot-check — the routine-regression run.",
      {yes:"run delta"})) return;
  evalStart({all:true,skip_stable:true});
};
$("#evRunAll").onclick=async()=>{
  const n=((_eval&&_eval.cases)||[]).filter(c=>c.enabled!==false).length;
  if(!n){ note($("#evRunNote"),"no enabled eval cases",false); return; }
  if(!await dlgConfirm("Run all "+n+" enabled eval cases? Every case plays through the "+
      "live agent loop and is graded by the judge — this is the most expensive run.",
      {yes:"run all"})) return;
  evalStart({all:true});
};
function renderEvalStatus(d){
  $("#evRunStatus").textContent=d.running
    ?("running… current case: "+(d.current||"—")+(d.cancelling?" — cancelling, stops after this case":"")):"";
  $("#evRunCancel").style.display=d.running&&!d.cancelling?"":"none";
  $("#evRunLast").textContent=(!d.running&&d.last)
    ?((d.last.cancelled?"cancelled — ":"")+(d.last.benchmark
      ?("last benchmark: "+(d.last.suites||0)+" suites · "+(d.last.passed||0)+" passed · "+
        (d.last.failed||0)+" failed · $"+(d.last.cost_usd||0).toFixed(4))
      :("last suite: "+(d.last.ran||0)+" ran · "+(d.last.passed||0)+" passed · "+
        (d.last.failed||0)+" failed"+(d.last.invalid?(" · "+d.last.invalid+" invalid (fallback)"):"")+" · $"+(d.last.cost_usd||0).toFixed(4)))):"";
}
$("#evRunCancel").onclick=async()=>{
  const r=await api("/api/admin/evals/cancel",{method:"POST"});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evRunNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
  note($("#evRunNote"),"cancel requested — the current case finishes, the rest is skipped",true);
  pollEvalStatus();
};
$("#evJudgeCal").onclick=async()=>{
  const out=$("#evJudgeCalOut");
  out.textContent="grading 10 frozen transcripts with the current judge…";
  try{
    const r=await api("/api/admin/evals/judge-calibration",{method:"POST"});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ out.textContent="error: "+(typeof j.detail==="string"?j.detail:r.status); return; }
    const pct=j.agreement==null?"—":Math.round(j.agreement*100)+"%";
    out.textContent="agreement: "+j.agree+"/"+j.n+" ("+pct+") · judge: "+(j.judge_model||"?")
      +" · $"+(j.cost_usd||0).toFixed(4)+"\n"
      +(j.pairs||[]).map(p=>(p.agree?"✓ ":"✗ ")+p.id+" — expected "+(p.expected?"pass":"fail")
        +", got "+(p.got?"pass":"fail")+(p.agree?"":(" — "+(p.notes||"")))).join("\n");
  }catch(e){ out.textContent="error: "+e; }
};
async function pollEvalStatus(){
  try{
    const d=await (await api("/api/admin/evals/run-status")).json();
    const wasPolling=!!_evTimer;
    renderEvalStatus(d);
    if(d.running && !_evTimer) _evTimer=setInterval(pollEvalStatus,3000);
    if(!d.running && _evTimer){
      clearInterval(_evTimer); _evTimer=null;
      if(wasPolling) loadEval();   // a suite just finished — refresh everything
    }
  }catch(e){ if(_evTimer){ clearInterval(_evTimer); _evTimer=null; } }
}

// -- results + trend --
async function loadEvalResults(){
  const q=_evSel?("?test_id="+encodeURIComponent(_evSel)):"";
  const d=await (await api("/api/admin/evals/results"+q)).json();
  $("#evResultRows").innerHTML=(d.results||[]).map(r=>
    "<tr><td class='muted'>"+new Date(r.ts*1000).toLocaleString()+"</td>"+
    "<td>"+esc(r.test_id)+"</td>"+
    "<td>"+(r.passed?"<span class='pill st up'>pass</span>":"<span class='pill st down'>fail</span>")+"</td>"+
    "<td>"+(r.score!=null?r.score:"—")+"</td>"+
    "<td class='muted' style='max-width:420px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' title='"+esc(r.judge_notes||"")+"'>"+esc((r.judge_notes||"").slice(0,120))+"</td>"+
    "<td class='muted'>$"+(r.cost_usd||0).toFixed(4)+"</td></tr>"
  ).join("") || '<tr><td colspan="6" class="muted">no results yet</td></tr>';
  loadEvalTrend();
}
async function loadEvalTrend(){
  const el=$("#evTrend");
  if(!_evSel){ el.textContent="select a case to see its pass-rate trend"; return; }
  const d=await (await api("/api/admin/evals/trend/"+encodeURIComponent(_evSel))).json();
  const t=d.trend||[];
  if(!t.length){ el.textContent="no runs recorded for '"+_evSel+"' yet"; return; }
  const pass=t.filter(x=>x.passed).length;
  el.innerHTML=esc(_evSel)+" · last "+t.length+" runs · "+Math.round(100*pass/t.length)+"% pass · "+
    t.map(x=>x.passed?"<span style='color:var(--ok)'>✓</span>":"<span style='color:var(--err)'>✗</span>").join(" ");
}
$("#evRefreshResults").onclick=loadEvalResults;

// -- proposals inbox --
async function loadEvalProposals(){
  const d=await (await api("/api/admin/evals/proposals?status=pending")).json();
  $("#evPropRows").innerHTML=(d.proposals||[]).map(p=>
    "<tr><td><b>"+esc(p.test_id)+"</b>"+
      "<div class='muted' style='font-size:11px'>"+new Date(p.ts*1000).toLocaleString()+"</div></td>"+
    "<td><span class='pill'>"+esc(p.classification||"?")+"</span>"+
      (p.target?"<div class='muted' style='font-size:11px;margin-top:2px'>→ "+esc(p.target)+"</div>":"")+
      (p.brain_preset?"<div class='muted' style='font-size:11px'>brain: "+esc(p.brain_preset)+
        " · base v"+esc(p.base_overlay_version??0)+"</div>":"")+"</td>"+
    "<td class='muted' style='font-size:12px;white-space:pre-wrap'>"+
      "<b>what:</b> "+esc(p.what||"")+"<br><b>cause:</b> "+esc(p.cause||"")+
      "<br><b>fix:</b> "+esc(p.fix||"")+
      (p.proposed_content?"<br><b>proposed:</b> "+esc(p.proposed_content):"")+"</td>"+
    "<td style='text-align:right;white-space:nowrap'>"+
      "<button data-pa='"+p.id+"'>accept</button> "+
      "<button class='danger' data-pr='"+p.id+"'>reject</button></td></tr>"
  ).join("") || '<tr><td colspan="4" class="muted">no pending proposals — failed evals file them here</td></tr>';
  $("#evPropRows").querySelectorAll("[data-pa]").forEach(b=>b.onclick=async()=>{
    const r=await api("/api/admin/evals/proposals/"+b.dataset.pa+"/accept",{method:"POST"});
    const j=await r.json().catch(()=>({}));
    const el=$("#evPropNote");
    if(!r.ok){ el.className="note err"; el.textContent=typeof j.detail==="string"?j.detail:"error"; return; }
    el.className=j.applied==="none"&&j.note?"note err":"note ok";
    el.textContent=j.applied==="none"?("not applied — "+(j.note||"status only")):
      ("accepted — applied to "+j.applied+": "+j.path);
    loadEvalProposals();
    if(j.applied==="brain-overlay") loadBrainOverlays();
  });
  $("#evPropRows").querySelectorAll("[data-pr]").forEach(b=>b.onclick=async()=>{
    const r=await api("/api/admin/evals/proposals/"+b.dataset.pr+"/reject",{method:"POST"});
    if(r.ok){ note($("#evPropNote"),"rejected",true); loadEvalProposals(); }
  });
}
$("#evRefreshProps").onclick=()=>{ loadEvalProposals(); loadBrainOverlays(); };

// -- versioned per-brain overlays --
let _evOverlayPreset="";
async function loadBrainOverlays(){
  const r=await api("/api/admin/brain-overlays");
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evOverlayNote"),studioDetail(j),false); return; }
  const overlays=j.overlays||[], select=$("#evOverlayPreset");
  select.innerHTML=overlays.map(o=>"<option value='"+esc(o.preset)+"'>"+
    esc(o.preset)+" (v"+esc(o.version)+")</option>").join("");
  if(!overlays.length){ $("#evOverlayRows").innerHTML=
    '<tr><td colspan="5" class="muted">no brain presets</td></tr>'; return; }
  if(!overlays.some(o=>o.preset===_evOverlayPreset))
    _evOverlayPreset=(overlays.find(o=>o.version>0)||overlays[0]).preset;
  select.value=_evOverlayPreset;
  await loadBrainOverlayHistory();
}
async function loadBrainOverlayHistory(){
  if(!_evOverlayPreset) return;
  const r=await api("/api/admin/brain-overlays/"+encodeURIComponent(_evOverlayPreset));
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evOverlayNote"),studioDetail(j),false); return; }
  $("#evOverlayRows").innerHTML=(j.versions||[]).map(v=>{
    const source=v.version===0?"empty baseline":
      v.source_version!=null?"rollback of v"+v.source_version:
      v.proposal_id?"proposal #"+v.proposal_id:"accepted overlay";
    return "<tr><td><b>v"+v.version+"</b> "+(v.active?"<span class='pill'>active</span>":"")+"</td>"+
      "<td>"+esc(source)+(v.experiment_id?"<div class='muted' style='font-size:11px'>"+
        esc(v.experiment_id)+"</div>":"")+"</td>"+
      "<td class='muted'>"+(v.created?new Date(v.created*1000).toLocaleString():"—")+"</td>"+
      "<td><details><summary>inspect</summary><pre style='max-width:520px;white-space:pre-wrap'>"+
        esc(JSON.stringify(v.overlay,null,2))+"</pre></details></td>"+
      "<td style='text-align:right'>"+(v.active?"":"<button data-evo-rollback='"+v.version+"'>restore</button>")+"</td></tr>";
  }).join("") || '<tr><td colspan="5" class="muted">no versions</td></tr>';
  $("#evOverlayRows").querySelectorAll("[data-evo-rollback]").forEach(b=>b.onclick=async()=>{
    if(!confirm("Append a new active version restoring "+_evOverlayPreset+"@v"+b.dataset.evoRollback+"?")) return;
    const r=await api("/api/admin/brain-overlays/"+encodeURIComponent(_evOverlayPreset)+"/rollback",
      {method:"POST",json:{version:Number(b.dataset.evoRollback)}});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ note($("#evOverlayNote"),studioDetail(j),false); return; }
    note($("#evOverlayNote"),"restored as "+j.path,true);
    await loadBrainOverlays();
  });
}
$("#evOverlayPreset").onchange=e=>{ _evOverlayPreset=e.target.value; loadBrainOverlayHistory(); };
$("#evOverlayRefresh").onclick=loadBrainOverlays;

// -- matched harness experiments --
let _evExperiments=[], _evExpDetail=null, _evExpId=null;
let _evExpTimer=null, _evExpVisible=false;

function evExpText(v){
  if(typeof v==="string") return v;
  if(v==null) return "";
  try{ return JSON.stringify(v); }catch(e){ return String(v); }
}
function evExpTime(v){
  if(v==null||v==="") return "—";
  const d=new Date(typeof v==="number"?v*1000:v);
  return isNaN(d.getTime())?String(v):d.toLocaleString();
}
function evExpMoney(v){
  const n=Number(v||0);
  return "$"+(Number.isFinite(n)?n:0).toFixed(4);
}
function evExpSummary(s,detail){
  s=s||{};
  if(s.runs==null&&s.passed==null&&s.pass_rate==null) return "—";
  const counted=s.counted!=null?s.counted:(s.ran!=null?s.ran:(s.runs||0));
  let out=String(s.passed||0)+"/"+String(counted)+" passed";
  if(s.pass_rate!=null) out+=" · "+(Number(s.pass_rate)*100).toFixed(detail?1:0)+"%";
  if(s.avg_score!=null) out+=" · avg score "+Number(s.avg_score).toFixed(2);
  if(detail) out+=" · "+(s.runs||0)+" runs · "+evExpMoney(s.cost_usd)+
    " · "+Number(s.tokens||0).toLocaleString()+" tokens · "+Number(s.elapsed_s||0).toFixed(1)+"s";
  return out;
}
function evExpStatus(status){
  const cls=status==="done"?"up":(status==="error"||status==="cancelled")?"down":"";
  return "<span class='pill st "+cls+"'>"+esc(status||"unknown")+"</span>";
}
function evExpComparison(exp){
  const b=(exp.baseline||{}).summary||{}, c=(exp.composed||{}).summary||{};
  const delta=k=>(b[k]==null||c[k]==null)?null:Number(c[k])-Number(b[k]);
  const fmt=(v,fn)=>v==null?"—":(v>0?"+":"")+fn(v);
  return "pass rate "+fmt(delta("pass_rate"),v=>(v*100).toFixed(1)+" pp")+
    " · avg score "+fmt(delta("avg_score"),v=>v.toFixed(2))+
    " · tokens "+fmt(delta("tokens"),v=>Math.round(v).toLocaleString())+
    " · elapsed "+fmt(delta("elapsed_s"),v=>v.toFixed(1)+"s");
}
function renderEvalExperimentArm(label,arm){
  let h="<details open style='border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-top:10px'>"+
    "<summary style='cursor:pointer'><b>"+esc(label)+"</b> <span class='muted'>"+
    esc(arm?evExpSummary(arm.summary,true):"not generated yet")+"</span></summary>";
  if(!arm) return h+"</details>";
  if(arm.rationale) h+="<p style='font-size:12px;white-space:pre-wrap'><b>Rationale</b><br>"+
    esc(arm.rationale)+"</p>";
  h+="<div class='muted' style='font-size:11px;margin-top:8px'>Overlay</div>"+
    "<pre>"+esc(JSON.stringify(arm.overlay||{},null,2))+"</pre>"+
    "<div class='muted' style='font-size:11px'>Run evidence</div>";
  const runs=arm.runs||[];
  if(!runs.length) h+="<p class='muted' style='font-size:12px'>No runs yet.</p>";
  else h+=runs.map((r,i)=>{
    const verdict=r.passed===true?"pass":r.passed===false?"fail":"—";
    const cls=r.passed===true?"up":r.passed===false?"down":"";
    const meta=["run "+(i+1)];
    if(r.status) meta.push(String(r.status));
    if(r.score!=null) meta.push("score "+Number(r.score).toFixed(2));
    meta.push(evExpMoney(r.cost_usd),Number(r.tokens||0).toLocaleString()+" tokens",
      Number(r.elapsed_s||0).toFixed(1)+"s");
    let out="<div style='border-top:1px solid var(--border);padding:8px 0'>"+
      "<div class='row'><span class='pill "+cls+"'>"+esc(verdict)+"</span>"+
      "<span class='muted' style='font-size:11px'>"+esc(meta.join(" · "))+"</span></div>";
    if(r.judge_notes) out+="<div class='muted' style='font-size:12px;white-space:pre-wrap;margin-top:5px'>"+
      "<b>Judge notes:</b> "+esc(r.judge_notes)+"</div>";
    const failures=Array.isArray(r.check_failures)?r.check_failures:(r.check_failures?[r.check_failures]:[]);
    if(failures.length) out+="<div class='errlist'><b>Check failures</b><br>"+
      esc(failures.map(evExpText).join("\n"))+"</div>";
    if(r.judge_error) out+="<div class='errlist'><b>Judge error:</b> "+esc(r.judge_error)+"</div>";
    if(r.note) out+="<div class='muted' style='font-size:12px;white-space:pre-wrap'>"+esc(r.note)+"</div>";
    return out+"</div>";
  }).join("");
  return h+"</details>";
}
function renderEvalExperimentDetail(exp){
  const section=$("#evExpDetailSection"), out=$("#evExpDetail");
  section.style.display="";
  if(!exp){ out.innerHTML="<p class='muted'>Loading experiment…</p>"; return; }
  const total=exp.total||{}, info=exp.case||{}, ids=exp.proposal_ids||[];
  const overlay=(exp.composed||{}).overlay||{};
  let h="<div class='row'>"+evExpStatus(exp.status)+"<span class='pill'>"+esc(exp.stage||"—")+"</span>"+
    "<b>"+esc(exp.id)+"</b></div>"+
    "<p class='muted' style='font-size:12px;white-space:pre-wrap'><b>"+esc(exp.case_id)+"</b>"+
    (info.name?" — "+esc(info.name):"")+"<br>"+esc(exp.reps)+" reps · created "+esc(evExpTime(exp.created))+
    " · updated "+esc(evExpTime(exp.updated))+"<br>Total "+evExpMoney(total.cost_usd)+" · "+
    esc(Number(total.tokens||0).toLocaleString())+" tokens</p>"+
    "<p style='font-size:12px'><b>Composed vs baseline:</b> "+esc(evExpComparison(exp))+"</p>";
  if(exp.error||exp.status==="cancelled"){
    const msg=(exp.status==="cancelled"?"Experiment cancelled. ":"")+(exp.error||"");
    h+="<div class='errlist'>"+esc(msg)+"</div>";
  }
  const canCreate=exp.status==="done"&&Object.keys(overlay).length>0&&!ids.length;
  if(canCreate) h+="<div class='row' style='margin:10px 0'><button class='primary' data-ev-exp-create>"+
    "Create gated proposal</button><span class='muted' style='font-size:12px'>"+
    "Creates one pending brain overlay; never accepts or applies it.</span></div>";
  if(ids.length) h+="<p style='font-size:12px'><b>Gated proposal IDs:</b> "+
    ids.map(id=>"<code>"+esc(id)+"</code>").join(", ")+
    " <button data-ev-exp-proposals>Review in Proposals</button><br>"+
    "<span class='muted'>Nothing is accepted or applied automatically.</span></p>";
  else if(exp.status==="done"&&!Object.keys(overlay).length)
    h+="<p class='muted' style='font-size:12px'>The composer kept the baseline; there is nothing to propose.</p>";
  const candidates=exp.candidates||{};
  h+=renderEvalExperimentArm("Baseline",exp.baseline||null)+
    renderEvalExperimentArm("Invariant candidate",candidates.invariant||null)+
    renderEvalExperimentArm("Domain candidate",candidates.domain||null)+
    renderEvalExperimentArm("Composed",exp.composed||null);
  out.innerHTML=h;
  const create=out.querySelector("[data-ev-exp-create]");
  if(create) create.onclick=()=>createEvalExperimentProposals(exp.id);
  const review=out.querySelector("[data-ev-exp-proposals]");
  if(review) review.onclick=()=>goTab("eval","proposals");
}
function renderEvalExperiments(){
  $("#evExpRows").innerHTML=_evExperiments.map(e=>{
    const info=e.case||{}, total=e.total||{};
    return "<tr><td style='white-space:nowrap'>"+evExpStatus(e.status)+
      "<div class='muted' style='font-size:11px'>"+esc(e.stage||"—")+"</div></td>"+
      "<td><b>"+esc(e.case_id)+"</b><div class='muted' style='font-size:11px'>"+esc(info.name||"")+"</div></td>"+
      "<td>"+esc(e.reps)+"</td><td style='font-size:12px'>"+esc(evExpSummary(e.baseline))+
      " <span class='muted'>→</span> "+esc(evExpSummary(e.composed))+"</td>"+
      "<td style='white-space:nowrap'>"+evExpMoney(total.cost_usd)+
      "<div class='muted' style='font-size:11px'>"+esc(Number(total.tokens||0).toLocaleString())+" tokens</div></td>"+
      "<td style='text-align:right'><button data-ev-exp-view='"+esc(e.id)+"'>view</button></td></tr>";
  }).join("") || '<tr><td colspan="6" class="muted">no experiments yet — use the experiment action on an eval case</td></tr>';
  $("#evExpRows").querySelectorAll("[data-ev-exp-view]").forEach(b=>
    b.onclick=()=>openEvalExperiment(b.getAttribute("data-ev-exp-view")));
}
function selectEvalExperiment(id){
  _evExpId=id; _evExpDetail=null;
  renderEvalExperimentDetail(null);
}
async function openEvalExperiment(id){
  selectEvalExperiment(id);
  await loadEvalExperimentDetail(id);
  syncEvalExperimentPolling();
}
async function loadEvalExperimentList(){
  try{
    const r=await api("/api/admin/evals/experiments");
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ note($("#evExpNote"),studioDetail(j),false); return; }
    _evExperiments=j.experiments||[];
    renderEvalExperiments();
  }catch(e){ note($("#evExpNote"),"experiment list: "+e.message,false); }
}
async function loadEvalExperimentDetail(id){
  try{
    const r=await api("/api/admin/evals/experiments/"+encodeURIComponent(id));
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ note($("#evExpNote"),studioDetail(j),false); return; }
    if(id!==_evExpId) return;
    _evExpDetail=j.experiment||null;
    renderEvalExperimentDetail(_evExpDetail);
  }catch(e){ note($("#evExpNote"),"experiment detail: "+e.message,false); }
}
function syncEvalExperimentPolling(){
  const running=_evExperiments.some(e=>e.status==="running")||
    !!(_evExpDetail&&_evExpDetail.id===_evExpId&&_evExpDetail.status==="running");
  if(!_evExpVisible||!running){
    if(_evExpTimer){ clearTimeout(_evExpTimer); _evExpTimer=null; }
    return;
  }
  if(_evExpTimer) return;
  _evExpTimer=setTimeout(()=>{
    _evExpTimer=null;
    if(_evExpVisible) refreshEvalExperiments();
  },3000);
}
async function refreshEvalExperiments(){
  if(!_evExpVisible) return;
  const id=_evExpId, jobs=[loadEvalExperimentList()];
  if(id) jobs.push(loadEvalExperimentDetail(id));
  await Promise.all(jobs);
  syncEvalExperimentPolling();
}
function stopEvalExperimentPolling(){
  _evExpVisible=false;
  if(_evExpTimer){ clearTimeout(_evExpTimer); _evExpTimer=null; }
}
async function startEvalExperiment(id){
  const reps=Math.min(3,Math.max(1,parseInt($("#evExpReps").value,10)||1));
  const runs=4*reps;
  if(!await dlgConfirm("Run a matched experiment for '"+id+"' with "+reps+" rep"+(reps===1?"":"s")+
      "? Four arms (baseline, invariant, domain, composed) × "+reps+" = "+runs+
      " real eval runs, plus model calls to propose and compose overlays. This can incur real eval cost. "+
      "Overlays stay isolated and nothing auto-applies.",{yes:"run experiment"})) return;
  try{
    const r=await api("/api/admin/evals/experiments",{method:"POST",
      headers:{"content-type":"application/json"},body:JSON.stringify({id,reps})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ note($("#evTabNote"),studioDetail(j),false); return; }
    const exp=j.experiment||{};
    if(!exp.id){ note($("#evTabNote"),"start response did not include an experiment",false); return; }
    _evExperiments=[exp].concat(_evExperiments.filter(e=>e.id!==exp.id));
    selectEvalExperiment(exp.id);
    goTab("eval","experiments");
    note($("#evExpNote"),"started "+exp.id+" — polling while running",true);
  }catch(e){ note($("#evTabNote"),"experiment start: "+e.message,false); }
}
async function createEvalExperimentProposals(id){
  const button=$("#evExpDetail [data-ev-exp-create]");
  if(button) button.disabled=true;
  try{
    const r=await api("/api/admin/evals/experiments/"+encodeURIComponent(id)+"/proposals",{method:"POST"});
    const j=await r.json().catch(()=>({}));
    if(!r.ok){
      note($("#evExpNote"),studioDetail(j),false);
      if(r.status===409) await refreshEvalExperiments();
      return;
    }
    const ids=(j.proposals||[]).map(p=>p.id).filter(x=>x!=null);
    if(_evExpDetail&&_evExpDetail.id===id){
      _evExpDetail.proposal_ids=ids;
      renderEvalExperimentDetail(_evExpDetail);
    }
    note($("#evExpNote"),"created "+ids.length+" gated proposal"+(ids.length===1?"":"s")+
      " — review in Proposals; nothing was applied",true);
    await refreshEvalExperiments();
  }catch(e){ note($("#evExpNote"),"create proposals: "+e.message,false); }
  finally{ if(button&&button.isConnected) button.disabled=false; }
}
$("#evExpRefresh").onclick=()=>{
  if(_evExpTimer){ clearTimeout(_evExpTimer); _evExpTimer=null; }
  refreshEvalExperiments();
};
$("#evExpClose").onclick=()=>{
  _evExpId=null; _evExpDetail=null;
  $("#evExpDetailSection").style.display="none";
  syncEvalExperimentPolling();
};

// The old in-tab sub-view switcher (Cases | Results | Statistics | Proposals |
// Experiments | Benchmark) is superseded by the Eval subtabs — see tabVisible(),
// which fires the same per-view loaders (results/stats, proposals, experiments,
// benchmark + their poll stops) when the matching subtab becomes visible.

// -- benchmark view --
// Guard-name completion for the ablation input: one shared datalist, filled
// once from the backend (the legal guards_off values — guard registries +
// named dispatch gates).
let _bmGuardsLoaded=false;
async function bmGuardDatalist(){
  if(_bmGuardsLoaded) return;
  _bmGuardsLoaded=true;
  const j=await (await api("/api/admin/evals/guards")).json().catch(()=>({}));
  if(!j.guards||!j.guards.length) return;
  const dl=document.createElement("datalist");
  dl.id="bmGuardNames";
  for(const g of j.guards){ const o=document.createElement("option"); o.value=g; dl.appendChild(o); }
  document.body.appendChild(dl);
}
function evBmVarRow(v){
  bmGuardDatalist();
  const tr=document.createElement("tr");
  tr.innerHTML='<td><input class="bm-label" style="width:180px"></td>'+
    '<td><input class="bm-model" placeholder="(blank = current brain)" style="width:180px"></td>'+
    '<td><select class="bm-harness"><option value="full">full</option><option value="brain">brain only</option></select></td>'+
    '<td><input class="bm-sampling" style="width:220px;font-family:var(--mono)"></td>'+
    '<td><input class="bm-noskills" placeholder="e.g. long-document" title="Comma-separated skill names this variant runs WITHOUT — hidden from the catalog, skill.load refuses. For A/Bs like same brain ± a skill." style="width:150px"></td>'+
    '<td><input class="bm-guards" list="bmGuardNames" placeholder="e.g. just_reply" title="Guard ablation: comma-separated guard names this variant runs WITHOUT (rail off = does the rail pay?). Unknown names are rejected with the full list." style="width:130px"></td>'+
    '<td><input class="bm-toolsonly" placeholder="e.g. code.run" title="Control arm: run with ONLY these tools (comma-separated), intersected with the unattended-safe set. code.run alone = the bash-only control — measures what the JayNet toolset adds over a bare bash agent." style="width:110px"></td>'+
    '<td><input class="bm-basesys" placeholder="prompt name" title="base_system: replace the whole system prompt with prompts/NAME.md (e.g. eval-bash-agent). Pair with tools_only for a clean control arm — the gate prompt advertises tools the arm does not have." style="width:100px"></td>'+
    '<td><input class="bm-reps" type="number" min="1" max="10" style="width:60px"></td>'+
    '<td><button class="bm-del danger">×</button></td>';
  tr.querySelector(".bm-label").value=v&&v.label||"";
  tr.querySelector(".bm-model").value=v&&v.model||"";
  tr.querySelector(".bm-harness").value=(v&&v.harness)||"full";
  tr.querySelector(".bm-sampling").value=v&&v.sampling||'{"temperature": 0, "seed": 42}';
  tr.querySelector(".bm-noskills").value=v&&v.disabled_skills||"";
  tr.querySelector(".bm-guards").value=v&&v.guards_off||"";
  tr.querySelector(".bm-toolsonly").value=v&&v.tools_only||"";
  tr.querySelector(".bm-basesys").value=v&&v.base_system||"";
  tr.querySelector(".bm-reps").value=v&&v.reps||3;
  tr.querySelector(".bm-del").onclick=()=>tr.remove();
  return tr;
}
$("#evBmAddVar").onclick=()=>$("#evBmVarRows").appendChild(evBmVarRow(null));

function evBmCollect(){
  const variants=[];
  for(const tr of $("#evBmVarRows").children){
    const label=tr.querySelector(".bm-label").value.trim();
    if(!label){
      const filled=[".bm-model",".bm-sampling"].some(s=>tr.querySelector(s).value.trim());
      if(filled){ note($("#evBmRunNote"),"a variant row has content but no label",false); return null; }
      continue;
    }
    const s=tr.querySelector(".bm-sampling").value.trim();
    let sampling=null;
    if(s){
      try{ sampling=JSON.parse(s); }
      catch(e){ note($("#evBmRunNote"),"bad sampling JSON on '"+label+"'",false); return null; }
      if(typeof sampling!=="object"||Array.isArray(sampling)||!sampling){
        note($("#evBmRunNote"),"sampling must be an object on '"+label+"'",false); return null; }
    }
    variants.push({label, model:tr.querySelector(".bm-model").value.trim()||null,
                   harness:tr.querySelector(".bm-harness").value,
                   disabled_skills:tr.querySelector(".bm-noskills").value.split(",")
                     .map(x=>x.trim()).filter(Boolean),
                   guards_off:tr.querySelector(".bm-guards").value.split(",")
                     .map(x=>x.trim()).filter(Boolean),
                   tools_only:tr.querySelector(".bm-toolsonly").value.split(",")
                     .map(x=>x.trim()).filter(Boolean),
                   base_system:tr.querySelector(".bm-basesys").value.trim()||null,
                   sampling, reps:parseInt(tr.querySelector(".bm-reps").value,10)||3});
  }
  return variants;
}

let _evBmPoll=null;
async function evBmStatus(){
  const r=await api("/api/admin/evals/run-status"); const j=await r.json();
  $("#evBmStatus").textContent=j.running
    ?("running: "+(j.current||"…")+(j.cancelling?" — cancelling, stops after this case":"")):
    j.last?((j.last.cancelled?"cancelled — ":"")+(j.last.benchmark?
      "last benchmark: "+j.last.suites+" suites, "+j.last.passed+" passed, "+j.last.failed+" failed, $"+(j.last.cost_usd||0).toFixed(4):
      "last suite: "+j.last.ran+" ran, "+j.last.passed+" passed, "+j.last.failed+" failed")):"";
  $("#evBmCancel").style.display=j.running&&!j.cancelling?"":"none";
  if(!j.running&&_evBmPoll){ clearInterval(_evBmPoll); _evBmPoll=null; loadBenchCompare(); loadStrengthMatrix(); }
}
$("#evBmCancel").onclick=async()=>{
  const r=await api("/api/admin/evals/cancel",{method:"POST"});
  if(r.ok){ note($("#evBmRunNote"),"cancel requested — stops after the current case",true); evBmStatus(); }
};
$("#evBmRun").onclick=async()=>{
  const variants=evBmCollect();
  if(!variants||!variants.length){ note($("#evBmRunNote"),"add at least one variant",false); return; }
  const body={variants};
  const id=$("#evBmCase").value.trim(), tag=$("#evBmTag").value.trim();
  if(id) body.id=id; else if(tag) body.tag=tag;
  else { note($("#evBmRunNote"),"pick a tag or a case id",false); return; }
  const r=await api("/api/admin/evals/benchmark/run",{method:"POST",
    headers:{"content-type":"application/json"},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evBmRunNote"),studioDetail(j),false); return; }
  note($("#evBmRunNote"),"started: "+j.runs+" runs ("+j.cases+" cases × "+j.variants+" variants)",true);
  if(!_evBmPoll) _evBmPoll=setInterval(evBmStatus,4000);
  evBmStatus();
};

// Benchmark label filtering: brain labels that map to a CURRENT catalog
// preset (name/alias/served_id, dash/underscore-insensitive) show by default;
// old brains and harness variants (guards_off, …) hide behind show-archived.
let _bmCurrent=null, _bmShowArchived=false;
function _bmNorm(s){ return String(s).replace(/[-_]/g,"").toLowerCase(); }
function _bmCur(label){ return _bmCurrent?_bmCurrent.has(_bmNorm(label)):true; }
document.querySelectorAll(".evBmArchived").forEach(cb=>cb.onchange=()=>{
  _bmShowArchived=cb.checked;
  document.querySelectorAll(".evBmArchived").forEach(x=>x.checked=cb.checked);
  loadBenchCompare(); loadStrengthMatrix();
});
async function loadBenchCompare(){
  const keep=[...$("#evBmPick").querySelectorAll("input:checked")].map(i=>i.value);
  const r=await api("/api/admin/evals/benchmark/brains"); const j=await r.json();
  _bmCurrent=new Set(j.current_labels||[]);
  const pick=$("#evBmPick"); pick.innerHTML="";
  const all=j.brains||[];
  const brains=_bmShowArchived?all:all.filter(_bmCur);
  if(!all.length){ $("#evBmEmpty").textContent="no results recorded yet"; return; }
  brains.forEach(b=>{
    const l=document.createElement("label");
    l.style.cssText="font-size:12px;display:flex;gap:4px;align-items:center";
    l.innerHTML='<input type="checkbox" value="'+esc(b)+'"> '+esc(b);
    l.querySelector("input").checked=keep.includes(b);
    l.querySelector("input").onchange=evBmCompare;
    pick.appendChild(l);
  });
  const hidden=all.length-brains.length;
  if(hidden>0) pick.insertAdjacentHTML("beforeend",
    `<span class="muted" style="font-size:11px">(${hidden} archived hidden)</span>`);
}
async function evBmCompare(){
  const sel=[...$("#evBmPick").querySelectorAll("input:checked")].map(i=>i.value);
  const tbl=$("#evBmTable"), empty=$("#evBmEmpty");
  if(sel.length<2){ tbl.style.display="none";
    empty.textContent=sel.length?"pick at least two variants":"";
    return; }
  const r=await api("/api/admin/evals/benchmark/compare?brains="+encodeURIComponent(sel.join(",")));
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ empty.textContent=studioDetail(j); return; }
  const rows=j.cases||[];
  $("#evBmHead").innerHTML="<th>Case</th>"+sel.map(b=>"<th>"+esc(b)+"</th>").join("");
  const cell=c=>c?((c.pass_rate*100).toFixed(0)+"% · "+(c.avg_score==null?"–":c.avg_score.toFixed(1))+
    '<br><span class="muted" style="font-size:11px">'+c.runs+" runs · $"+c.avg_cost_usd.toFixed(4)+
    " · "+c.avg_elapsed_s.toFixed(0)+"s · "+(Math.round((c.avg_tokens||0)/100)/10)+"k tok · "+
    Math.round((c.delegation_rate||0)*100)+"% deleg"+
    (c.avg_cache_hit_ratio==null?"":" · "+Math.round(c.avg_cache_hit_ratio*100)+"% cache")+
    "</span>"):'<span class="muted">—</span>';
  // overall row: mean pass rate / mean score over the cases each variant ran
  const overalls=sel.map(b=>{
    const cs=rows.map(r=>r.per_brain[b]).filter(Boolean);
    if(!cs.length) return {b,pr:null,sc:null};
    const scs=cs.filter(c=>c.avg_score!=null);
    const chr=cs.filter(c=>c.avg_cache_hit_ratio!=null);
    return {b, pr:cs.reduce((a,c)=>a+c.pass_rate,0)/cs.length,
            sc:scs.length?scs.reduce((a,c)=>a+c.avg_score,0)/scs.length:null,
            tok:cs.reduce((a,c)=>a+(c.avg_tokens||0),0)/cs.length,
            deleg:cs.reduce((a,c)=>a+(c.delegation_rate||0),0)/cs.length,
            cache:chr.length?chr.reduce((a,c)=>a+c.avg_cache_hit_ratio,0)/chr.length:null};
  });
  $("#evBmRows").innerHTML=rows.map(row=>"<tr><td>"+esc(row.test_id)+"</td>"+
    sel.map(b=>"<td>"+cell(row.per_brain[b])+"</td>").join("")+"</tr>").join("");
  $("#evBmRows").innerHTML+="<tr><td><b>overall</b></td>"+overalls.map(o=>
    o.pr==null?"<td>—</td>":"<td><b>"+(o.pr*100).toFixed(0)+"% · "+
    (o.sc==null?"–":o.sc.toFixed(1))+'</b><br><span class="muted" style="font-size:11px">'+
    (Math.round(o.tok/100)/10)+"k tok · "+Math.round(o.deleg*100)+"% deleg"+
    (o.cache==null?"":" · "+Math.round(o.cache*100)+"% cache")+"</span></td>").join("")+"</tr>";
  // Benchmark-informed routing: crown the winner and offer a one-click
  // slot assignment (human-gated — the shootout's point is picking a brain).
  const win=overalls.filter(o=>o.pr!=null)
    .sort((x,y)=>(y.pr-x.pr)||((y.sc||0)-(x.sc||0)))[0];
  const wdiv=$("#evBmWinner");
  if(!win||!rows.length){ wdiv.style.display="none"; }
  else{
    wdiv.style.display="";
    wdiv.innerHTML="★ <b>"+esc(win.b)+"</b> leads ("+(win.pr*100).toFixed(0)+"%"+
      (win.sc!=null?" · "+win.sc.toFixed(1):"")+") — route it: preset "+
      '<select id="evBmWinPreset"></select> → slot <select id="evBmWinSlot"></select> '+
      '<button id="evBmWinApply">assign</button> <span class="note" id="evBmWinNote"></span>';
    if(!_presets) _presets=await (await api("/api/admin/presets")).json();
    const ps=_presets.presets||[];
    // best-effort preselect: the preset whose litellm alias IS the winner label
    const match=ps.find(p=>p.alias===win.b);
    $("#evBmWinPreset").innerHTML=ps.map(p=>'<option value="'+esc(p.name)+'"'+
      (match&&p.name===match.name?" selected":"")+">"+esc(p.name)+
      (p.alias?" · "+esc(p.alias):"")+"</option>").join("");
    $("#evBmWinSlot").innerHTML=(_presets.slot_names||[])
      .map(s=>"<option>"+esc(s)+"</option>").join("");
    $("#evBmWinApply").onclick=async()=>{
      const r2=await api("/api/admin/preset-slots",{method:"PUT",
        headers:{"content-type":"application/json"},
        body:JSON.stringify({updates:{[$("#evBmWinSlot").value]:$("#evBmWinPreset").value}})});
      const j2=await r2.json().catch(()=>({}));
      note($("#evBmWinNote"),
        r2.ok?"assigned — restart the process to apply"
             :(typeof j2.detail==="string"?j2.detail:"error"), r2.ok);
      if(r2.ok) _presets=await (await api("/api/admin/presets")).json();
    };
  }
  empty.textContent=rows.length?"":"no recorded runs for the picked variants";
  tbl.style.display=rows.length?"":"none";
}
$("#evBmRefresh").onclick=loadBenchCompare;
async function loadStrengthMatrix(){
  const r=await api("/api/admin/evals/strength-matrix"); const j=await r.json().catch(()=>({}));
  if(!_bmCurrent){ const bj=await (await api("/api/admin/evals/benchmark/brains")).json().catch(()=>({}));
    _bmCurrent=new Set(bj.current_labels||[]); }
  const allCells=j.cells||[];
  const cells=allCells.filter(c=>_bmShowArchived||_bmCur(c.brain));
  const tbl=$("#evSmTable"), empty=$("#evSmEmpty");
  if(!cells.length){ tbl.style.display="none";
    empty.textContent=allCells.length?"all recorded labels are archived — tick 'show archived'":"no data yet — run a benchmark to fill the matrix";
    empty.style.display=""; return; }
  empty.style.display="none";
  const brains=[...new Set(cells.map(c=>c.brain))].sort();
  // Columns: every DEFINED strength (the endpoint's registry list) plus any
  // measured strength no longer registered — unmeasured ones render as empty
  // columns, so the matrix doubles as a coverage map for the vocabulary.
  const strengths=[...new Set((j.registry||[]).concat(cells.map(c=>c.strength)))].sort();
  const at=(s,b)=>cells.find(c=>c.strength===s&&c.brain===b);
  // Models downwards, strengths across (too many brains for columns) —
  // and per strength column the top-3 brains get decreasing green shades.
  const shade=["rgba(120,190,90,.30)","rgba(120,190,90,.18)","rgba(120,190,90,.09)"];
  const podium={};
  strengths.forEach(s=>{
    const ranked=cells.filter(c=>c.strength===s&&c.pass_rate>0)
      .sort((a,b)=>b.pass_rate-a.pass_rate||b.runs-a.runs).slice(0,3);
    ranked.forEach((c,i)=>{ podium[c.brain+"|"+s]=shade[i]; });
  });
  let h="<thead><tr><th>brain</th>"+strengths.map(s=>"<th>"+esc(s)+"</th>").join("")+"</tr></thead><tbody>";
  brains.forEach(b=>{
    h+="<tr><td><b>"+esc(b)+"</b></td>"+strengths.map(s=>{
      const c=at(s,b);
      if(!c) return "<td class='muted'>·</td>";
      const pct=Math.round(c.pass_rate*100);
      const bg=podium[b+"|"+s];
      return `<td${bg?` style="background:${bg}"`:""} title='${c.runs} run(s)'>${pct}% <span class='muted' style='font-size:11px'>(${c.runs})</span></td>`;
    }).join("")+"</tr>";
  });
  tbl.innerHTML=h+"</tbody>"; tbl.style.display="";
}
$("#evSmRefresh").onclick=loadStrengthMatrix;
$("#evTvRun").onclick=async()=>{
  const out=$("#evTvOut");
  out.textContent="reading the trace DB…";
  try{
    const min=Math.max(1,parseInt($("#evTvMin").value||"25",10));
    const j=await (await api("/api/admin/evals/tool-value?min_calls="+min)).json();
    out.textContent=j.ok?j.text:(j.note||"no data");
  }catch(e){ out.textContent="error: "+e; }
};
function loadBench(){
  if(!$("#evBmVarRows").children.length) $("#evBmVarRows").appendChild(evBmVarRow(null));
  loadBenchCompare(); loadStrengthMatrix(); evBmStatus();
}

// -- statistics view --
let _evDays=30, _evStatOpen=null, _evBrain="";
async function loadEvalStatsBrains(){
  const d=await (await api("/api/admin/evals/benchmark/brains")).json().catch(()=>({}));
  const sel=$("#evStatsBrain");
  sel.innerHTML='<option value="">default (live runs)</option>'+
    (d.brains||[]).map(b=>'<option value="'+esc(b)+'">'+esc(b)+'</option>').join("");
  sel.value=_evBrain;
  if(sel.value!==_evBrain){ _evBrain=""; sel.value=""; }   // label vanished
}
$("#evStatsBrain").onchange=()=>{ _evBrain=$("#evStatsBrain").value; loadEvalStats(); };
function evStatCard(k,v){return '<div class="stat"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>';}
// Small inline-SVG line chart: values 0..100, null = gap. No libraries.
function evSvgChart(width,height,labels,series){
  const pad=26,n=labels.length,w=width-pad-8,h=height-pad-14;
  const x=i=>pad+(n<2?w/2:w*i/(n-1));
  const y=v=>8+h*(1-Math.max(0,Math.min(100,v))/100);
  let s='<svg viewBox="0 0 '+width+' '+height+'" style="width:100%;max-width:'+width+'px;display:block">';
  [0,50,100].forEach(g=>{
    s+='<line x1="'+pad+'" y1="'+y(g)+'" x2="'+(width-8)+'" y2="'+y(g)+'" stroke="var(--border)" stroke-width="1"/>'+
       '<text x="2" y="'+(y(g)+3)+'" fill="var(--muted)" font-size="9">'+g+'</text>';});
  if(n){
    s+='<text x="'+x(0)+'" y="'+(height-2)+'" fill="var(--muted)" font-size="9" text-anchor="middle">'+esc(labels[0])+'</text>';
    if(n>1) s+='<text x="'+x(n-1)+'" y="'+(height-2)+'" fill="var(--muted)" font-size="9" text-anchor="middle">'+esc(labels[n-1])+'</text>';
  }
  series.forEach(se=>{
    let seg=[];
    const flush=()=>{ if(seg.length>1) s+='<polyline points="'+seg.join(" ")+'" fill="none" stroke="'+se.color+'" stroke-width="2"/>'; seg=[]; };
    se.values.forEach((v,i)=>{
      if(v==null){ flush(); return; }
      seg.push(x(i).toFixed(1)+","+y(v).toFixed(1));
      if(se.dots) s+='<circle cx="'+x(i).toFixed(1)+'" cy="'+y(v).toFixed(1)+'" r="2" fill="'+se.color+'"/>';
    });
    flush();
  });
  return s+"</svg>";
}
async function loadEvalStats(){
  const d=await (await api("/api/admin/evals/stats?days="+_evDays+
    (_evBrain?"&brain="+encodeURIComponent(_evBrain):""))).json();
  const k=d.kpis||{}, pcs=d.per_case||[];
  const flaky=pcs.filter(c=>c.runs>1).sort((a,b)=>b.flakiness-a.flakiness)[0];
  $("#evKpis").innerHTML=[
    ["Pass rate",k.pass_rate!=null?Math.round(100*k.pass_rate)+"%":"—"],
    ["Avg score",k.avg_score!=null?k.avg_score.toFixed(1):"—"],
    ["Runs",k.runs||0],
    ["Total cost","$"+(k.total_cost_usd||0).toFixed(4)],
    ["Cost / pass",k.cost_per_pass!=null?"$"+k.cost_per_pass.toFixed(4):"—"],
    ["Flakiest case",flaky?(esc(flaky.test_id)+" · "+Math.round(100*flaky.flakiness)+"%"):"—"],
    ["Judge fallbacks",k.judge_fallbacks||0],
    ["Crashes",k.crashes||0],
  ].map(([a,b])=>evStatCard(a,b)).join("");
  const days=(d.series||[]);
  $("#evSeriesChart").innerHTML=days.length
    ?evSvgChart(720,180,days.map(x=>x.day),[
      {color:"var(--ok)",values:days.map(x=>x.pass_rate!=null?100*x.pass_rate:null)},
      {color:"var(--warn)",values:days.map(x=>x.avg_score!=null?10*x.avg_score:null)}])
    :'<span class="muted" style="font-size:12px">no runs in this window</span>';
  $("#evCaseStatRows").innerHTML=pcs.map(c=>
    "<tr data-evstat='"+esc(c.test_id)+"' style='cursor:pointer'>"+
    "<td><b>"+esc(c.test_id)+"</b></td>"+
    "<td>"+c.runs+"</td>"+
    "<td>"+Math.round(100*c.pass_rate)+"%</td>"+
    "<td>"+(c.avg_score!=null?c.avg_score.toFixed(1):"—")+"</td>"+
    "<td>"+(c.runs>1?Math.round(100*c.flakiness)+"%":"—")+"</td>"+
    "<td class='muted'>$"+(c.avg_cost_usd||0).toFixed(4)+"</td>"+
    "<td class='muted'>"+(c.avg_elapsed_s||0).toFixed(1)+"s</td>"+
    "<td>"+(c.last_passed?"<span style='color:var(--ok)'>✓</span>":"<span style='color:var(--err)'>✗</span>")+"</td>"+
    "<td class='muted' style='font-size:12px'>"+esc((c.brains||[]).join(", ")||"—")+"</td></tr>"
  ).join("") || '<tr><td colspan="9" class="muted">no runs in this window</td></tr>';
  $("#evCaseStatRows").querySelectorAll("[data-evstat]").forEach(tr=>tr.onclick=()=>toggleCaseStatTrend(tr));
}
async function toggleCaseStatTrend(tr){
  const id=tr.getAttribute("data-evstat");
  const next=tr.nextElementSibling;
  if(next&&next.classList.contains("ev-stat-detail")){ next.remove(); return; }   // collapse
  document.querySelectorAll(".ev-stat-detail").forEach(x=>x.remove());
  const d=await (await api("/api/admin/evals/trend/"+encodeURIComponent(id)+
    (_evBrain?"?brain="+encodeURIComponent(_evBrain):""))).json();
  const t=d.trend||[];
  const row=document.createElement("tr");
  row.className="ev-stat-detail";
  row.innerHTML="<td colspan='9' style='background:var(--panel2)'>"+
    "<div class='muted' style='font-size:11px;margin-bottom:4px'>"+esc(id)+" — score ×10 over the last "+t.length+" runs</div>"+
    (t.length?evSvgChart(480,120,t.map(x=>new Date(x.ts*1000).toLocaleDateString()),
      [{color:"var(--warn)",dots:true,values:t.map(x=>x.score!=null?10*x.score:null)}])
     :'<span class="muted" style="font-size:12px">no runs recorded</span>')+"</td>";
  tr.after(row);
}
document.querySelectorAll("#evStatsWin button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("#evStatsWin button").forEach(x=>x.classList.toggle("active",x===b));
  _evDays=parseInt(b.dataset.days,10);
  loadEvalStats();
});
$("#evStatsRefresh").onclick=loadEvalStats;

// -- period comparison --
function _evDelta(v,suffix){
  if(v==null) return "—";
  const s=(v>0?"+":"")+(suffix==="%"?Math.round(100*v)+"%":v.toFixed(1));
  return "<span style='color:var("+(v>0?"--ok":v<0?"--err":"--muted")+")'>"+s+"</span>";
}
$("#evCmpGo").onclick=async()=>{
  const aF=$("#evCmpAFrom").value,aT=$("#evCmpATo").value,bF=$("#evCmpBFrom").value,bT=$("#evCmpBTo").value;
  if(!aF||!aT||!bF||!bT){ note($("#evCmpNote"),"fill all four dates",false); return; }
  const q=new URLSearchParams({a_from:aF,a_to:aT,b_from:bF,b_to:bT});
  const r=await api("/api/admin/evals/compare?"+q);
  const j=await r.json().catch(()=>({}));
  if(!r.ok){ note($("#evCmpNote"),typeof j.detail==="string"?j.detail:"error",false); return; }
  const pct=v=>v==null?"—":Math.round(100*v)+"%";
  $("#evCmpRows").innerHTML=(j.cases||[]).map(c=>
    "<tr><td><b>"+esc(c.test_id)+"</b></td>"+
    "<td>"+c.a_runs+"</td><td>"+pct(c.a_pass_rate)+"</td><td>"+(c.a_avg_score!=null?c.a_avg_score.toFixed(1):"—")+"</td>"+
    "<td>"+c.b_runs+"</td><td>"+pct(c.b_pass_rate)+"</td><td>"+(c.b_avg_score!=null?c.b_avg_score.toFixed(1):"—")+"</td>"+
    "<td>"+_evDelta(c.pass_delta,"%")+"</td><td>"+_evDelta(c.score_delta)+"</td></tr>"
  ).join("") || '<tr><td colspan="9" class="muted">no runs in either window</td></tr>';
};

// Restore the tab from the URL hash on load (F5 stays where you were).
// Supports #<top>/<sub>; old flat-tab hashes redirect via HASH_MAP (goTab's
// replaceState rewrites them to the new form). Unknown/empty hashes fall back
// to Status; names are matched against fixed key sets, so no injection
// surface. goTab reuses the lazy-load guards — already-loaded subtabs just
// re-render.
{
  const raw=location.hash.slice(1);
  const parts=(HASH_MAP[raw]||raw).split("/");
  goTab(parts[0],parts[1]||null);
}
