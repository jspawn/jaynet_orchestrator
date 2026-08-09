# Operating JayNet day to day

Logs, traces, spend, and the "why is it doing that" workflow. Install lives in
[install.md](install.md), upgrades in [upgrading.md](upgrading.md), the
security posture in [security.md](security.md).

## Services and logs

Two systemd user units do everything:

- `jaynet-web.service` — the Python service (web console, agent loop,
  tool registry) **and** the supervised llama-server children. Stopping it
  also kills stray model servers (`KillMode=mixed`).
- `litellm-proxy.service` — the OpenAI-compatible proxy on `:4000` that
  unifies local and cloud models.

```bash
systemctl --user status jaynet-web litellm-proxy
journalctl --user -u jaynet-web -f          # service + model servers
scripts/orch --doctor                             # env, paths, ports, services
```

The same information, live and prettier: **Admin → Status** (service health,
hardware, recent runs) and **Admin → Processes** (per-server cards with
auto-refreshing log tails, start/stop/restart).

## Runs and traces

Every run — web chat, CLI, chains, scheduled jobs — is logged step by step to
`trace.db` in your data dir (`JAYNET_DATA`), two tables:

- `runs` — one row per request: owner, message, final answer, status
  (`ok` / `error` / `budget_exceeded`), tokens, cost.
- `events` — one row per step: `model_turn`, `tool_call`, `tool_result`,
  `error`, with iteration number and payload.

Ways to look:

- **Admin → Status → Recent runs** — click a run for the step-by-step view.
- **CLI:** `scripts/orch --trace <run_id_prefix>` replays a run;
  `scripts/orch --details "<msg>"` adds a per-tool usage breakdown to a fresh
  one.
- **SQL**, when you want the shape of things:

```bash
sqlite3 "$JAYNET_DATA/trace.db"

-- most recent runs
SELECT id, status, cost_usd, total_tokens, user_message
  FROM runs ORDER BY started_at DESC LIMIT 10;

-- cost and iterations per run
SELECT id, status, cost_usd, total_tokens,
       json_extract(summary_json, '$.iterations') AS iters
  FROM runs ORDER BY started_at DESC LIMIT 20;

-- step-by-step replay of one run
SELECT iteration, kind, payload_json
  FROM events WHERE run_id LIKE 'abc%' ORDER BY ts;

-- which tools actually fire
SELECT json_extract(payload_json, '$.name') AS tool, COUNT(*) n
  FROM events WHERE kind = 'tool_call'
  GROUP BY 1 ORDER BY n DESC LIMIT 15;
```

The trace stores what `trace.log_content` in `config/runtime.yaml` allows —
full content while developing, metadata-only if you'd rather not keep
sensitive text in a SQLite file.

## Usage and spend

- **Local runs are free** but still counted (tokens, iterations).
- **Cloud spend** is accounted per run in `trace.db` (`cost_usd`, priced from
  `runtime.yaml`) — the proxy itself is deliberately stateless.
- Per-user view: **account menu → Usage** (totals, by month/year, recent runs
  with per-run cost). Admin → Users shows the same across all users, plus
  budgets you can set per account.

## When things go wrong

| Symptom | Likely cause | Where to look |
|---|---|---|
| console unreachable | service down | `systemctl --user status jaynet-web`, `journalctl --user -u jaynet-web -e` |
| `connection refused :4000` | proxy down | `journalctl --user -u litellm-proxy -e` |
| model card red in Admin → Processes | server crashed / OOM | its log tail in Processes; [llama-ops.md](llama-ops.md#when-a-server-misbehaves) |
| run ends `budget_exceeded` | ceilings too tight for the task | raise per-run (CLI flags / quick settings) or per-user budget |
| `PrivacyViolation` | cloud tool called on a tainted conversation | expected behavior — opt in with `share_private` or restructure |
| tool missing from the registry | import error in a tool file | boot log: "Failed to import tools.…"; `orch --list-tools` |
| cloud call 5xx | key invalid / rate-limited / model id drifted | `~/.config/jaynet.env`; litellm.yaml header warns ids drift |
| slow first request after restart | KV cache cold | normal; warms after one query |

## Backups

**Admin → Backup** creates and lists full data-dir backups (users, chats,
projects, wiki, memory, Studio layer). Restore procedure and what migrates
automatically on upgrade: [upgrading.md](upgrading.md#data-safety). Keep the
data dir out of any git checkout — it is live state, not source.
