---
name: atlas-translate-docs
description: Turns the hardware documents the engineer supplied for this firmware project (schematic PDFs, component datasheets, the MCU reference manual, the PRD) into embarch-atlas's tiered, page-cited, cross-checked distillations and a schematic netlist. Run when documents are added or changed under the project's sources dir, or when atlas-build asks for reference-manual sections.
argument-hint: "[all | <file in sources> | rm-worklist <atlas> | standard <doc_id> <section>...]"
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
- [ ] 3 schematics embarch-atlas schematic <pdf> --board <label>   (one per [[boards]] entry; labels come from atlas.toml, never guessed); read its "N still unnamed on 3+-pin parts": for a module whose pad names the PDF's text lacks, render the symbol's region of its page (netlist.json `pages`) to an image with PyMuPDF `get_pixmap(clip=...)` and view it with Read (no verb crops yet), read the names off it, record each with embarch-atlas declare-pin-name <board> <ref> <pin> <name> --prov llm-unverified --note <sheet/crop>, then ASK the engineer to confirm and re-record with --prov engineer-confirmed; never by editing netlist.json or boards/<label>/pin_names.json
- [ ] 4 interfaces embarch-atlas interfaces; show the proposed mating rules with their scores; ASK the engineer which connectors mate, and record the answer with embarch-atlas declare-interface <board>:<ref> <board>:<ref> --rule <rule> (or --pin-map 1:6,2:5,...) --note <source>, never by editing boards/system.json
- [ ] 5 missing    embarch-atlas missing; show inventory/missing.md; ASK the engineer to confirm or reject its table of proposed matches (--confirm-all, or --confirm / --reject per row), then ASK for the files still missing or "not needed"
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
| appnote | `reference/datasheet-procedure.md`. An application note, user guide or technical note supports a part and never stands in for its datasheet, so its card says which part it supports, with the part numbers on its `aka:` line |
| reference-manual | `reference/rm-procedure.md`. RM prose is distilled **lazily**, only for the sections in the worklist that atlas-build writes (`docs/<rm>/worklist.json`), because what is "in use" depends on the build. With no worklist yet, run only `doc prep` and `mcu svd` / `mcu vote` |
| prd | `reference/prd-procedure.md` |
| standard | `reference/standard-procedure.md`. Distilled **lazily**: the card first, then only the sections in `docs/<doc>/worklist.json` (`embarch-atlas doc sections <doc> --add ...`); ASK the engineer which sections the firmware implements |
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

`/atlas-translate-docs standard <doc_id> <section>...` adds the sections to the standard's
worklist (`doc sections --add`) and distils the ones not frozen yet, by
`reference/standard-procedure.md` steps 3-7.
