/* ==================== workspace file explorer + editor ====================
   Owns the files modal (#filesModal) and the editor modal (#editorModal).
   Loaded after app.js; app.js drives it through window.FileUI and injects the
   chat-owned state it needs (active project, conversation id, change hooks)
   via FileUI.init({...}). Generic helpers ($, toast, showModal, fmtSize) are
   app.js globals. */
"use strict";
const FileUI = (() => {

let _deps = { getProject: () => null, ensureCid: () => "", onChanged: () => {} };
const $ = s => document.querySelector(s);
const _proj = () => _deps.getProject() || null;         // {id,name} | null
const _changed = () => { try { _deps.onChanged(); } catch (_) {} };

/* The active filespace: a project, else this chat's scratch workspace. Both
   expose the same {entries:[{path,type,size}]} list + /file CRUD + /mkdir
   + /rename + /download. */
function fsBase() {
  const p = _proj();
  return p ? ("/api/projects/" + p.id) : ("/api/chat-scratch/" + _deps.ensureCid());
}

let fmEntries = [];             // last-loaded flat entries [{path,type,size}]
let fmSel = new Set();          // selected paths (files and/or folders)
let fmCollapsed = new Set();    // collapsed folder paths (kept across renders)
let fmOrder = [];               // visible paths in render order (for range/arrow select)
let fmAnchor = null;            // last-picked path (shift-range anchor)
let fmFilter = "";

// Fetch the list, update the top-bar counter, and re-render the modal if open.
async function refresh() {
  let entries = [];
  try { const r = await fetch(fsBase() + "/files"); if (r.ok) entries = (await r.json()).entries || []; }
  catch (_) { }
  fmEntries = entries;
  const n = entries.filter(e => e.type !== "dir").length;
  const badge = $("#filesCount"); if (badge) { badge.textContent = n; badge.hidden = !(n > 0); }
  const chip = $("#projActive");
  if (chip) chip.title = (_proj() ? ("Project: " + _proj().name) : "No project — files live in this chat")
    + " · " + (n ? (n + " file" + (n === 1 ? "" : "s")) : "no files yet") + " — click to open";
  if (!$("#filesModal").hidden) renderFm();
}

function open() {
  $("#fmWhere").textContent = _proj() ? ("Project · " + _proj().name) : "Current chat";
  $("#filesModal").hidden = false;
  refresh();
}
function close() { $("#filesModal").hidden = true; upHide(); }

function renderFm() {
  const tree = $("#fmTree"), entries = fmEntries;
  const valid = new Set(entries.map(e => e.path));
  for (const p of [...fmSel]) if (!valid.has(p)) fmSel.delete(p);   // prune deleted selections
  fmOrder = [];
  if (!entries.length) {
    tree.innerHTML = "";
    const empty = document.createElement("div"); empty.className = "fm-empty";
    empty.innerHTML = _proj()
      ? "empty — drop files here, or "
      : "files the agent creates in this chat will appear here — or ";
    if (_proj()) {
      const b1 = document.createElement("button"); b1.className = "mini"; b1.textContent = "+ file";
      b1.onclick = () => fmNewFile();
      const b2 = document.createElement("button"); b2.className = "mini"; b2.textContent = "↑ upload";
      b2.onclick = () => $("#fmFileInput").click();
      empty.append(b1, document.createTextNode(" "), b2);
    }
    tree.appendChild(empty);
    syncToolbar(); return;
  }
  // apply search filter (dirs stay so matches keep their context)
  const filtered = fmFilter
    ? entries.filter(e => e.path.toLowerCase().includes(fmFilter) || e.type === "dir")
    : entries;
  const root = { dirs: new Map(), files: [], path: null };
  const dnode = (parts) => {
    let n = root;
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (!n.dirs.has(p)) n.dirs.set(p, { dirs: new Map(), files: [], path: parts.slice(0, i + 1).join("/") });
      n = n.dirs.get(p);
    } return n;
  };
  for (const e of filtered) {
    const parts = e.path.split("/");
    if (e.type === "dir") dnode(parts);
    else (parts.length > 1 ? dnode(parts.slice(0, -1)) : root).files.push(e);
  }
  tree.innerHTML = ""; tree.appendChild(fmDir(root, 0));
  syncToolbar();
}

function fmDir(node, depth) {
  const frag = document.createDocumentFragment();
  for (const name of [...node.dirs.keys()].sort((a, b) => a.localeCompare(b))) {
    const child = node.dirs.get(name), dp = child.path || name, collapsed = fmCollapsed.has(dp);
    frag.appendChild(fmRow(dp, "dir", name, 0, depth, collapsed));
    fmOrder.push(dp);
    const kids = document.createElement("div"); kids.className = "fm-kids" + (collapsed ? " hidden" : "");
    kids.appendChild(fmDir(child, depth + 1));
    frag.appendChild(kids);
  }
  for (const e of node.files.slice().sort((a, b) => a.path.localeCompare(b.path))) {
    frag.appendChild(fmRow(e.path, "file", e.path.split("/").pop(), e.size, depth, false));
    fmOrder.push(e.path);
  }
  const wrap = document.createElement("div"); wrap.appendChild(frag); return wrap;
}

function fmRow(path, type, name, size, depth, collapsed) {
  const row = document.createElement("div");
  row.className = "fm-row fm-" + type + (fmSel.has(path) ? " sel" : "");
  row.dataset.path = path; row.style.paddingLeft = (6 + depth * 15) + "px";
  row.setAttribute("draggable", "true");
  const cb = document.createElement("input"); cb.type = "checkbox"; cb.className = "fm-cb"; cb.checked = fmSel.has(path);
  cb.onclick = ev => { ev.stopPropagation(); fmPick(path, ev.shiftKey ? "range" : "toggle"); };
  const caret = document.createElement("span"); caret.className = "fm-caret";
  caret.textContent = type === "dir" ? (collapsed ? "▸" : "▾") : "";
  const nm = document.createElement("span"); nm.className = "fm-name";
  nm.textContent = name;   // dirs are already marked by the ▸/▾ caret span
  nm.title = type === "file" ? (path + " · " + fmtSize(size)) : path;
  row.append(cb, caret, nm);
  if (type === "file") { const sz = document.createElement("span"); sz.className = "fm-size"; sz.textContent = fmtSize(size); row.append(sz); }
  const toggleDir = () => { if (fmCollapsed.has(path)) fmCollapsed.delete(path); else fmCollapsed.add(path); renderFm(); };
  row.onclick = (ev) => {
    if (ev.target === cb) return;
    if (ev.target === caret && type === "dir") { toggleDir(); return; }
    if (ev.shiftKey) { ev.preventDefault(); fmPick(path, "range"); return; }
    if (ev.ctrlKey || ev.metaKey) { ev.preventDefault(); fmPick(path, "toggle"); return; }
    fmPick(path, "single");
  };
  row.ondblclick = (ev) => {
    if (ev.target === cb) return;
    if (type === "dir") toggleDir(); else openFile(path);
  };
  // right-click context menu
  row.oncontextmenu = (ev) => { ev.preventDefault(); fmPick(path, "single"); showCtx(ev.clientX, ev.clientY, path, type); };
  // drag-and-drop: drag files/folders into a folder
  row.ondragstart = (ev) => {
    if (!fmSel.has(path)) fmPick(path, "single");
    ev.dataTransfer.setData("text/plain", JSON.stringify([...fmSel]));
    ev.dataTransfer.effectAllowed = "move";
    row.classList.add("dragging");
  };
  row.ondragend = () => row.classList.remove("dragging");
  if (type === "dir") {
    row.ondragover = (ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = ev.dataTransfer.types.includes("Files") ? "copy" : "move"; row.classList.add("drag-over"); };
    row.ondragleave = () => row.classList.remove("drag-over");
    row.ondrop = async (ev) => {
      ev.preventDefault(); row.classList.remove("drag-over");
      // OS files dropped on a folder row upload INTO that folder.
      if (ev.dataTransfer.files && ev.dataTransfer.files.length) {
        await uploadFiles([...ev.dataTransfer.files], path);
        return;
      }
      let items; try { items = JSON.parse(ev.dataTransfer.getData("text/plain")); } catch (_) { return; }
      if (!Array.isArray(items) || !items.length) return;
      // don't drop into self or a child
      if (items.includes(path) || items.some(p => path.startsWith(p + "/"))) return;
      for (const src of items) {
        const fname = src.split("/").pop();
        const dst = path + "/" + fname;
        await fetch(fsBase() + "/rename", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ from: src, to: dst }) });
      }
      fmSel.clear();
      toast(items.length + " item" + (items.length > 1 ? "s" : "") + " moved to " + name);
      await refresh(); _changed();
    };
  }
  return row;
}

// Select `path`. mode: 'single' = only this (replace); 'toggle' = add/remove
// individual (Ctrl); 'range' = contiguous span from the anchor (Shift, replaces).
function fmPick(path, mode) {
  const ai = fmAnchor != null ? fmOrder.indexOf(fmAnchor) : -1;
  if (mode === "range" && ai >= 0) {
    const bi = fmOrder.indexOf(path);
    if (bi >= 0) { fmSel.clear(); const lo = Math.min(ai, bi), hi = Math.max(ai, bi);
      for (let i = lo; i <= hi; i++) fmSel.add(fmOrder[i]); }   // anchor stays put for further shift-extends
  } else if (mode === "toggle") {
    if (fmSel.has(path)) fmSel.delete(path); else fmSel.add(path);
    fmAnchor = path;
  } else {                                                // 'single' (and range with no anchor)
    fmSel.clear(); fmSel.add(path); fmAnchor = path;
  }
  renderFm();
}

function syncToolbar() {
  const n = fmSel.size;
  $("#fmRename").disabled = n !== 1;
  $("#fmDelete").disabled = n === 0;
  const selEntries = [...fmSel].map(p => fmEntries.find(x => x.path === p)).filter(Boolean);
  const nFiles = selEntries.filter(e => e.type !== "dir").length;
  const nDirs = selEntries.filter(e => e.type === "dir").length;
  $("#fmDownload").disabled = nFiles === 0;
  $("#fmDuplicate").disabled = nFiles !== 1 || nDirs > 0;
  $("#fmMoveTo").disabled = n === 0;
  $("#fmSelInfo").textContent = n ? (n + " selected") : "";
  const all = $("#fmSelAll");
  if (all) {
    all.checked = fmOrder.length > 0 && fmSel.size >= fmOrder.length;
    all.indeterminate = fmSel.size > 0 && fmSel.size < fmOrder.length;
  }
  const nf = fmEntries.filter(e => e.type !== "dir").length, nd = fmEntries.filter(e => e.type === "dir").length;
  $("#fmCount").textContent = fmEntries.length ? (nf + " file" + (nf === 1 ? "" : "s") + ", " + nd + " folder" + (nd === 1 ? "" : "s")) : "";
  // uploads land in a single selected folder, else root — say so upfront
  const t = uploadTarget();
  const hint = $("#fmUpHint");
  if (hint) hint.textContent = t ? ("uploads → " + t + "/") : "";
}

/* ---- toolbar operations ---- */
async function fmDownload() {
  // Download each selected FILE (folders are skipped) via the raw-bytes endpoint,
  // which sends Content-Disposition: attachment. Works for text and binary alike.
  const files = [...fmSel].filter(p => { const e = fmEntries.find(x => x.path === p); return e && e.type !== "dir"; });
  if (!files.length) { toast("Select a file to download (folders can't be downloaded)."); return; }
  for (let i = 0; i < files.length; i++) {
    const a = document.createElement("a");
    a.href = fsBase() + "/download?path=" + encodeURIComponent(files[i]);
    a.download = files[i].split("/").pop();
    document.body.appendChild(a); a.click(); a.remove();
    if (i < files.length - 1) await new Promise(r => setTimeout(r, 300));   // stagger multi-file downloads
  }
}
async function fmDelete() {
  const items = [...fmSel]; if (!items.length) return;
  const where = _proj() ? ("project “" + _proj().name + "”") : "this chat";
  showModal("Delete " + items.length + " item" + (items.length > 1 ? "s" : "") + " from " + where + "? " +
            "Folders are removed with all their contents. This cannot be undone.", async () => {
    for (const p of items) {
      const r = await fetch(fsBase() + "/file?path=" + encodeURIComponent(p), { method: "DELETE" });
      if (r.ok && (editorFile === p || (editorFile && editorFile.startsWith(p + "/")))) { editorFile = null; $("#editorModal").hidden = true; }
    }
    fmSel.clear();
    await refresh(); _changed();
  });
}
async function fmNewFile(dirPrefix) {
  const ph = (dirPrefix ? dirPrefix + "/" : "") || "";
  const path = await dlgPrompt("New file path (e.g. notes.md or sub/dir/file.txt):", { value: ph, yes: "create" }); if (!path) return;
  const r = await fetch(fsBase() + "/file?path=" + encodeURIComponent(path),
    { method: "PUT", headers: { "content-type": "text/plain" }, body: "" });
  if (!r.ok) { await dlgAlert("Could not create file."); return; }
  await refresh(); _changed(); openFile(path);
}
async function fmNewFolder() {
  const path = await dlgPrompt("New folder path (e.g. drafts or 2026/reports):", { yes: "create" }); if (!path) return;
  const r = await fetch(fsBase() + "/mkdir",
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path }) });
  if (!r.ok) { await dlgAlert("Could not create folder."); return; }
  await refresh();
}
async function fmRename() {
  if (fmSel.size !== 1) return;
  const from = [...fmSel][0];
  const to = await dlgPrompt("Rename / move — new path:", { value: from, yes: "rename" }); if (!to || to === from) return;
  const r = await fetch(fsBase() + "/rename",
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ from, to }) });
  if (!r.ok) { const d = await r.json().catch(() => ({})); await dlgAlert("Rename failed: " + (d.detail || ("HTTP " + r.status))); return; }
  if (editorFile === from) editorFile = to;
  fmSel.clear(); fmSel.add(to); fmAnchor = to;
  await refresh(); _changed();
}

/* ---- upload (with live progress + per-file status) ---- */
// Uploads land in a single selected folder; otherwise in the workspace root.
function uploadTarget() {
  if (fmSel.size === 1) {
    const p = [...fmSel][0], e = fmEntries.find(x => x.path === p);
    if (e && e.type === "dir") return p;
  }
  return "";
}
function upHide() { const b = $("#fmUp"); if (b) { b.hidden = true; b.innerHTML = ""; } }
function upRow(id, name) {
  let row = document.getElementById(id);
  if (!row) {
    row = document.createElement("div"); row.className = "fm-uprow"; row.id = id;
    row.innerHTML = '<span class="nm"></span><div class="bar"><i></i></div><span class="st"></span>';
    row.querySelector(".nm").textContent = name;
    $("#fmUp").appendChild(row);
  }
  return row;
}
function upProgress(id, name, frac) {
  const row = upRow(id, name); row.querySelector(".bar>i").style.width = Math.round(frac * 100) + "%";
  row.querySelector(".st").textContent = Math.round(frac * 100) + "%";
}
function upDone(id, name, ok, msg) {
  const row = upRow(id, name); row.classList.toggle("ok", ok); row.classList.toggle("err", !ok);
  row.querySelector(".bar>i").style.width = "100%";
  const st = row.querySelector(".st"); st.textContent = (ok ? "✓ " : "✕ ") + msg; st.title = msg;
}
function uploadOne(id, file, dir) {
  return new Promise((resolve) => {
    const rel = (dir ? dir + "/" : "") + file.name;
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", fsBase() + "/file?path=" + encodeURIComponent(rel));
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) upProgress(id, rel, e.loaded / e.total); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve({ ok: true });
      else { let d = "HTTP " + xhr.status; try { d = JSON.parse(xhr.responseText).detail || d; } catch (_) { }
             resolve({ ok: false, error: xhr.status === 413 ? ("too large (max " + MAX_FILE_MB + " MB)") : d }); }
    };
    xhr.onerror = () => resolve({ ok: false, error: "network error" });
    xhr.send(file);
  });
}
async function uploadFiles(files, dir) {
  dir = dir !== undefined ? dir : uploadTarget();
  const cap = MAX_FILE_MB * 1024 * 1024, box = $("#fmUp");
  box.hidden = false; box.innerHTML = "";
  let ok = 0, bad = 0, i = 0;
  for (const f of files) {
    const id = "up_" + (i++);
    const rel = (dir ? dir + "/" : "") + f.name;
    if (f.size > cap) { upDone(id, rel, false, "too large — " + fmtSize(f.size) + " > " + MAX_FILE_MB + " MB limit"); bad++; continue; }
    upProgress(id, rel, 0);
    const r = await uploadOne(id, f, dir);
    if (r.ok) { upDone(id, rel, true, fmtSize(f.size) + " uploaded"); ok++; }
    else      { upDone(id, rel, false, r.error); bad++; }
  }
  await refresh(); _changed();
  if (!bad) setTimeout(upHide, 2500);          // all good → auto-dismiss; keep errors visible
}

/* ---- context menu ---- */
let _ctxUploadDir = null;      // set by "upload here…", consumed by the file input
function showCtx(x, y, path, type) {
  const m = $("#fmCtx"); if (!m) return;
  const isFile = type === "file";
  const items = [
    isFile ? { label: "Open", icon: "•", fn: () => openFile(path) } : { label: "Expand / collapse", icon: "▸", fn: () => {
      if (fmCollapsed.has(path)) fmCollapsed.delete(path); else fmCollapsed.add(path); renderFm(); } },
    !isFile ? { label: "New file here…", icon: "+", fn: () => fmNewFile(path) } : null,
    !isFile ? { label: "Upload here…", icon: "↑", fn: () => { _ctxUploadDir = path; $("#fmFileInput").click(); } } : null,
    isFile ? { label: "Download", icon: "↓", fn: () => { fmSel.clear(); fmSel.add(path); fmDownload(); } } : null,
    { label: "Rename", icon: "", fn: () => { fmSel.clear(); fmSel.add(path); renderFm(); fmRename(); } },
    isFile ? { label: "Duplicate", icon: "⧉", fn: () => { fmSel.clear(); fmSel.add(path); renderFm(); fmDuplicate(); } } : null,
    { label: "Move to…", icon: "↷", fn: () => { if (!fmSel.has(path)) { fmSel.clear(); fmSel.add(path); renderFm(); } fmMoveTo(); } },
    { sep: true },
    { label: "Delete", icon: "✕", cls: "danger", fn: () => { fmSel.clear(); fmSel.add(path); renderFm(); fmDelete(); } },
  ].filter(Boolean);
  m.innerHTML = "";
  for (const it of items) {
    if (it.sep) { const s = document.createElement("div"); s.className = "ctx-sep"; m.appendChild(s); continue; }
    const d = document.createElement("div"); d.className = "ctx-item" + (it.cls ? " " + it.cls : "");
    d.textContent = (it.icon ? it.icon + " " : "") + it.label;
    d.onclick = () => { m.hidden = true; it.fn(); };
    m.appendChild(d);
  }
  // position: clamp to viewport
  m.style.left = Math.min(x, innerWidth - 180) + "px";
  m.style.top = Math.min(y, innerHeight - m.children.length * 30 - 20) + "px";
  m.hidden = false;
}

/* ---- duplicate ---- */
async function fmDuplicate() {
  const files = [...fmSel].filter(p => { const e = fmEntries.find(x => x.path === p); return e && e.type !== "dir"; });
  if (files.length !== 1) return;
  const src = files[0], parts = src.split("/"), fname = parts.pop();
  const dot = fname.lastIndexOf("."), base = dot > 0 ? fname.slice(0, dot) : fname, ext = dot > 0 ? fname.slice(dot) : "";
  const dst = (parts.length ? parts.join("/") + "/" : "") + base + "-copy" + ext;
  // read then write — server has no copy endpoint
  const r = await fetch(fsBase() + "/file?path=" + encodeURIComponent(src));
  if (!r.ok) { toast("Could not read file"); return; }
  const data = await r.json();
  if (data.binary) { toast("Cannot duplicate binary files yet"); return; }
  const w = await fetch(fsBase() + "/file?path=" + encodeURIComponent(dst),
    { method: "PUT", headers: { "content-type": "text/plain" }, body: data.content || "" });
  if (!w.ok) { toast("Duplicate failed"); return; }
  toast("Duplicated → " + dst.split("/").pop());
  fmSel.clear(); fmSel.add(dst);
  await refresh(); _changed();
}

/* ---- move to folder ---- */
async function fmMoveTo() {
  const items = [...fmSel]; if (!items.length) return;
  // collect available folders
  const dirs = fmEntries.filter(e => e.type === "dir" && !items.includes(e.path)).map(e => e.path);
  dirs.unshift(".");  // root
  const target = await dlgPrompt("Move " + items.length + " item" + (items.length > 1 ? "s" : "") + " to folder:\n\n" +
    "Available: " + dirs.join(", ") + "\n\nOr type a new folder path:", { value: ".", yes: "move" });
  if (!target) return;
  let moved = 0;
  for (const src of items) {
    const fname = src.split("/").pop();
    const dst = (target === "." ? "" : (target + "/")) + fname;
    if (dst === src) continue;
    const r = await fetch(fsBase() + "/rename", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ from: src, to: dst }) });
    if (r.ok) moved++;
  }
  fmSel.clear();
  toast(moved + " item" + (moved > 1 ? "s" : "") + " moved");
  await refresh(); _changed();
}

/* ==================== file editor (and read-only deliverable viewer) ======== */
let editorFile = null, cmEditor = null, editorDirty = false;
const _CM_MODE = { py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "javascript", tsx: "javascript", json: { name: "javascript", json: true },
  c: "text/x-csrc", h: "text/x-csrc", cpp: "text/x-c++src", hpp: "text/x-c++src", cc: "text/x-c++src",
  java: "text/x-java", rs: "rust", go: "go", html: "htmlmixed", htm: "htmlmixed", xml: "xml",
  svg: "xml", css: "css", md: "markdown", markdown: "markdown", yaml: "yaml", yml: "yaml", toml: "toml",
  sh: "shell", bash: "shell", zsh: "shell", sql: "sql", dockerfile: "dockerfile",
  ini: "properties", cfg: "properties", conf: "properties", env: "properties", properties: "properties" };
function modeForPath(p) {
  const base = p.split("/").pop().toLowerCase();
  if (base === "dockerfile") return "dockerfile";
  return _CM_MODE[(base.split(".").pop() || "")] || null;
}
const _IMG_EXTS = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"];

function _cmEnsure() {
  if (!window.CodeMirror) return null;
  if (!cmEditor) {
    // Light theme gets CodeMirror's built-in light ("default") theme — the
    // dark dracula box inside a light page was the one visible seam.
    const light = document.body.classList.contains("light");
    cmEditor = CodeMirror.fromTextArea($("#editorArea"),
      { lineNumbers: true, theme: light ? "default" : "dracula",
        indentUnit: 2, viewportMargin: Infinity });
    cmEditor.on("change", () => { if (editorFile) setDirty(true); });
  }
  return cmEditor;
}
function setDirty(on) {
  editorDirty = !!on;
  const el = $("#editorDirty"); if (el) el.hidden = !editorDirty;
}
function _showTextarea(on) {
  // CodeMirror wraps the textarea in .CodeMirror; image mode hides both.
  const cmWrap = $("#editorModal .CodeMirror"), ta = $("#editorArea"), img = $("#editorImg");
  if (cmWrap) cmWrap.style.display = on ? "" : "none";
  if (ta && !cmWrap) ta.style.display = on ? "" : "none";
  if (img) img.hidden = on;
}
async function closeEditor() {
  if (editorDirty && !await dlgConfirm("Discard unsaved changes?", { yes: "discard" })) return;
  $("#editorModal").hidden = true; editorFile = null; setDirty(false);
}

async function openFile(path) {
  const r = await fetch(fsBase() + "/file?path=" + encodeURIComponent(path));
  if (!r.ok) return;
  const f = await r.json();
  const ext = (path.split(".").pop() || "").toLowerCase();
  if (f.binary) {
    if (_IMG_EXTS.includes(ext)) { openImagePreview(path); return; }
    dlgAlert("“" + path + "” is a binary file and can't be edited here."); return;
  }
  editorFile = path;
  $("#editorSave").hidden = false; $("#editorDownload").hidden = true;
  $("#editorPath").textContent = (_proj() ? _proj().name : "chat") + " / " + path + (f.truncated ? "  (truncated view — saving would clip)" : "");
  $("#editorMsg").textContent = ""; $("#editorModal").hidden = false;
  _showTextarea(true);
  const ro = !!f.truncated, content = f.content || "";
  const cm = _cmEnsure();
  if (cm) {
    cm.setOption("mode", modeForPath(path)); cm.setOption("readOnly", ro);
    cm.setValue(content); setTimeout(() => { cm.refresh(); cm.focus(); }, 0);
  } else { const ta = $("#editorArea"); ta.value = content; ta.readOnly = ro; ta.focus(); }
  setDirty(false);
}
/* Binary images open as a visual preview instead of the "can't edit" alert. */
function openImagePreview(path) {
  editorFile = null;
  $("#editorSave").hidden = true;
  $("#editorPath").textContent = (_proj() ? _proj().name : "chat") + " / " + path;
  $("#editorMsg").textContent = ""; $("#editorModal").hidden = false;
  const dl = $("#editorDownload");
  dl.hidden = false; dl.href = fsBase() + "/download?path=" + encodeURIComponent(path);
  dl.setAttribute("download", "");
  _showTextarea(false);
  const img = $("#editorImg");
  img.alt = path;
  img.src = fsBase() + "/download?path=" + encodeURIComponent(path) + "&inline=1";
}
async function saveEditor() {
  if (editorFile == null) return;
  const content = cmEditor ? cmEditor.getValue() : $("#editorArea").value;
  const r = await fetch(fsBase() + "/file?path=" + encodeURIComponent(editorFile),
    { method: "PUT", headers: { "content-type": "text/plain" }, body: content });
  $("#editorMsg").textContent = r.ok ? "saved ✓" : "save failed";
  if (r.ok) { setDirty(false); refresh(); _changed(); }
}
/* Open a generated deliverable in the same editor popup, READ-ONLY + download. */
async function openDeliverable(runId, name) {
  const url = "/api/output/" + runId; let text;
  try { const r = await fetch(url + "?inline=1"); if (!r.ok) throw new Error("http " + r.status); text = await r.text(); }
  catch (_) { window.open(url + "?inline=1", "_blank", "noopener"); return; }
  editorFile = null;
  $("#editorPath").textContent = name + "  (read-only)"; $("#editorMsg").textContent = "";
  $("#editorSave").hidden = true;
  const dl = $("#editorDownload"); dl.hidden = false; dl.href = url; dl.setAttribute("download", "");
  $("#editorModal").hidden = false;
  _showTextarea(true);
  const cm = _cmEnsure();
  if (cm) {
    cm.setOption("mode", modeForPath(name)); cm.setOption("readOnly", true);
    cm.setValue(text); setTimeout(() => { cm.refresh(); }, 0);
  } else { const ta = $("#editorArea"); ta.value = text; ta.readOnly = true; }
  setDirty(false);
}

/* ---- wiring (called once from init; elements live in index.html) ---- */
function init(deps) {
  _deps = Object.assign(_deps, deps || {});

  $("#fmClose").onclick = close;
  $("#fmRefresh").onclick = refresh;
  $("#fmNewFile").onclick = () => fmNewFile();
  $("#fmNewFolder").onclick = fmNewFolder;
  $("#fmRename").onclick = fmRename;
  $("#fmDownload").onclick = fmDownload;
  $("#fmDelete").onclick = fmDelete;
  $("#fmDuplicate").onclick = fmDuplicate;
  $("#fmMoveTo").onclick = fmMoveTo;
  $("#fmUpload").onclick = () => { _ctxUploadDir = null; $("#fmFileInput").click(); };
  $("#fmFileInput").addEventListener("change", async () => {
    const fs = [...$("#fmFileInput").files]; $("#fmFileInput").value = "";
    if (fs.length) await uploadFiles(fs, _ctxUploadDir !== null ? _ctxUploadDir : undefined);
    _ctxUploadDir = null;
  });
  $("#fmSelAll").addEventListener("change", (e) => {
    if (e.target.checked) fmOrder.forEach(p => fmSel.add(p)); else fmSel.clear(); renderFm(); });
  $("#filesModal").addEventListener("click", (e) => { if (e.target.id === "filesModal") close(); });  // backdrop closes

  // collapse / expand all
  let allCollapsed = true;
  $("#fmCollapseAll").onclick = () => {
    const dirs = fmEntries.filter(e => e.type === "dir").map(e => e.path);
    if (allCollapsed) { dirs.forEach(d => fmCollapsed.delete(d)); allCollapsed = false; }
    else { dirs.forEach(d => fmCollapsed.add(d)); allCollapsed = true; }
    renderFm();
  };

  // search / filter
  const s = $("#fmSearch");
  if (s) s.addEventListener("input", () => { fmFilter = s.value.toLowerCase(); renderFm(); });

  // drag-and-drop upload (OS files onto the modal backdrop area)
  const modal = $("#filesModal"), dz = $("#fmDropZone");
  if (modal && dz) {
    let dragDepth = 0;
    modal.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer.types.includes("Files")) return;
      dragDepth++; dz.hidden = false;
    });
    modal.addEventListener("dragleave", () => { dragDepth--; if (dragDepth <= 0) { dragDepth = 0; dz.hidden = true; } });
    modal.addEventListener("dragover", (e) => {
      if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; }
    });
    modal.addEventListener("drop", async (e) => {
      dragDepth = 0; dz.hidden = true;
      if (!e.dataTransfer.files.length) return;
      e.preventDefault();
      await uploadFiles([...e.dataTransfer.files]);
    });
  }

  // context menu dismiss
  document.addEventListener("click", () => { const m = $("#fmCtx"); if (m) m.hidden = true; });
  document.addEventListener("contextmenu", (e) => {
    const m = $("#fmCtx"); if (m && !m.hidden && !m.contains(e.target)) m.hidden = true;
  });

  // keyboard: navigation + operations inside the file manager
  document.addEventListener("keydown", (e) => {
    if ($("#filesModal").hidden) return;
    const t = e.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    if (e.key === "Escape") { close(); return; }
    if (typing) return;  // don't intercept while typing in search/editor
    const cur = fmSel.size === 1 ? fmOrder.indexOf([...fmSel][0]) : -1;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!fmOrder.length) return;
      e.preventDefault();
      const next = e.key === "ArrowDown"
        ? Math.min(fmOrder.length - 1, cur < 0 ? 0 : cur + 1)
        : Math.max(0, cur < 0 ? 0 : cur - 1);
      fmPick(fmOrder[next], "single");
      const row = $("#fmTree .fm-row.sel"); if (row) row.scrollIntoView({ block: "nearest" });
    }
    else if (e.key === "ArrowRight" && cur >= 0) {
      const p = fmOrder[cur], ent = fmEntries.find(x => x.path === p);
      if (ent && ent.type === "dir" && fmCollapsed.has(p)) { fmCollapsed.delete(p); renderFm(); }
    }
    else if (e.key === "ArrowLeft" && cur >= 0) {
      const p = fmOrder[cur], ent = fmEntries.find(x => x.path === p);
      if (ent && ent.type === "dir" && !fmCollapsed.has(p)) { fmCollapsed.add(p); renderFm(); }
    }
    else if (e.key === "Delete" && fmSel.size) { e.preventDefault(); fmDelete(); }
    else if (e.key === "F2" && fmSel.size === 1) { e.preventDefault(); fmRename(); }
    else if ((e.key === "Enter" || e.key === " ") && fmSel.size === 1) {
      e.preventDefault();
      const p = [...fmSel][0], ent = fmEntries.find(x => x.path === p);
      if (ent && ent.type === "dir") { if (fmCollapsed.has(p)) fmCollapsed.delete(p); else fmCollapsed.add(p); renderFm(); }
      else if (ent) openFile(p);
    }
    else if (e.key === "a" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault(); fmOrder.forEach(p => fmSel.add(p)); renderFm();
    }
  });

  // editor wiring
  $("#editorSave").onclick = saveEditor;
  $("#editorClose").onclick = closeEditor;
  document.addEventListener("keydown", (e) => {
    if ($("#editorModal").hidden) return;
    if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); saveEditor(); }
    else if (e.key === "Escape") closeEditor();
  });
  const ta = $("#editorArea");
  if (ta) ta.addEventListener("input", () => { if (editorFile) setDirty(true); });
}

return { init, refresh, open, close, isOpen: () => !$("#filesModal").hidden,
         fsBase, openFile, openDeliverable };
})();
window.FileUI = FileUI;
