# JayNet Orchestrator — consolidated latest (2026-07-09)

Every file edited/created this session, at its LATEST state. Extract over
/srv/orchestrator/ (it preserves paths). Restart orchestrator-web + hard-refresh.

DELIBERATELY EXCLUDED (yours — never overwrite): runtime/tool_base.py,
tests/conftest.py, config/litellm.yaml, systemd units, start-brain*.sh, presets/.

## Backend / runtime
- runtime/loop.py .............. per-tool timeout, null-msg fix, workspace-root inject,
                                 grill mode, sub-agent progress forwarding, tool preview
                                 cap (web.tool_preview_chars), _normalize_verify bool guard
- runtime/boot_posture.py ...... serve the GPU-1 default at boot via model.use

## Tools (auto-discovered)
- tools/verify/score.py ........ verify.score / verify.rank / verify.probe
                                 (numeric 0-9 scale, dominant extraction, grammar, no_think)
- tools/ops/run.py ............. ops.run (gated host exec) + ops.status (stack health)
- tools/council/debate.py ...... council.debate (multi-round multi-model deliberation)
- tools/trace/mine.py .......... trace.mine (recurring tool-sequence miner, AWO)
- tools/serve/lifecycle.py ..... serve.start reads tools.serve.command_template  ← the fix
- tools/pdf/create.py .......... pdf.create (Chromium render)
- tools/fs/ops.py .............. fs.find
- tools/model/catalog.py ....... model.list / model.use (static-port swap)

## Config / prompt
- config/runtime.yaml .......... verify, tools.ops(+status), council, models catalog
                                 (brain / coder=Tess / ornith, boot:[coder]),
                                 command_template, web.tool_preview_chars, concurrency=1
- prompts/orchestrator.md ...... all routing/guidance additions

## Web console
- web/server.py ................ file download endpoints, grill field, /api/tools params,
                                 boot-posture startup hook
- web/static/index.html ........ download btn, grill toggle, COMPACT quick-settings grid,
                                 admin tool-reference (?) modal
- web/static/app.js ............ download, grill (null-safe), tool-help, collapsed live
                                 activity box, reload-safe persistence, expandable tool
                                 results, null-safe toggle reads

## Tests
- tests/test_*.py .............. verify / ops / council / trace / boot / timeout / catalog / pdf / fs

## Deploy order note
index.html + app.js are a MATCHED PAIR — deploy together (a JS ref to a missing HTML
element is what caused the last crash). Same for lifecycle.py + the runtime.yaml
command_template line. After deploy: grep a marker on the CODE side, not just config.
