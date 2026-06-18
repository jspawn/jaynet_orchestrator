---
name: long-document
description: Summarise or analyse a document (or many files) too large to fit comfortably in context. Load when working with very long text.
---
# Working with a document too large for one pass

When the material won't fit comfortably in your context — or would crowd out the
room you need to reason — don't try to swallow it whole. Pick the approach that
fits the task:

1. **Will you query it more than once?** Index it, then retrieve only what each
   question needs. Split the text into chunks and `rag.index(collection="doc:<short-name>",
   text=<chunk>, source=<filename>)`, then `rag.search` per question. This keeps
   your context small across a whole conversation about the document.

2. **One-off summary or extraction?** Map-reduce with sub-agents so the raw text
   never enters your own context. For each section, `agent.spawn(task="Summarise
   this section in 5 bullet points: …", tools=["fs.read"])`; then combine the
   section summaries into the final answer yourself. Each child returns only its
   distilled result.

3. **Just slightly too long?** Read it in ranges (e.g. `fs.read` with offsets, or
   process file-by-file) and accumulate notes as you go.

Always state your method in one line ("summarised in five sections via
sub-agents") and flag anything you had to drop or truncate for length, so the user
knows the summary isn't guaranteed complete.
