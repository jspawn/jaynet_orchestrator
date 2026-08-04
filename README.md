# JayNet

**A local-first LLM orchestrator for your own hardware.** JayNet runs a
capable agent — with tools, memory, skills, projects and scheduled runs —
entirely on a workstation you own. Local models do the work; cloud models
exist only as an approval-gated escape hatch. One Python service, one web
console, no containers required.

*Why I made it: I'm a dad and IT guy by day, a learner and vibe coder by
night. JayNet is my daily driver and my learning project rolled into one —
built in the evenings to understand how agents really work, and opinionated
about privacy because it handles my family's stuff. If that's your vibe too,
welcome.*

Status: **v0.9.x** (semver, [changelog](CHANGELOG.md)) — daily-driven and
feature-rich; 1.0 is the public release milestone ([what's left](docs/development.md#versioning)).
License: MIT.

## Why JayNet

The self-hosted agent world splits into three camps, and JayNet is
deliberately none of them:

- **Chat frontends** (Open WebUI, LibreChat) give you chat + RAG, but models
  are fixed endpoints and agents an afterthought. JayNet inverts that: the
  agent loop is the product, and the models are *managed infrastructure* the
  agent can reconfigure mid-chat.
- **Agent platforms** (Dify, Flowise, n8n, AGiXT) bring visual workflows and
  plugin catalogs — at the cost of container orchestras and complexity.
  JayNet's answers are **chains** (small YAML pipelines) and an **MCP
  bridge**, both plain text in one service.
- **Agent frameworks** (LangGraph, CrewAI) are libraries for building what
  JayNet already is.

So it's for the **single operator or small team with a GPU workstation** who
wants a private multi-model agent that owns its whole stack — and values
knowing exactly what ran over having a marketplace of integrations.

The headline features: several local models working as one (a *brain* that
reasons and routes, a swappable *specialist* it delegates to, CPU embed +
rerank for RAG) · a mid-chat **model switcher** the brain operates itself ·
~100 tools plus on-demand **skills** and chains · the **Studio**, where
admins build new skills/connectors in the browser (AI-assisted) and share
them as `.jaypack` files · **privacy guardrails**: cloud calls are
approval-gated, private tool results never leave the box without consent,
every step is traced.

## Quick start

Minimal install — one CPU, one small model, ~15 minutes:

```bash
git clone https://github.com/jspawn/jaynet.git /srv/orchestrator && cd /srv/orchestrator
scripts/quickstart.sh
```

Then run the two commands it prints and open `http://127.0.0.1:8071` (the
admin password is generated and logged on first boot). For the full setup
with systemd services, run `scripts/setup.sh` instead — and validate either
with `scripts/orch --doctor`.

| Tier | What you need | What you get |
|---|---|---|
| **Minimal** | x86_64 Linux, 8 GB RAM, 10 GB disk, no GPU | Full agent chat with the default brain (Qwen3-4B), CPU inference |
| **Full setup** | 16 GB RAM, 100 GB disk, GPU sized to your brain (8 GB VRAM for 4–8B … 24–32 GB for 30B-class MoE) | GPU brain, RAG, model switcher |
| **Production** | 64 GB RAM, 2× 32 GB GPU | 35B-class brain + 27B specialist side by side ([example](#example-setup-wolf--my-daily-driver)) |

Everything by hand, multi-GPU builds, reverse proxy, uninstall:
**[docs/install.md](docs/install.md)**. Models to download:
**[docs/models.md](docs/models.md)** (license-clean defaults, all
Apache-2.0/MIT).

## Configuration at a glance

JayNet is configured in layers, each simple on its own:

- **Behavior** — `config/runtime.yaml`: the system prompt, budgets, tool
  selection, privacy gates, voice channel, per-tool settings. Most of it is
  commented inline; unknown keys get a "did you mean" warning at boot.
- **Secrets, paths, ports** — `~/.config/orchestrator.env` (template in
  `example_configs/`): API keys, tokens, install/data dirs, service ports.
  Never committed.
- **Models** — the preset catalog (Admin → Presets): which models exist,
  their weights, ports, strengths, and *where they run* — any GPU count,
  mixed vendors, CPU fallback ([how placement works](docs/model-placement.md)).
  Cloud models live here too: aliases, costs, fallbacks — keys stay in the
  env file.
- **Admin console** — service/hardware status, managed processes, the
  prompt, run defaults, tool toggles, users, flagged sessions, RAG, and the
  Studio (build your own skills, chains, API connectors and tools in the
  browser).
- **User menu** — per-user settings: location & timezone (for local,
  fresh answers), theme, chat style, run budgets and preferences, 2FA, and
  API tokens for the [HTTP API](docs/api.md) / CLI clients.

Deeper dives: [architecture & subsystems](docs/architecture.md) ·
[security posture](docs/security.md) ·
[upgrading & migrations](docs/upgrading.md) ·
[development & versioning](docs/development.md) ·
[testing](docs/testing.md).

## Example setup (wolf) — my daily driver

The deployment this repo's shipped config mirrors — a single workstation
running everything:

- **Hardware:** AMD Ryzen 9 7950X (16C/32T), 64 GB RAM,
  2× AMD Radeon AI PRO R9700 32 GB (RDNA4, ROCm), 2× 1 TB NVMe
  (models and data on separate disks)
- **Models:** brain = Qwen3.6-35B-A3B MoE on GPU 0; specialist = Fable-27B
  on GPU 1, swapped mid-chat when a task calls for it (coding, research,
  security variants); embed + rerank on CPU for RAG
- **Stack:** llama.cpp self-built (ROCm + Vulkan), LiteLLM proxy, web
  console — all systemd user services; the process manager supervises the
  model servers
- **Around it:** nginx + Let's Encrypt on a separate host, a SearXNG
  container for web search, cloud models (kimi, glm, gemini, qwen) as
  approval-gated escalation only

Yours will differ — that's the point of the preset catalog.

## References & incorporated ideas

Where some of the ideas came from:

| Source | What we took from it |
| --- | --- |
| [arxiv.org/abs/2601.22037](https://arxiv.org/abs/2601.22037) — "Optimizing Agentic Workflows using Meta-tools" (AWO) | Profile-guided tool-call sequence mining from execution traces → `trace.mine`, the AWO-style recurring-sequence miner over `trace.db` that finds composite tool patterns bundleable into meta-tools. |
| [arxiv.org/abs/2601.01885](https://arxiv.org/abs/2601.01885) | Salience memory: salience-weighted compaction, pinned tool results surviving compaction. |
| [arxiv.org/abs/2607.05391](https://arxiv.org/abs/2607.05391) — "LLM-as-a-Verifier" | `verify.score` / `verify.rank`: logit-expectation over single-token grades instead of a judge's emitted token — continuous, tie-free scores. |
| [github.com/masamasa59/ai-agent-papers](https://github.com/masamasa59/ai-agent-papers) — agent-papers taxonomy | Harness engineering as its own discipline, versioned skill libraries (→ `skills/`), structured episodic memory (→ `memory.*` + `kg.*`), execution-trajectory logging as foundational (→ `trace.db`). |
| [looprails.dev](https://looprails.dev) — "Agentic Loops in the Wild" | The verifier is the central variable: successful agents use external, ungameable verifiers. Shaped `verify.*`: wire loop decisions to `test.run`/`code.run` results, not model self-assessment. |
| [github.com/Sahir619/fable-method](https://github.com/Sahir619/fable-method) | The Fable methodology — triviality gate, classify→define done→evidence→decide→act→verify→report — adapted into the `fable-method`, `fable-loop`, `fable-judge` skills. |
| [Karpathy's LLM-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | `/llmwiki`: an LLM-maintained persistent wiki as compiled knowledge, complementing RAG's raw sources. |
| "Get things done the engineering way" skill collections | `grill-me`, `writing-great-skills` (→ `/wgs`), and the diff-based two-axis code review ported as `skills/diff-review`. |
| OpenRouter / Z.ai docs | Provider comparison, GLM-5.2 specs, endpoints, pricing → cloud-model consolidation. |

Development history (earlier sessions): Session 1 — core architecture,
LiteLLM, tool registry, agent loop, trace logging. Session 2 — branding,
`ask.user`, archives, admin UI, vision fix. Session 3 — model preset system,
ConcurrencyGate. Session 4 — brain+specialist posture, `council.debate`,
`verify.*`, `ops.*`, `trace.mine`, `boot_posture`.

## Contact

Questions, ideas, bugs: Christian — <cf@jaynet.ch> ·
[jaynet.ch](https://jaynet.ch). Once the repo is public, GitHub issues are
the preferred channel.
