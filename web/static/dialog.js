/* dialog.js — styled, promise-based replacements for the native
   alert()/confirm()/prompt() (GUI audit C4: browser dialogs can't be themed
   and "prevent additional dialogs" silently breaks flows like rename).
   Self-contained (own CSS, own DOM) so every page can use it; colors follow
   the page's CSS variables with dark-theme fallbacks.

   await dlgAlert("something failed")
   await dlgConfirm("delete x?")                    → true | false
   await dlgConfirm("replace?", {yes:"replace", danger:false})
   await dlgPrompt("name:", {value:"draft.md"})     → string | null
*/
(function () {
  const css = `
.dlg-overlay{position:fixed;inset:0;background:rgba(4,6,10,.6);backdrop-filter:blur(3px);
  display:flex;align-items:center;justify-content:center;z-index:200}
.dlg-box{background:var(--panel,#161a21);border:1px solid var(--border-soft,#2a323d);
  border-radius:12px;padding:22px;max-width:400px;width:calc(100vw - 48px);
  box-shadow:0 12px 40px rgba(0,0,0,.4)}
.dlg-text{margin:0 0 16px;white-space:pre-line;color:var(--fg,#e6e9ee);
  font-size:14px;line-height:1.45;overflow-wrap:anywhere}
.dlg-input{width:100%;box-sizing:border-box;background:var(--panel2,#1c212a);
  color:var(--fg,#e6e9ee);border:1px solid var(--border,#2a323d);border-radius:6px;
  padding:8px 10px;font:inherit;font-size:13px;margin-bottom:14px}
.dlg-input:focus{outline:2px solid var(--accent,#E8C04A);outline-offset:1px}
.dlg-row{display:flex;gap:8px;justify-content:flex-end}
.dlg-yes{background:var(--accent,#E8C04A);color:#1c1608;border:0;border-radius:6px;
  padding:8px 15px;cursor:pointer;font-weight:600;font:inherit;font-size:13px}
.dlg-yes.danger{background:var(--err,#e06c75);color:#1c0608}
.dlg-yes:hover{filter:brightness(1.08)}
.dlg-no{background:var(--panel2,#1c212a);color:var(--fg,#e6e9ee);
  border:1px solid var(--border,#2a323d);border-radius:6px;padding:8px 15px;
  cursor:pointer;font:inherit;font-size:13px}
.dlg-no:hover{border-color:#4a5462}
`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function open(opts) {
    return new Promise((resolve) => {
      const overlay = el("div", "dlg-overlay");
      const box = el("div", "dlg-box");
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "true");
      box.appendChild(el("p", "dlg-text", opts.text));
      let inp = null;
      if (opts.input) {
        inp = el("input", "dlg-input");
        inp.type = "text";
        inp.value = opts.value || "";
        if (opts.placeholder) inp.placeholder = opts.placeholder;
        inp.spellcheck = false;
        box.appendChild(inp);
      }
      const row = el("div", "dlg-row");
      const done = (val) => {
        document.removeEventListener("keydown", onKey, true);
        overlay.remove();
        resolve(val);
      };
      if (!opts.alert) {
        const no = el("button", "dlg-no", "cancel");
        no.type = "button";
        no.onclick = () => done(null);
        row.appendChild(no);
      }
      const ok = el("button", "dlg-yes" + (opts.danger ? " danger" : ""), opts.yes || "ok");
      ok.type = "button";
      ok.onclick = () => done(opts.input ? (inp.value.trim() || null) : true);
      row.appendChild(ok);
      box.appendChild(row);
      overlay.appendChild(box);
      overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) done(null); });
      const onKey = (e) => {
        if (e.key === "Escape") { e.stopPropagation(); done(null); }
        else if (e.key === "Enter") { e.stopPropagation(); e.preventDefault(); ok.click(); }
      };
      document.addEventListener("keydown", onKey, true);
      document.body.appendChild(overlay);
      (inp || ok).focus();
      if (inp) inp.select();
    });
  }

  window.dlgAlert = (text) => open({ text, alert: true, yes: "ok", danger: false });
  window.dlgConfirm = (text, o) => {
    o = o || {};
    return open({ text, yes: o.yes || "confirm",
                  danger: o.danger !== false }).then((v) => v === true);
  };
  window.dlgPrompt = (text, o) => {
    o = o || {};
    return open({ text, input: true, value: o.value, placeholder: o.placeholder,
                  yes: o.yes || "save", danger: false });
  };
})();
