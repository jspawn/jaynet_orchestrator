---
name: mermaid
description: Render and verify diagrams with mermaid-cli (mmdc) — flowcharts, architecture sketches, sequence diagrams. Load when the user asks for a diagram/chart/drawing, when documentation needs a visual, or when a ```mermaid block should be checked before shipping.
---
# Mermaid diagrams with mermaid-cli

Mermaid source renders as a diagram on GitHub automatically (a fenced
\`\`\`mermaid block in markdown). For anything else — PNG/SVG files, or
VERIFYING what a diagram actually looks like — use mermaid-cli (`mmdc`).

## Workflow

1. Write the source to a `.mmd` file (fs.write). For a diagram that already
   exists in a markdown file, extract the fenced block first:

       awk '/^```mermaid$/{f=1;next} f&&/^```$/{f=0} f' README.md > /tmp/d.mmd

2. Render it (code.run):

       mmdc -i /tmp/d.mmd -o /tmp/d.png -b white      # PNG to eyeball
       mmdc -i /tmp/d.mmd -o diagram.svg              # SVG for docs

3. **Always view the render** (read the PNG back) before delivering.
   Crossed edges, tall skinny sprawl and overlapping labels are only visible
   in the render — fix by regrouping into `subgraph`s and shortening labels,
   not by nudging coordinates (mermaid has none).

4. If `mmdc` is missing (`which mmdc`): tell the user to install it
   (`npm i -g @mermaid-js/mermaid-cli`) — do not silently skip the verify
   step by "trusting" the syntax.

## Syntax that survives renderers

- Quote any label with special characters: `ID["label (with parens)"]`.
- Line break inside a label: `<br/>`.
- Subgraph with a title: `subgraph NAME["the title"] ... end`.
- Prefer `flowchart TB`; use `LR` only for narrow chains. Keep nodes under
  ~15 — split bigger systems into two diagrams.
- Avoid `<-->` (needs newer renderers); use two arrows or one direction.
- Edge labels: `A -->|"words"| B`; thick arrow for the headline flow: `==>`.
