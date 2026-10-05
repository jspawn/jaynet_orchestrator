# PageIndex plugin

Vectorless, reasoning-based retrieval for long PDFs, via the
[`pageindex`](https://pypi.org/project/pageindex/) pip SDK (MIT). Instead of
embedding chunks and hoping vector similarity finds the right ones, the SDK
builds a hierarchical **tree index** of the document with an LLM — section
titles, summaries, page ranges — and the JayNet brain *navigates* that tree
with tools. Everything runs against the local LiteLLM proxy; nothing leaves
the box.

## What it does

- `doc.index(action=build, path=…)` → indexes a PDF from the workspace ONCE
  (minutes on a long document — one LLM pass over the whole file) and returns
  a `doc_id`. The index is persistent (`<data>/pageindex/`): reuse it across
  runs and questions. `action=list` / `action=delete` manage the store.
- `doc.tree(doc_id, node_summary?)` → the section tree: titles, summaries,
  page ranges — no page text. Navigation, not reading.
- `doc.pages(doc_id, pages="1-3,7")` → the exact page text (capped at ~50k
  chars per call — ask for fewer pages when you hit it).

Overlap with existing surfaces: `doc.extract` reads a PDF inline and the
`long-document` skill re-reads chunks every time — both stay right for SHORT
documents. This plugin is for long ones (100+ pages) and repeat questions:
index once, navigate forever.

## Setup

1. Install the SDK into the JayNet venv: `pip install pageindex`
   (declared in `plugin.yaml` — until it's importable the plugin shows
   **unavailable** in Admin → Harness → Plugins with the missing package
   listed; installing pip deps needs one JayNet restart).
2. Enable the plugin: Admin → Harness → Plugins, or
   `plugins.pageindex.enabled: true` in runtime.yaml.
3. Make sure the configured model alias exists in your LiteLLM config —
   the default `openai/local-specialist` rides the standard specialist
   alias.

## Config (`plugins.pageindex`, admin → Harness → Runtime or runtime.yaml)

```yaml
plugins:
  pageindex:
    enabled: true
    model: openai/local-specialist  # litellm model string for indexing; the
                                    # alias after openai/ is served by the proxy
    storage_path: ""                # "" = <data>/pageindex[/<owner>] — per-account
                                    # (owner scoping); the ownerless CLI path keeps
                                    # the plain root, where pre-scoping indexes stay
    api_base: ""                    # "" = orchestrator.litellm_base
    api_key: ""                     # "" = $LITELLM_MASTER_KEY or sk-local
```

Defaults need no edits on a standard JayNet box — the client talks to the
same LiteLLM proxy everything else uses.

## Usage

```
User: what does the contract say about termination for convenience?
Brain: doc.index action=build path="contract.pdf"      # once — minutes
       doc.tree doc_id="…"                              # find the section
       doc.pages doc_id="…" pages="34-36"               # read exact pages
       → answer with page citations
```

## Caveats

- **Text-based PDFs only, no OCR** — scanned documents stay with
  `doc.extract` / the `pdf` skill.
- Indexing cost scales with document length (one LLM pass per page batch);
  a very long PDF on a slow local model can take a long time. That's the
  price paid once — reads afterwards are free of LLM calls.
- Retrieval quality depends on the local model's section summaries; the
  page ranges themselves are deterministic and reliable.
- The index store in `<data>/pageindex/` grows with every indexed document —
  `doc.index action=delete` removes one.
