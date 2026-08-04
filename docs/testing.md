# Testing

The pytest suite (~840 tests, no network, ~45 s). For the in-agent `test.run`
harness (the tool the model uses to run tests inside a project), see
[testing-harness.md](testing-harness.md) — this page is about JayNet's own
suite.

## Running it

```bash
cd /srv/orch-dev        # the dev checkout
.venv/bin/python -m pytest tests/ -q                 # full suite
.venv/bin/python -m pytest tests/test_verify.py -q   # one file
.venv/bin/python -m pytest tests/ -k voice -x        # by keyword, stop on first failure
```

Any venv with `requirements.txt` + `requirements-web.txt` +
`requirements-test.txt` works (the live install's
`/srv/orchestrator/.venv/bin/python` is the usual one).

## Conventions

- **No network, ever** — HTTP is monkeypatched; models are stubbed
  (`runtime.run` is faked in the web fixtures).
- **No cross-test imports** — helpers are copied, not shared.
- Comments short and plain; one behavior per test.

Fixtures (`tests/conftest.py`):

| Fixture | Gives you |
|---|---|
| `project` / `config` / `ctx` | a tmp project tree, a minimal runtime config, a ToolContext factory (`ctx(work_root=…)`) for calling tools directly |
| `git_repo` | `project` as a committed git repo |
| `web_app(env=…)` | in-process FastAPI app on throwaway dirs (loads the real `config/runtime.yaml`, preset paths rewritten to this checkout) |
| `web_client(app)` | logged-in httpx client over ASGI (`async with … as c:`) |
| `record_run(app)` | captures the kwargs `runtime.run` is called with |

## What the files cover

### Agent loop & budgets

| File | What it pins |
|---|---|
| `test_loop_regressions.py` (75) | the regression battery: rejected tool calls, cancel, budget stops, compaction edge cases |
| `test_budget.py` | token accounting — cached prompt tokens at reduced weight |
| `test_agent_budget.py` | sub-agent (agent.spawn) budget assembly + clamping |
| `test_model_concurrency.py` | per-backend model-call concurrency gate |
| `test_tool_timeout.py` | a blocking tool is cancelled, the run continues |
| `test_compact.py` · `test_salience_compaction.py` | /compact slicing; pinned results survive compaction |
| `test_anchor_placement.py` · `test_progress_and_signals.py` | working anchors, note.set scratchpad, no-progress breaker |
| `test_turn_body.py` | model-turn body; `chat_template_kwargs` is llama.cpp-only |
| `test_optimizations.py` · `test_sampling.py` | context/latency optimizations, code.delegate; per-run sampler merge |
| `test_architect.py` | the architect pipeline: plan → review → arbitrate/refine → execute |

### Web, API & chats

| File | What it pins |
|---|---|
| `test_api_contract.py` | the stable API shapes (docs/api.md) — breaking one is a release decision |
| `test_version.py` | version surfaces in /api/health + admin status |
| `test_voice_chat.py` | /api/voice chat mode (`voice:false`) branching + IDOR safety |
| `test_web_regressions.py` · `test_current_chat.py` | chat ownership rules; the unsaved chat follows the user across devices |
| `test_outputs_traversal.py` | path-traversal guards on client-controlled ids |
| `test_flags.py` · `test_watchdog.py` | flag-for-debugging flow; the run coroner's trigger + reports |
| `test_goal.py` | /goal store, grammar, supervisor loop |
| `test_imp.py` · `test_slash.py` · `test_wgs.py` · `test_wiki.py` | /imp, slash-command parsing/execution, /wgs, /llmwiki |
| `test_project_fileops.py` | project/scratch file ops behind the file manager |
| `test_confirmation_gate.py` | unattended runs must deny, never hang on a TTY prompt |

### Tools — files & code

| File | What it pins |
|---|---|
| `test_workspace.py` | fs.* confinement to the run's work roots |
| `test_fs_find.py` · `test_fs_resolve_hint.py` · `test_fs_diff.py` | find/resolve hints; edit/write diffs (the chat's inline diff badges) |
| `test_code_tools.py` · `test_code_exec_coverage.py` | code.run/patch/symbols/tree/deps; sandbox command construction |
| `test_git_ops.py` | git remote/working-tree ops and worktrees |
| `test_lint_and_trace.py` | lint.run, trace.query, trace content gating |
| `test_pdf_create.py` · `test_deliver_files.py` | dependency-free pdf.create; deliver.files confinement |

### Tools — web & research

| File | What it pins |
|---|---|
| `test_web_fetch_guards.py` · `test_web_request.py` | SSRF guards: loopback/link-local rejected, redirect hops re-checked |
| `test_web_search_priority.py` | backend order: SearXNG (local) → cloud fallbacks |
| `test_web_crawl.py` · `test_web_extract.py` · `test_browser.py` | bounded crawl, extraction sub-agent, shared browser session |
| `test_arxiv_coverage.py` · `test_research.py` · `test_docs_summarize.py` | arxiv parsing; research.* state spine; docs.summarize rollups |

### Knowledge, chains & integrations

| File | What it pins |
|---|---|
| `test_memory_coverage.py` · `test_kg_coverage.py` | memory.* FTS5 round-trips; kg entity/relation graph |
| `test_rag_confinement.py` | rag.index confined to work roots like fs.* |
| `test_chains.py` | chain.list / chain.run pipelines |
| `test_mcp.py` · `test_connector.py` | MCP bridge; declarative API connectors |

### Models, serving & verification

| File | What it pins |
|---|---|
| `test_preset_store.py` · `test_cloud_store.py` | preset catalog DB (+ ORCH_HOME-relative seed paths); cloud catalog + keyless-proxy render |
| `test_model_alias.py` · `test_model_alias_sync.py` | alias resolution; cross-config consistency (litellm ↔ runtime ↔ costs) |
| `test_model_catalog.py` · `test_specialist_strengths.py` | model.list/model.use on static ports; strengths in the system prompt |
| `test_llm_call_coverage.py` · `test_spawn_model_resolution.py` | llm.call shaping; agent.spawn model resolution |
| `test_serve_lifecycle.py` · `test_boot_posture.py` · `test_process_manager_stats.py` | serve.start/stop; boot posture; MTP stats parsing |
| `test_pid_reuse_guards.py` | job.cancel/serve.stop verify process identity |
| `test_start_model_sh.py` | the launcher's two modes (incl. injection guards) |
| `test_verify.py` · `test_verify_gate.py` · `test_verify_env.py` | verify.* scoring; verifier-gated termination; check-command env |
| `test_council.py` · `test_eval_compare_coverage.py` | council.debate flow; eval.compare |
| `test_ops.py` | ops.run allowlist + metachar rejection |
| `test_scheduler.py` · `test_job_wait.py` | schedule.* parsing/store/scoping; job.wait poll exemption |
| `test_trace_mine.py` | trace.mine sequence extraction + safety flags |

### Studio, registry & skills

| File | What it pins |
|---|---|
| `test_discovery.py` | every tool is discovered under its expected name |
| `test_registry_extra.py` · `test_studio_layers.py` | the custom layer (ORCH_DATA/custom) loading + precedence |
| `test_studio_routes.py` | Studio admin API CRUD |
| `test_jaypack.py` | .jaypack export/import round-trips |
| `test_skills_catalog.py` | every skill has a non-empty description |
| `test_privacy_source_of_truth.py` | privacy flags live on the tools, not config copies |

### Install & config

| File | What it pins |
|---|---|
| `test_config_check.py` | the runtime.yaml typo guard |
| `test_pull_model.py` | pull-model filtering/selection/download layout |
