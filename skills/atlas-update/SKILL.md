---
name: atlas-update
description: Brings embarch-atlas up to date after new firmware commits or changed hardware documents - re-translates only what changed, builds a new atlas for the new commit reusing every unchanged translation and verdict, and diffs it against the previous atlas. Run when the engineer asks to update the atlas or when status reports it stale.
disable-model-invocation: true
argument-hint: "<target> [<commit>, default HEAD]"
---

# atlas-update

Atlases are immutable: an update **builds a new one** and leaves the old one in
place, so two commits can always be compared. Contract: `DESIGN.md` in the
embarch-atlas repo.

Current state:
!`embarch-atlas status 2>&1 | head -40`

## Steps

```
- [ ] 1 what moved  embarch-atlas status: which atlas is current for <target>, what is stale (commit, documents, schematics)
- [ ] 2 documents   if any source changed: embarch-atlas inventory; then /atlas-translate-docs for the changed documents only
                    (per-page hashes: only changed pages go back to the LLM; frozen output of the rest is reused)
- [ ] 3 schematics  if a board PDF changed: embarch-atlas schematic <pdf> --board <label>; diff the netlist; if a connector's nets changed, re-run embarch-atlas interfaces and ASK the engineer to re-confirm the mating rule
- [ ] 4 bump        embarch-atlas update <target> <commit>
                    (build, join, RM worklist, second-look queue, diff; it stops at step 2/3 work if a source changed)
- [ ] 5 RM          if it reports RM units to distil: /atlas-translate-docs rm-worklist <atlas>, then update again
- [ ] 6 2nd look    the batches it wrote hold only the diff (new or changed facts): one blind subagent per batch,
                    exactly as atlas-build step 5 (prompt, compare, record), then embarch-atlas join <atlas>
- [ ] 7 report      diff-from-<previous>.md in the new atlas: what changed in firmware (pins, DT, drivers), in the
                    documents, in the board; new and resolved mismatch candidates; the backlog it did not batch
```

## Rules

- Never edit an existing atlas. Never delete one unless the engineer asks.
- A document revision that supersedes another keeps the old distillation in place,
  marked superseded in the manifest. Facts keyed by native names survive page
  moves, and their citations are re-derived.
- If nothing moved, say so and stop: no rebuild.
