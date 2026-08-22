# Jay's Studio packs

A small side-repo of ready-to-import `.jaypack` files:
**[jaynet-studio-packs](https://github.com/jspawn/jaynet-studio-packs)**.
Each pack installs into the custom layer (see [studio.md](studio.md)) — no
repo changes, survives upgrades, removable from the same Studio screen.

## What's in it

**Thinking skills (28 packs)** — structured-reasoning procedures for
decisions, diagnosis, risk and strategy, ported unchanged from
[tjboudreaux/cc-thinking-skills](https://github.com/tjboudreaux/cc-thinking-skills)
(MIT, © TJ Boudreaux). They are pure know-how skills: no tools, no code, no
credentials — the safest possible first import.

Good entry points:

- **thinking-model-router** — unsure which frame fits? It maps the problem and
  returns `NONE`, one skill, or up to three complementary ones.
- **thinking-scientific-method** — a symptom with several plausible causes:
  rank hypotheses, run the cheapest discriminating check first.
- **thinking-pre-mortem** — stress-test a plan by assuming it already failed.
- **thinking-reversibility** — classify a decision by how costly it is to undo.

The full list with one-liners lives in the packs repo README.

## Importing

Admin → Studio → **Import .jaypack**, pick the file — the skill appears in the
model's catalog immediately. Name clash? The UI asks before overwriting.

A note on catalog size: every installed skill costs one line in the always-on
catalog the brain sees. 28 thinking skills roughly double that block. It's
still small, but if you only need a few frames, import only those — the router
plus three or four leaves covers most work.

## Sharing your own

Export any Studio row as `.jaypack`, push it to your own repo, and others can
import it the same way. The format is a plain zip with a `jaypack.yaml`
manifest — inspectable before you install anything.
