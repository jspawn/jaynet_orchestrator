# Your Local AI Assistant, Explained in Plain Language

A friendly walkthrough of what this project is and the handful of ideas that make
it tick — written for a curious reader, not an engineer. No prior knowledge
assumed.

> A note on one word: I've taken **"ARD"** to mean **RAG** (the "give it a
> private library" feature), since it belongs with tool/skill/MCP as a way the
> assistant gets things done. If you meant **ADR** (a way teams record *why* they
> made a design choice), say so and I'll add that instead.

---

## The big picture

Imagine hiring a capable assistant who lives entirely on your own computer. On
their own they're competent but not a genius. Their real value is that they know
**when to use a tool, when to consult a reference, and when to phone a
specialist** — and they keep careful notes the whole time. This project is a
hands-on guide to building exactly that assistant, so you understand every part
of it rather than renting a black box from someone else's cloud.

The most important thing to understand up front: the AI itself has **no memory
and takes no actions**. You send it text, it sends text back, and it instantly
forgets the exchange. All the apparent cleverness — remembering the conversation,
actually *doing* things, staying within limits — comes from a small program
wrapped around the AI called the **loop**. The AI is the brain in a jar; the loop
is the body, the hands, and the notebook. When the AI "decides to search the
web," what really happens is it writes a little note that says *"please search for
X,"* and the loop reads that note and chooses whether to act on it.

With that in mind, here are the four ways you give the assistant new abilities
and knowledge.

---

## 1. A Tool — something the assistant can *do*

A **tool** is a single, concrete action: search the web, read a file, run a
calculation, check how busy the graphics cards are. If a tool were a word, it
would be a **verb**.

The analogy: it's like a single app on a phone, or a single button on a control
panel. Each one does one well-defined job and hands back a result.

How it works in practice is pleasantly simple. Each tool is just a small file
dropped into a folder. The assistant is shown a menu of what's available, and
when it wants one, it writes a structured request ("use the *web search* tool
with the query *Zürich weather*"). The loop runs that tool for real, gets the
answer, and hands it back to the assistant to continue. Adding a brand-new
ability is literally a matter of writing one new file and restarting — no
rewiring of everything else.

The key safety point: **the assistant never runs anything itself.** It only
*asks*. The loop is the gatekeeper that decides whether the request actually
happens — which is what makes the whole system safe to give real abilities to.

---

## 2. A Skill — a written playbook the assistant can *read*

A **skill** is not an action — it's **knowledge**. It's a written set of
instructions for handling a particular kind of task well: "here's how to put
together a polished slide deck," or "here's the disciplined way to tackle a big
coding project — plan first, work in small checkpoints, don't try to do it all at
once."

The analogy: if a tool is a power drill, a skill is the **instruction manual** or
a recipe card. It doesn't do anything by itself; it makes the person *using* the
tools far more competent and consistent.

Why have these separately from tools? Because not every task needs the same
guidance, and stuffing every instruction into the assistant's head at all times
would overwhelm it (and cost more). Instead, skills sit on a shelf and the
assistant pulls down the relevant one **only when a task calls for it** — reads
it, follows it, then moves on. It's a way to give the assistant deep, situational
expertise on demand without bloating it.

So: a **tool** changes what the assistant *can do*; a **skill** changes how
*well and how sensibly* it does it.

---

## 3. MCP — a universal adapter for plugging in outside services

**MCP** stands for *Model Context Protocol*. The plain-language version: it's a
**standard-shaped plug**, like USB, for connecting your assistant to outside
software — someone's calendar, a company database, a project tracker, a document
store.

The problem it solves: before a common standard existed, every time you wanted
your assistant to talk to a new service, someone had to hand-build a custom
connector for that one service. Tedious and fragile. MCP is an agreed-upon shape
for those connections, so any service that "speaks MCP" can be plugged in without
bespoke wiring — the same way any USB device works in any USB port.

In this particular project, MCP is left as a **deliberately empty slot** — the
socket is there and labelled, but a connector hasn't been built yet. That's a
common and sensible choice: you reserve the standard spot so that *later*, adding
connections to outside tools is a clean plug-in rather than a redesign.

The distinction worth holding onto: a **tool** is usually something built into
your assistant; **MCP** is the standardized doorway for reaching tools and data
that live *outside* it, owned by someone else.

---

## 4. RAG — giving the assistant a searchable private library

**RAG** stands for *Retrieval-Augmented Generation*, which is a mouthful for a
simple idea: **let the assistant look things up in your own documents before it
answers.**

The analogy: it's the difference between an assistant answering from memory and
one who first walks to a **filing cabinet of your material**, pulls the few pages
actually relevant to your question, reads them, and *then* answers — citing what
it found.

Why it matters: the AI's built-in knowledge is general, frozen at its training
date, and knows nothing about *your* private files, your company's handbook, or
last week's notes. RAG bridges that gap. You feed in your documents; the system
quietly indexes them so they can be searched by *meaning* rather than just exact
words. When you ask a question, it finds the most relevant passages, slips them
into the conversation, and the assistant answers grounded in **your** material
instead of guessing.

There are two quality steps under the hood, worth knowing by name only: a fast
**search** finds a shortlist of maybe-relevant passages, and a slower, pickier
**re-ranking** step re-reads that shortlist to push the genuinely best matches to
the top. Fast-but-rough, then slow-but-accurate — a sensible pattern you see all
over computing.

So RAG is how the assistant gets **knowledge of your world**, the same way a tool
gives it *abilities* and a skill gives it *method*.

---

## How they fit together

Picture a single request — *"summarise our Q3 policy changes and email me the
key risks."* A well-equipped assistant might:

- pull down the relevant **skill** (the playbook for writing a clear summary),
- use **RAG** to find the actual Q3 policy documents in your library,
- call a **tool** to draft and format the result,
- and reach an outside service through **MCP** to actually send the email.

Four different mechanisms, each doing the thing it's best at, coordinated by that
humble loop in the middle.

---

## The quiet safety rails (worth a mention)

The guide is just as concerned with keeping this assistant *trustworthy* as
capable. Four ideas run underneath everything:

- **Budgets** — every task has hard ceilings on time, money, and effort, so it
  can never quietly spiral. It stops and says so when it hits a limit.
- **Tracing** — every single step is written down, so you can always go back and
  see exactly what it did and why. Nothing is mysterious after the fact.
- **A confirmation gate** — risky actions pause and ask for your explicit
  approval before proceeding.
- **Privacy boundaries** — anything marked sensitive cannot be quietly handed off
  to an outside cloud AI without your say-so.

---

## The one-line takeaway

The intelligence isn't a magic box — it's a simple, transparent loop you control,
given **abilities** (tools), **method** (skills), **outside reach** (MCP), and
**knowledge of your own world** (RAG), all kept honest by budgets, logs, and your
explicit permission.
