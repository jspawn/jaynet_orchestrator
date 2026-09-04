---
name: research-and-verify
shape: research-and-verify
description: >
  Find a specific fact, source, URL, version, or dataset where the answer
  must be RIGHT and is judged against ground truth (typical shape: "find the
  official X and write it to file Y"). Load when the task asks to look up
  and deliver verifiable findings. The procedure frontier models run
  implicitly: define the exact artifact → search broadly → verify each
  candidate against TWO independent signals → deliver in the named format.
---
# Research and verify — the procedure

Small models lose these tasks by answering from training data or taking the
first search hit. A lookup task is graded on correctness, and "official" /
"current" answers rot — training data is not a source. Run the steps in
order.

## 1. Define the exact artifact (one pass, write it down)
`note.set` with:
- **What is being asked**, precisely: a URL, a version string, a name, a
  number? Per item if there are several.
- **The deliverable**: the named output file and its exact format (fields,
  schema, example). Write to it EARLY with partial results, then fill in —
  an 80% file beats a 100% chat answer.
- **What "right" means**: e.g. "official" = maintained by the authors/org
  itself, not a mirror, fork, or re-implementation.

## 2. Search broadly, shortlist candidates
- `web.search` per item; collect 2-3 candidates each, not one.
- Query with the CURRENT year for anything time-sensitive (versions,
  prices, availability) — never your training data's year.
- Thin or JS-heavy page → `web.render`; big page → `web.fetch` + range reads.

## 3. Verify every candidate against TWO independent signals
One source is a rumor. Accept a candidate only when two independent places
agree — e.g. the paper's own page links the repo AND the org's GitHub hosts
it; the vendor's docs state the version AND a release note confirms it.
Mirror/fork/aggregator sites do NOT count as a second signal for
"official". Keep a `note.set` tally: item → candidate → the two signals.

## 4. Deliver in the named format
- Write/refresh the deliverable file as items verify, not in one burst at
  the end.
- Diff your output against the required schema field by field — right
  values in the wrong format is a fail.
- Confirm the file exists with `fs.list` before finishing.

## 5. Escalation rungs (when stuck, in order)
1. Rephrase the query (exact title in quotes, site: filters, the org name).
2. Go one level up: the paper/article/vendor landing page instead of search.
3. `code.delegate` a bulk lookup batch with your tally from step 3.
4. An item stays unverifiable → deliver the rest, say plainly which item
   has no verified answer. Never fill the gap with a guess.

## Anti-patterns (each seen failing real runs)
- Answering lookups from memory — stale "official" links are the classic.
- First search hit becomes the answer without a second source.
- A fork/mirror delivered as the "official" source.
- Perfect research, never written to the named file.
- One giant end-of-run write instead of delivering as items verify.
