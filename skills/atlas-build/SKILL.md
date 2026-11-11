---
name: atlas-build
description: Builds an embarch-atlas for one firmware target at one git commit - its own worktree and build, the pin/net map, the mismatch report and MAP.md - joined to the translated hardware documents, then runs the second, independent look on the facts that atlas uses. Run when the engineer asks for an atlas of a given commit, usually HEAD.
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
- [ ] 2 build      embarch-atlas build <target> <commit>        (minutes; fails loudly if the build did not come from the worktree; a target with `no_board = true` skips the west build, see below)
- [ ] 3 join       embarch-atlas join <atlas>                   (dt.json, in_use.json, pins, checks, mismatches, MAP.md)
- [ ] 4 RM         embarch-atlas mcu rm-sections <atlas>; if the worklist has sections with no frozen distillation, run /atlas-translate-docs rm-worklist <atlas>
- [ ] 5 2nd look   queue --top N, one blind subagent per batch (prompt), compare, record (below)
- [ ] 6 re-join    embarch-atlas join <atlas>                   (so the report reflects the second look)
- [ ] 7 report     lead with the checks that passed, then candidates by class; the atlas id; stale inputs; open questions
```

## A target with no Zephyr board yet

When the PCB's board files are still being written, the target says so in `atlas.toml`
(`no_board = true`, with no `board`; `build --no-board` states it for one run). A forgotten
`board` is still an error, so never set `no_board` to get past one. `build` then writes an atlas
whose stamp says there is no build and why, with no worktree and no west build; step 3 joins
from the schematic and the datasheets, step 4 (RM worklist) has nothing to read and is skipped.
`status` and `MAP.md` list what is absent and what is reduced; say so in the step 7 report and
do not ask that atlas for `config`, `dt_node`, `footprint` or `stacks`. Once the target has a
board, set it, drop `no_board`, and `build` supersedes the atlas.

## Step 5: the second look

```
embarch-atlas second-look queue <atlas> --top <N>      # ranked; groups the N most important items into page batches
embarch-atlas second-look prompt <atlas> <batch#>      # the blind reader's instructions for one batch
embarch-atlas second-look compare <atlas> readings.jsonl -o verdicts.jsonl
embarch-atlas second-look record <atlas> verdicts.jsonl
```

The queue lists facts this atlas **uses** that need an independent look, ranked: straps and
pins the join reads first, then figure-only and `llm-unverified` facts, units printed once
per row group, quirks, register semantics, condition-bearing limits. Items with a frozen
verdict for the same content hash are skipped, so each round only pays for new facts. Pick
`N` from the budget: one batch is one set of pages, about 1.5K tokens a page to view.

For each batch, spawn a **fresh subagent** (Agent tool) and give it exactly the output of
`second-look prompt`: the PDF, the pages and what to extract, **never the first-pass
values**. Collect its JSON Lines into `readings.jsonl`. **The verdict is not yours to
make**: `second-look compare` decides it deterministically (numbers equal, same unit,
same strap address, same bits; for quirks and rules the reader's supports/contradicts):

- `confirmed`: the record becomes `llm-verified` evidence (or stays `deterministic+crosschecked`);
- `conflict`: both readings are kept, and the fact appears in the mismatch report as a
  document-reading conflict;
- `unresolved`: the reader could not find it; the record stays `llm-unverified`.

Batch independent pages in parallel subagents; do not show one reader another's output.

## Step 7: what the engineer sees

`checks.md` first: how many checks ran and passed, per class. Then the candidates,
ranked, with evidence and "what would settle it". Expected ones (SWD, NRST,
test-point-only nets) are folded into counts. End with the questions only the
engineer can answer: mating rules, rework, which build is the reference.
