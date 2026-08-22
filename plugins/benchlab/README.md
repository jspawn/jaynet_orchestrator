# benchlab — public agent benchmarks as JayNet eval cases

Imports tasks from public agent benchmarks and converts them into eval cases
that run through the existing JayNet eval harness:

- **Terminal-Bench** (`laude-institute/terminal-bench`, Apache-2.0): a curated
  container-free subset of the core tasks. Grading is deterministic — each
  case embeds the task's own pytest checks in an `expect.checker` script
  (fixtures are seeded; grading code is never shown to the agent).
- **GAIA** (`gaia-benchmark/GAIA`, CC-BY-4.0, **gated** HF dataset): Level-1
  validation questions as normalized exact-match cases (`expect.answer_exact_any`
  against the gold answer, keyed on the `FINAL ANSWER:` marker, same as the
  real GAIA scorer). Requires your own HF token with access to the dataset,
  set as `HF_TOKEN` in the service environment (e.g. `~/.config/jaynet.env`);
  it is never logged. Attachments are downloaded and seeded as project files.

## Enable and use

1. Enable in **Admin → Plugins** (builtin plugins are disabled by default).
2. Ask the agent (or use the tools directly):
   - `bench.sources` — what's supported and already imported.
   - `bench.fetch` — clone the Terminal-Bench catalog into the data-dir cache
     (`benchlab/terminal-bench`, ~170 MB, network).
   - `bench.import` — write eval YAMLs to the custom evals dir. Defaults to the
     audited container-free Terminal-Bench subset (~10 tasks) or 50 GAIA rows;
     re-imports overwrite only `tb-*`/`gaia-*` cases benchlab owns.
3. Run the cases in **Admin → Eval**, and compare brains in the **Benchmark**
   tab (tags `bench`, `tb`, `gaia`).

## Honesty note

These are **JayNet-condition runs**: no containers, a different sandbox, the
agent's own toolset and prompts. Scores are comparable over time and across
your own brain variants — they are **not** leaderboard-official Terminal-Bench
or GAIA numbers.

Upstream licenses apply to the imported task content: Terminal-Bench is
Apache-2.0; GAIA is CC-BY-4.0 and gated — importing it requires accepting the
dataset terms on Hugging Face with your own account.
