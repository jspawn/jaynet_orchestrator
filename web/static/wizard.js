/* wizard.js — guided start: "What would you like to achieve?" + two or
   three simple questions that route to the right tool (plain chat, /goal,
   /loop, or a new project). With this many ways to run things, choosing
   shouldn't be the hard part. Self-contained (own CSS, own DOM), same
   pattern as dialog.js.

   const r = await startWizard()
   // null (cancelled) or:
   // {kind:"chat",    text}  plain message — prefill the composer
   // {kind:"prefill", text}  /goal or /loop command — prefill the composer
   // {kind:"project"}        open the new-project flow
*/
(function () {
  const css = `
.wiz-overlay{position:fixed;inset:0;background:rgba(4,6,10,.6);backdrop-filter:blur(3px);
  display:flex;align-items:center;justify-content:center;z-index:200}
.wiz-box{background:var(--panel,#161a21);border:1px solid var(--border-soft,#2a323d);
  border-radius:12px;padding:22px;max-width:440px;width:calc(100vw - 48px);
  box-shadow:0 12px 40px rgba(0,0,0,.4)}
.wiz-title{margin:0 0 4px;color:var(--fg,#e6e9ee);font-size:15px;font-weight:600}
.wiz-sub{margin:0 0 14px;color:var(--fg-dim,#9aa4b0);font-size:12.5px;line-height:1.4}
.wiz-area{width:100%;box-sizing:border-box;background:var(--panel2,#1c212a);
  color:var(--fg,#e6e9ee);border:1px solid var(--border,#2a323d);border-radius:8px;
  padding:9px 11px;font:inherit;font-size:13.5px;min-height:64px;resize:vertical;
  margin-bottom:12px}
.wiz-input{width:100%;box-sizing:border-box;background:var(--panel2,#1c212a);
  color:var(--fg,#e6e9ee);border:1px solid var(--border,#2a323d);border-radius:6px;
  padding:8px 10px;font:inherit;font-size:13px;margin-bottom:10px}
.wiz-area:focus,.wiz-input:focus{outline:2px solid var(--accent,#E8C04A);outline-offset:1px}
.wiz-cards{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.wiz-card{text-align:left;background:var(--panel2,#1c212a);color:var(--fg,#e6e9ee);
  border:1px solid var(--border,#2a323d);border-radius:8px;padding:10px 12px;
  cursor:pointer;font:inherit}
.wiz-card:hover{border-color:var(--accent,#E8C04A)}
.wiz-card b{display:block;font-size:13.5px;margin-bottom:2px}
.wiz-card span{font-size:12px;color:var(--fg-dim,#9aa4b0);line-height:1.35}
.wiz-row{display:flex;gap:8px;justify-content:flex-end}
.wiz-go{background:var(--accent,#E8C04A);color:#1c1608;border:0;border-radius:6px;
  padding:8px 15px;cursor:pointer;font-weight:600;font:inherit;font-size:13px}
.wiz-go:hover{filter:brightness(1.08)}
.wiz-back{background:var(--panel2,#1c212a);color:var(--fg,#e6e9ee);
  border:1px solid var(--border,#2a323d);border-radius:6px;padding:8px 15px;
  cursor:pointer;font:inherit;font-size:13px}
.wiz-back:hover{border-color:#4a5462}
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

  window.startWizard = function () {
    return new Promise((resolve) => {
      const overlay = el("div", "wiz-overlay");
      const box = el("div", "wiz-box");
      overlay.appendChild(box);
      overlay.addEventListener("mousedown", (e) => {
        if (e.target === overlay) done(null);
      });
      const state = { what: "", kind: "", mode: "" };

      function done(v) {
        document.removeEventListener("keydown", onKey, true);
        overlay.remove();
        resolve(v);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.stopPropagation(); done(null); }
      }
      document.addEventListener("keydown", onKey, true);

      function head(title, sub) {
        box.innerHTML = "";
        box.appendChild(el("p", "wiz-title", title));
        if (sub) box.appendChild(el("p", "wiz-sub", sub));
      }
      function row(back, go, goFn) {
        const r = el("div", "wiz-row");
        if (back) {
          const b = el("button", "wiz-back", back);
          b.type = "button";
          b.onclick = () => stepWhat(state.what);
          r.appendChild(b);
        }
        if (go) {
          const g = el("button", "wiz-go", go);
          g.type = "button";
          g.onclick = goFn;
          r.appendChild(g);
        }
        box.appendChild(r);
      }
      const oneLine = (s) => s.replace(/\s*\n\s*/g, " ").trim();

      // step 1: the goal in the user's own words
      function stepWhat(prefill) {
        head("What would you like to achieve?",
             "A sentence is enough — the next questions pick the right tool for it.");
        const ta = el("textarea", "wiz-area");
        ta.value = prefill || "";
        ta.placeholder = "e.g. modernize the billing module, research heat-pump prices, keep an eye on my servers…";
        box.appendChild(ta);
        row(null, "continue →", () => {
          state.what = oneLine(ta.value);
          if (!state.what) { ta.focus(); return; }
          stepKind();
        });
        ta.focus();
      }

      // step 2: how big is it?
      function stepKind() {
        head("How big is it?", "Be honest — small is fine, most things are small.");
        const cards = el("div", "wiz-cards");
        const opts = [
          ["chat", "A quick question", "One answer, maybe a lookup. Just ask it."],
          ["task", "A task — a few steps", "Tools, files or code; done in one conversation."],
          ["long", "A long job", "Many steps; should keep working until it's done, no babysitting."],
          ["project", "An ongoing workspace", "A topic I'll return to — files, wiki and chats that stay."],
        ];
        for (const [k, b, s] of opts) {
          const c = el("button", "wiz-card");
          c.type = "button";
          c.appendChild(el("b", "", b));
          c.appendChild(el("span", "", s));
          c.onclick = () => {
            state.kind = k;
            if (k === "project") return done({ kind: "project" });
            if (k === "long") return stepMode();
            done({ kind: "chat", text: state.what });
          };
          cards.appendChild(c);
        }
        box.appendChild(cards);
        row("← back", null, null);
      }

      // step 3 (long job): alone or together?
      function stepMode() {
        head("Does it need you along the way?",
             "Both run until done and report progress to this chat on every device.");
        const cards = el("div", "wiz-cards");
        const opts = [
          ["loop", "Mostly on its own", "Loop: fresh context every iteration — the marathon runner. STATE.md carries memory; best for well-defined finish lines."],
          ["goal", "Work it with me", "Goal: keeps the conversation between turns — better when you'll steer, discuss, or the context matters."],
        ];
        for (const [k, b, s] of opts) {
          const c = el("button", "wiz-card");
          c.type = "button";
          c.appendChild(el("b", "", b));
          c.appendChild(el("span", "", s));
          c.onclick = () => { state.mode = k; stepDone(); };
          cards.appendChild(c);
        }
        box.appendChild(cards);
        row("← back", null, null);
      }

      // step 4 (loop/goal): the finish line
      function stepDone() {
        head("When is it done?",
             "The loop/goal stops when this holds — a judge double-checks.");
        const dw = el("input", "wiz-input");
        dw.placeholder = "done when: e.g. all tests pass, the report is saved";
        box.appendChild(dw);
        let ck = null;
        if (state.mode === "loop") {
          ck = el("input", "wiz-input");
          ck.placeholder = "optional check command, exit 0 = done (e.g. pytest -q)";
          box.appendChild(ck);
          box.appendChild(el("p", "wiz-sub",
            "With a check command, IT decides completion instead of the judge — deterministic, runs in the workspace."));
        }
        row("← back", "start →", () => {
          let cmd = `/${state.mode} ${state.what}`;
          if (dw.value.trim()) cmd += ` | done when: ${oneLine(dw.value)}`;
          if (ck && ck.value.trim()) cmd += ` | check: ${ck.value.trim()}`;
          done({ kind: "prefill", text: cmd });
        });
        dw.focus();
      }

      stepWhat("");
      document.body.appendChild(overlay);
    });
  };
})();
