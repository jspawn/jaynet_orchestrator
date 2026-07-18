---
name: fable-judge
description: Adversarial verification of finished work — treats any completion report as unverified claims and re-checks them by direct observation. Load after substantive work, or when any agent or tool claims "done".
---
# The Fable Judge

Adversarial verification of finished work. Treats any completion report as a set of claims and believes nothing it did not observe. Use after any substantive work, or when any agent/tool claims "done".

**When to use:** after `code.delegate` returns, after `architect` completes, after any multi-step task finishes, when you're suspicious of a result, or when asked to "judge", "verify", "prove it works".

**Tools you'll need:** `code.run`/`test.run` (re-execute claims), `fs.read`/`fs.grep` (inspect changes), `git.diff` (what actually changed), `verify.score` (quality gate), `agent.spawn` (for independent verification subagent).

---

## The principle

The most documented failure of coding agents is claiming success regardless of reality. Tests get weakened until they agree. Reports say "all tests pass" after failure transcripts. Files are created but never verified. The judge exists to catch this.

**Rule: a claim is verified ONLY if you observed it yourself in this session.** Reading the executor's report that it passed is not verification. You must re-run.

## Step 1 — Extract claims

From the completed work (or the executor's report), extract every verifiable claim as a numbered list:

```
1. "Fixed the timezone bug in formatDate"
2. "All 42 tests pass"
3. "Build is green"
4. "No other files were modified"
5. "Cleaned up temp files"
```

Each claim becomes a verification target.

## Step 2 — Independent re-execution

For each claim, verify by the appropriate method:

| Claim type | Verification method |
|---|---|
| "Tests pass" | `test.run` or `code.run` — run the actual test suite, read the output |
| "Build is green" | `code.run` the build command, check exit code + output |
| "Fixed X" | Run the specific scenario that was broken, observe it working |
| "No other files changed" | `git.diff` or `fs.list` — check what actually changed vs what was claimed |
| "File exists / was created" | `fs.read` the file, verify it has the expected content |
| "Cleaned up" | `fs.find` for scratch files, temp dirs, test artifacts |
| "Performance improved" | Run the benchmark, compare numbers |
| "API returns X" | `code.run` a curl/request, read the response |

**Rules:**
- Run each check yourself. Do not trust cached results from the executor's session.
- If a check requires credentials or infra you don't have, mark it CAVEAT with "cannot verify: {reason}".
- If a check contradicts the claim, capture the actual output verbatim.

## Step 3 — Hunt for fraud patterns

Beyond re-executing claims, actively look for these common fraud patterns:

| Pattern | How to detect |
|---|---|
| **Weakened tests** | `git.diff` on test files — were assertions removed, thresholds relaxed, error cases deleted? |
| **Try/catch swallowing** | `fs.grep` for broad `except:` or `catch(e){}` added around the changed code |
| **Disabled checks** | `fs.grep` for `skip`, `@disabled`, `TODO: re-enable`, `.skip(` in test files |
| **Hardcoded test data** | Tests that check against hardcoded values that match the new (potentially wrong) behavior |
| **Missing edge cases** | The fix handles the happy path but the original bug's edge case isn't tested |
| **Leftover debug code** | `fs.grep` for `console.log`, `print(`, `debugger`, `TODO`, `HACK`, `XXX` in changed files |
| **Scope creep** | `git.diff` shows changes to files not mentioned in the task |
| **False cleanup** | Claim says "cleaned up" but temp files, .bak files, or debug outputs remain |

## Step 4 — Verdict

For each claim, assign one of:

- **VERIFIED** — you observed it passing/true yourself
- **CAVEAT** — partially true, or true with conditions, or you couldn't fully verify (state why)
- **REFUTED** — the claim is false; state what you observed instead

Then an overall verdict:

- **VERIFIED** — all claims verified, no fraud patterns found
- **CAVEATS** — most claims verified but some have conditions or couldn't be checked
- **REFUTED** — one or more material claims are false

## Step 5 — Report

Format:
```
## Judge verdict: {VERIFIED|CAVEATS|REFUTED}

### Claims
1. "Fixed the timezone bug" — VERIFIED: test_timezone passes, output shows correct UTC offset
2. "All 42 tests pass" — REFUTED: 41 pass, test_edge_case fails with ValueError
3. "Build is green" — VERIFIED: exit code 0, no warnings
4. "No other files modified" — CAVEAT: git diff shows a formatting change in utils.py (benign)

### Fraud check
- No weakened tests detected
- No swallowed exceptions
- One TODO comment added (line 45) — not a fraud, but should be tracked

### Recommendation
{What needs to happen before this work can be considered done}
```

## Usage with verify.score

After the manual judge pass, also run `verify.score` with criteria derived from the original task's definition of done. The judge's verdict and verify.score should agree. If they disagree, trust the judge (it observed; the scorer inferred).

## Spawned mode

For maximum rigor, spawn the judge as an independent subagent via `agent.spawn`:
- Give it the original task + the claimed results
- Do NOT give it the executor's reasoning
- Let it discover and verify independently
- Its verdict is authoritative
