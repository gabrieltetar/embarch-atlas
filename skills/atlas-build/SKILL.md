---
name: atlas-build
description: Builds an embarch-atlas for one firmware target at one git commit - its own worktree and build, the pin/net map, the mismatch report and MAP.md - joined to the translated hardware documents, then runs the second, independent look on the facts that atlas uses. Run when the engineer asks for an atlas of a given commit, usually HEAD.
disable-model-invocation: true
argument-hint: "<target> [<commit>, default HEAD]"
---

# atlas-build

An atlas is pinned to **one target + one firmware commit** and never edited
afterwards. A later commit gets a new atlas (`/atlas-update`). Contract:
`DESIGN.md` in the embarch-atlas repo.

**Hard rules:**
- The engineer's working tree, branch, index and `embarch/build/` dirs are never
  touched. The build runs in a git worktree under `embarch/atlas/.work/`, and
  `git worktree add/remove` there is the only git write allowed.
- No flashing, no hardware. Never fetch anything.
- Mismatches are **candidates** for the engineer, never facts. Say which source
  each side comes from.

Current state:
!`embarch-atlas status 2>&1 | head -40`

## Steps

```
- [ ] 1 preflight  atlas.toml has the target; inventory is fresh (else run /atlas-translate-docs first); boards/<label>/netlist.json exists for the target's schematic_boards
- [ ] 2 build      embarch-atlas build <target> <commit>        (minutes; fails loudly if the build did not come from the worktree)
- [ ] 3 join       embarch-atlas join <atlas>                   (dt.json, in_use.json, pins, checks, mismatches, MAP.md)
- [ ] 4 RM         embarch-atlas mcu rm-sections <atlas>; if the worklist has sections with no frozen distillation, run /atlas-translate-docs rm-worklist <atlas>
- [ ] 5 2nd look   embarch-atlas second-look queue <atlas>; process the queue (below); embarch-atlas second-look record <atlas> <verdicts.jsonl>
- [ ] 6 re-join    embarch-atlas join <atlas>                   (so the report reflects the second look)
- [ ] 7 report     lead with the checks that passed, then candidates by class; the atlas id; stale inputs; open questions
```

## Step 5: the second look

The queue lists facts this atlas **uses** that need an independent look: figure-only
facts, `llm-unverified` records, limits whose unit is printed once per row group,
condition-bearing limits, quirks and register semantics. Items with a frozen
verdict for the same content hash are skipped.

For each queue item, spawn a **fresh subagent** (Agent tool) that has not seen the
first pass. Give it only the document path, the page(s), and **what to extract**
(symbol, parameter, condition, or the quirk's question), **never the first-pass
value**. It reads the page image (`Read` with `pages=`) and returns its own
value, unit, condition and a verbatim quote. You then compare:

- agree: verdict `confirmed`; the record becomes `llm-verified` (or stays
  `deterministic+crosschecked`);
- disagree: verdict `conflict`; keep both readings, and the fact appears in the
  mismatch report as a document-reading conflict;
- the subagent cannot find it: verdict `unresolved`; the record stays `llm-unverified`.

Batch items that share a page into one subagent. Keep the verdict file as JSON
Lines: `{"id", "verdict", "value", "unit", "cond", "q", "p", "reader": "second-look"}`.

## Step 7: what the engineer sees

`checks.md` first: how many checks ran and passed, per class. Then the candidates,
ranked, with evidence and "what would settle it". Expected ones (SWD, NRST,
test-point-only nets) are folded into counts. End with the questions only the
engineer can answer: mating rules, rework, which build is the reference.
