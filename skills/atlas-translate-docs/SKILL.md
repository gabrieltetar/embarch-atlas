---
name: atlas-translate-docs
description: Turns the hardware documents the engineer supplied for this firmware project (schematic PDFs, component datasheets, the MCU reference manual, the PRD) into embarch-atlas's tiered, page-cited, cross-checked distillations and a schematic netlist. Run when documents are added or changed under the project's sources dir, or when atlas-build asks for reference-manual sections.
disable-model-invocation: true
argument-hint: "[all | <file in sources> | rm-worklist <atlas>]"
---

# atlas-translate-docs

Converts documents; it never interprets the firmware. Contract: `DESIGN.md` in
the embarch-atlas repo (`embarch-atlas --help` works from anywhere).

**Hard rules, every step:**
- **Never fetch a document**, and never send document or firmware content to any
  service but this session. A missing document becomes a question for the engineer.
- Write only under the project's `embarch/atlas/`.
- Every number you write is on the page you cite, or is tagged `(derived)`,
  `(figure)` or `(inference)`. Never invent a citation. Never delete a failing
  record; fix it or leave it `llm-unverified`.
- Never state a board fact you inferred as confirmed. Board facts come from the
  schematic analysis or from the engineer.

Current state (injected when the skill loads):
!`embarch-atlas status 2>&1 | head -40`

## Steps

Copy this checklist into your reply and tick it as you go. Stop at any step that
produces a question the engineer has to answer first.

```
- [ ] 0 config     embarch-atlas init <fw>   (only if atlas.toml is missing; then ask the engineer for boards/targets/MCU)
- [ ] 1 inventory  embarch-atlas inventory
- [ ] 2 questions  read inventory/manifest.md: unknown kinds, missing revisions, image-only pages -> ASK
- [ ] 3 schematics embarch-atlas schematic <pdf> --board <label>   (one per [[boards]] entry; labels come from atlas.toml, never guessed)
- [ ] 4 interfaces embarch-atlas interfaces; show the proposed mating rules with their scores; ASK the engineer to confirm or correct
- [ ] 5 missing    embarch-atlas missing; show inventory/missing.md; ASK for the files or "not needed"
- [ ] 6 distil     per changed document, one per pass (below)
- [ ] 7 report     what was translated, what failed checks, the open questions
```

Step 1 diffs against the previous manifest. **Unchanged documents skip steps 3–6.**
For a changed PDF, run `embarch-atlas doc prep`, then `embarch-atlas doc thaw-check
<doc_id>`: it lists the frozen units whose pages did not change (kept) and the ones
that must go back through the LLM. Each procedure ends with `embarch-atlas doc
freeze <doc_id>`, which records the LLM-written files in `cache/llm/`.

## Step 6: distil one document

Pick the procedure by the manifest's `kind`:

| kind | Procedure |
|---|---|
| datasheet | `reference/datasheet-procedure.md` |
| reference-manual | `reference/rm-procedure.md`. RM prose is distilled **lazily**, only for the sections in the worklist that atlas-build writes (`docs/<rm>/worklist.json`), because what is "in use" depends on the build. With no worklist yet, run only `doc prep` and `mcu svd` / `mcu vote` |
| prd | `reference/prd-procedure.md` |
| schematic | done in step 3, nothing to distil |

Every procedure ends the same way:

```
embarch-atlas doc render <doc_id>
embarch-atlas doc check <doc_id>        # repeat: fix the flagged records, re-run, until it exits 0 or only llm-unverified items remain
embarch-atlas doc freeze <doc_id> --model <model>
```

A document whose `doc check` cannot reach exit 0 because of a checker limitation (not a
wrong record) is reported with the limitation named, so the tool gets fixed; do not
bend records to satisfy a checker bug.

Report per document: records per kind, anchored %, what is `llm-unverified` and
why, card and section token sizes, how many image Reads it took.

## The second look is not here

A fact's second, independent look happens in **atlas-build**, on the facts that
atlas actually uses (owner decision, 2026-09-29). Do not run second passes in
this skill.

## When atlas-build calls back

`/atlas-translate-docs rm-worklist <atlas>` distils only the RM sections listed in
that atlas's worklist that have no frozen distillation yet (key = content hash of
their pages), then renders and checks them.
