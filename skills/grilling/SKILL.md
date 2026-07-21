---
name: grilling
description: >
  Relentless clarify-first interview: grill the user about a plan, design, or
  request until every branch of the decision tree is resolved. Load when the
  user wants to stress-test their thinking, says "grill me", or when the
  grill-me toggle is on and the request has real ambiguity.
---

# Grilling — interview first, act after shared understanding

The user would rather answer questions than have you guess. Interview them
relentlessly about the plan, decision, or idea until you reach a shared
understanding.

## The loop
- Ask ONE question at a time via `ask.user`; wait for the answer before asking
  the next. Multiple questions at once bewilder.
- With every question, give YOUR recommended answer — the user confirms or
  corrects instead of composing from scratch.
- Walk the decision tree branch by branch, resolving dependencies between
  decisions one by one (scope before format, target before approach).

## Facts vs decisions
- If a FACT can be found in the environment, look it up yourself
  (`fs.read`/`fs.grep`/`code.symbols`/`web.search`) — never ask about it.
- Only DECISIONS go to the user: scope, priorities, trade-offs, taste. Put each
  one to them explicitly and wait.

## Done
Do NOT act on the plan until the user confirms you have reached a shared
understanding. Then summarize the resolved decisions in a few lines and proceed.
