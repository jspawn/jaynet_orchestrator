---
name: long-document
description: Summarise or analyse a document (or many files) too large to fit comfortably in context. Load when working with very long text.
---
# Working with a document too large for one pass

When the material won't fit comfortably in your context — or would crowd out the
room you need to reason — don't try to swallow it whole. **The file is the
context**: keep the bulk on disk and address it programmatically. Pick the
approach that fits the task:

1. **One-off aggregation, extraction, or summary over one big file?** Slice it
   with `code.execute` and map sub-LLM calls over the slices — the bulk never
   enters your own context. Read the file with plain Python (`open().read()`),
   split it into chunks (by lines/records/bytes — whatever the structure is),
   then `llm_query_batched([chunk_prompts...])` and reduce the answers yourself
   (counts, merged lists, a final synthesis). Subcalls are billed to your run
   and capped per execution (the tool result shows `subcalls.used/max`) — size
   chunks so you stay well under the cap, and aggregate incrementally for very
   large inputs. Verify: re-run a cheap programmatic check over the raw file
   (e.g. your own regex count) when the answer must be exact — don't trust the
   map step blindly.

2. **Will you query it more than once?** Index it, then retrieve only what each
   question needs. Split the text into chunks and `rag.index(collection="doc:<short-name>",
   text=<chunk>, source=<filename>)`, then `rag.search` per question. This keeps
   your context small across a whole conversation about the document.

3. **Each chunk needs tools, judgement, or its own multi-step work?** Map-reduce
   with sub-agents instead of subcalls: `agent.spawn(task="Summarise this
   section in 5 bullet points: …", tools=["fs.read"])` per section, then combine
   yourself. Heavier than `llm_query` per chunk, so prefer option 1 for plain
   extraction/summary.

4. **Just slightly too long?** Read it in ranges (e.g. `fs.read` with offsets, or
   process file-by-file) and accumulate notes as you go.

Two supporting habits:

- **A tool result arrived too big to keep quoting?** Push it out of the
  conversation with `context.stage` — you get a workspace file path back, then
  work on that file with the options above.
- **Pin, don't re-read.** If one small piece of the material (a schema, a
  header, a legend) must stay verbatim all run, `context.pin` it instead of
  re-fetching.

Always state your method in one line ("aggregated 40k log lines via chunked
sub-LLM calls, verified by regex count") and flag anything you had to drop or
truncate for length, so the user knows the answer isn't guaranteed complete.
