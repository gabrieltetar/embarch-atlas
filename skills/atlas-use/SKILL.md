---
name: atlas-use
description: How to answer a hardware question in a firmware repo that has an embarch-atlas (an `embarch/atlas/MAP.md` exists) - which pin, net, part, register, limit, timing or quirk; what the schematic, a datasheet or the MCU reference manual says; whether the devicetree or a driver agrees with the hardware. Use before writing or reviewing a driver, a devicetree node or board code, and whenever a question depends on the hardware.
---

# Using the hardware atlas

The atlas holds the project's schematics, datasheets, MCU reference manual and PRD,
distilled, page-cited and joined to one firmware build. Read it; do not read the
PDFs whole, and never answer a hardware question from memory.

## The protocol: MAP → card → section → page

1. **MAP** (`embarch/atlas/MAP.md`, usually already in context). It names the atlas
   (target + commit), the parts on the board with their doc ids and card paths, the
   documents tied to no part (standards, specifications, the PRD), the MCU and the
   peripherals in use, the boards and their interfaces, and where the pin map and the
   mismatch report are. If its first lines say it is **stale** (HEAD moved past the
   atlas's commit), say so in your answer and suggest `/atlas-update`.
2. **Card** (`docs/<doc_id>/card.md`, ≤ 1.5K tokens). The part's role, key limits,
   register summary, the **quirks**, and a section index.
3. **Section** (`docs/<doc_id>/s/<secid>.md`). Tables follow one condition per row with
   the symbol and unit repeated, so `grep` returns whole rows. `docs/<doc_id>/facts.txt`
   has one fact per line (`<id> key=value; ...; cite=<doc> p<N>; prov=<class>`):
   `grep -r <symbol> embarch/atlas/docs/*/facts.txt` finds a fact across every part.
4. **Page**. To verify, or when a section is missing, `Read` the PDF with `pages=<N>`
   (about 1.5K tokens a page). The PDF is under the project's sources dir named in
   `docs/<doc_id>/doc.json`.

Board wiring is not in the datasheet docs. It is in `atlases/<id>/pins.md` (every MCU
pin → net → far-end part pin → DT node → driver) and `boards/<label>/nets.md`,
`parts.md`. Keys are board-qualified (`B:U3-12`, net `A:VBUS`).

## Rules

- **Cite every hardware fact** as `<doc_id> p<N>` (or `<board>:<ref>` for wiring).
  If you cannot cite it, say you do not know.
- **Respect provenance.** `prov=llm-unverified` or `src=figure` facts are leads, not
  truth: check the page before relying on one. `deterministic` and `llm-verified` facts
  can be used directly.
- **A quirk beats a table.** Read the card's quirks before trusting a limit or a
  register reset: conditions in footnotes, values that differ between tables,
  figure-only behaviour, silent defaults.
- **Mismatches are candidates.** `atlases/<id>/checks.md` lists what passed and what
  disagrees (schematic vs DT vs driver vs datasheet). Report a candidate with both
  sides' sources; never "fix" one side to match the other without the engineer.
- **Never infer a board fact.** If the atlas does not have it (a mating rule not yet
  declared, a part without a datasheet), say it is missing and name the document or the
  answer the engineer would need to supply. Missing documents are listed in
  `inventory/missing.md`.
- **The atlas is pinned.** It describes one commit. Code you are changing may already
  differ; say which you are describing.

## Quick lookups

| Question | Where |
|---|---|
| Which MCU pin drives X / what is on pin P | `atlases/<id>/pins.md` (or `pin_slice.txt`) |
| Part on a net, its MPN, DNP | `boards/<label>/nets.md`, `parts.md` |
| What feeds a rail, what enables it, a net no MCU pin reaches | `net_trace <board:net>` (MCP or `embarch-atlas q net-trace`) |
| A Kconfig symbol's value and why | `config <SYMBOL>`; add `build=<dir>` for a branch build |
| A devicetree node: properties and where each was set, driver, pins | `dt_node <label or path>`; `build=<dir>` likewise |
| Flash/RAM use, headroom in the code partition, what grew between two builds | `footprint` (`vs=<dir or pinned>`, `by=dir|file|symbol`) |
| Thread stack sizes and where each is defined | `stacks` (static); peaks on the board: `live_stacks` (pass `build` when the board runs another build than the atlas) |
| A limit / threshold / timing, or any behaviour | `find_fact` (records and distilled prose), or `grep -r <SYMBOL> embarch/atlas/docs/*/facts.txt embarch/atlas/docs/*/s/`, then the section |
| A register or field of an external part | `docs/<doc>/s/` register section, or `grep` the register name in `facts.txt` |
| An MCU register or field | `docs/<svd-doc>/s/<PERIPH>.md`; conflicts between the SVD and the RM are `conflict` records under the RM doc |
| What an MCU register holds on the running board | `reg_read PERIPH.REG` (or a whole `PERIPH`), listed when embarch-core answers: read through Core with the core halted about 20 ms, decoded field by field; a read-to-clear register is refused unless `allow_clear` |
| What an external part's register holds on the board | `reg_read <DT label>.<REG>` (or a whole small part): the shell's `i2c read` through Core, decoded against the datasheet; a device that does not answer is reported with its `device list` state |
| A command on the board's shell | `shell <command>` for reads (only the project's `read_only` commands run; it refuses the rest and names `shell_run`), `shell_run <command>` for anything that can change the board; both Core's exchange on the role's console, not a hand-written serial script. `reg_read_clear` is the write tool for a read that clears a flag |
| How an MCU peripheral must be programmed | `docs/<rm-doc>/s/<ch>.must.md`, then the section |
| Does DT/driver agree with the board | `atlases/<id>/checks.md` |
| A product requirement | `docs/<prd-doc>/s/`, `facts.txt` (`requirement` records) |
