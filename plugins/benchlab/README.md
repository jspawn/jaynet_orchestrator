# benchlab — public agent benchmarks as JayNet eval cases

Imports tasks from public agent benchmarks and converts them into eval cases
that run through the existing JayNet eval harness:

- **Terminal-Bench** (`laude-institute/terminal-bench`, Apache-2.0), two import
  modes:
  - **lite** (default): a curated container-free subset of the core tasks.
    Grading is deterministic — each case embeds the task's own pytest checks
    in an `expect.checker` script (fixtures are seeded; grading code is never
    shown to the agent). pytest is a grading-only dep: the checker uses the
    runtime python when it has it, else self-bootstraps a cached venv
    (`<data>/benchlab/checker-venv`, network once) — fresh installs self-heal.
  - **full**: any task in the catalog. At import, each task's `Dockerfile` is
    built into a cached podman image (`benchlab-tb-<name>-<hash>`) plus a
    thin test layer on top (`…-t<hash>`: pytest — strict, build fails without
    it — plus the tests' own pip deps, scanned from the test imports and
    installed per-package tolerant: local helper modules are skipped and one
    unresolvable name, e.g. the agent's own solution module, never keeps the
    whole task unimportable), the task's tests are staged host-side under
    `benchlab/tests/<name>/` (never visible to the agent), and the generated
    case carries `container: {image, workdir: /app, network: true}` and a
    per-case turn cap (1200s). At run time the eval runner starts the
    container over the case sandbox (2 GB / 2 CPUs, outbound network like
    upstream), routes `code.execute` INTO it, and the checker `podman cp`s
    the staged tests to `/tests` (the upstream convention) and runs pytest
    inside the container. **Multi-service tasks** (a `docker-compose.yaml`
    with more than one service, e.g. `sql-injection-attack`) get
    `container.compose` + `client_service` instead: at run time the runner
    starts the task's OWN compose stack via podman-compose under a unique
    per-run project (siblings DNS-reachable from the client container,
    depends_on/init ordering handled by compose, the prebuilt client image
    injected as `T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME` so nothing rebuilds)
    and tears it down with `down -v` after the case — project-scoped named
    volumes keep runs isolated. Requires `podman-compose` on the host;
    without it these cases skip like any missing capability.
- **GAIA** (`gaia-benchmark/GAIA`, CC-BY-4.0, **gated** HF dataset): Level-1
  validation questions as normalized exact-match cases (`expect.answer_exact_any`
  against the gold answer, keyed on the `FINAL ANSWER:` marker, same as the
  real GAIA scorer). Requires your own HF token with access to the dataset,
  set as `HF_TOKEN` in the service environment (e.g. `~/.config/jaynet.env`);
  it is never logged. Attachments are downloaded and seeded as project files.

## Enable and use

1. Enable in **Admin → Plugins** (builtin plugins are disabled by default).
2. Ask the agent (or use the tools directly):
   - `bench.sources` — what's supported and already imported (lite/full counts).
   - `bench.fetch` — clone the Terminal-Bench catalog into the data-dir cache
     (`benchlab/terminal-bench`, ~170 MB, network).
   - `bench.import` — write eval YAMLs to the custom evals dir. Lite mode
     defaults to the audited container-free subset (~10 tasks) or 50 GAIA
     rows; re-imports overwrite only `tb-*`/`gaia-*` cases benchlab owns.
   - `bench.import` with `mode: "full"` — container cases (tag `tb-full`).
     Optional `tasks` allowlist and `limit`; the default is the whole catalog.
     Requires **rootless podman**; image **builds need network** (base pulls,
     apt/pip in the task Dockerfiles) and can take a long first run — images
     are cached by content hash, so re-imports only rebuild changed tasks.
     A full import of a task that also has a lite case overwrites it (same
     `tb-<name>` id, same benchmark task, higher fidelity).
3. Run the cases in **Admin → Eval**, and compare brains in the **Benchmark**
   tab (tags `bench`, `tb`, `tb-full`, `gaia`).

## Grading needs pytest

The lite checker runs pytest host-side. pytest ships in `requirements.txt`
(since 1.4.0), so a standard install has it in the runtime venv and grading
works out of the box; on an older install add it once:

    uv pip install --python .venv/bin/python pytest

When the runtime venv lacks pytest the checker self-bootstraps a cached
checker venv under the benchlab data dir (uv-first, pip fallback; network
once). Cases imported before the uv-first checker template existed should
be re-imported (`bench.import` overwrites its own `tb-*` cases) — their
baked-in checker is the old bootstrap-less one and only works with pytest
in the runtime venv.

## Honesty note

These are **JayNet-condition runs**: the agent's own toolset and prompts, our
own budgets. Scores are comparable over time and across your own brain
variants — they are **not** leaderboard-official Terminal-Bench or GAIA
numbers.

Full mode (containers) is close to the official Terminal-Bench protocol —
same image, same tests, run in-container, outbound network during the agent
phase. Remaining divergences: the agent works through the `code.execute` +
`fs.*` tool surface instead of a raw shell, and JayNet's per-case budgets
apply (1200s turn cap per case) instead of upstream's step limits.

Upstream licenses apply to the imported task content: Terminal-Bench is
Apache-2.0; GAIA is CC-BY-4.0 and gated — importing it requires accepting the
dataset terms on Hugging Face with your own account.
