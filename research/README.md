# embarch-atlas, document edition: research synthesis

**Status:** draft, 2026-09-29.

These are the generalized, public-safe copies of the atlas document-research
notes. Full-fidelity versions — with the target project's real part numbers,
board names, file paths and spike scripts/outputs — live in that project's
machine-local atlas folder, not in this repo.

Eight research territories ran in parallel (16 agents, 54 minutes). Each note
was then checked by an independent verifier, which re-ran the spike scripts
and re-opened the sources: 107 load-bearing claims checked, 71 confirmed, 29
corrected in place (marked `[corrected in verification]`), 0 refuted, 7
unverifiable. Every note ends with its own Verification section.

This file is the entry point. It says what the notes settled, where they
disagree, what they found on the proof-of-concept product, and what the
owner has to decide before the three skills get drafted. The notes carry the
evidence; this file carries none of its own.

## Index

| Note | Territory | The one thing to take from it |
|---|---|---|
| [10](10-doc-conversion.md) | PDF -> text for datasheets, reference manual, requirements doc | The text layer holds every number exactly; learned converters fail on table *structure*, and some corrupt values |
| [11](11-datasheet-distillation.md) | Board-independent datasheet distillation | Records as source of truth, markdown rendered from them, deterministic checks; the quirks section is the product |
| [12](12-schematic-pdf.md) | Schematic PDF -> netlist, multi-board | Manufacturing-package PDFs embed the exact netlist and BOM; geometry reproduces it 526/529; vision reads pin rows, not wires |
| [13](13-mcu-reference.md) | MCU reference manual, datasheet, register file, errata, pins, clocks | Structured-first: register file + header + vendor configurator database; the reference manual adds constraints and prose for chapters in use only |
| [14](14-llm-browsing-format.md) | Is markdown best to browse? Serving | Yes as a container, *with row rules*; table of contents -> find -> section -> page; 7 MCP tools |
| [15](15-cross-source-join.md) | Pin/net map and mismatch report | First pin/net map for the proof-of-concept board; 11 of 13 checkable hand findings reproduced mechanically |
| [16](16-landscape-update.md) | Who else does the document side | PDF-schematic ingestion, reference-manual selection by build and requirements-to-firmware tracing are unclaimed |
| [17](17-ingestion-procedures.md) | Ingestion as a product | Manifest, missing-docs question list, provenance classes, frozen LLM outputs, resumable steps |

Spikes, with re-run instructions in each README, live only in the
machine-local, full-fidelity copy of this research.

## The picture

Three findings reshape the plan.

1. **A schematic in this class of manufacturing-package PDF needs no machine
   learning.** The two exported PDFs carry the whole netlist in their
   bookmark tree and every part's parameters (part number, value,
   fitted/not-fitted) in JavaScript popups. That gives 793 parts, 2,016 pins
   and 365 nets, every pin on a net, in about 5 s, deterministically. Four
   notes found this independently (12, 15, 16, 17). No surveyed product or
   open tool reads it (16).
2. **For datasheets, the PDF text layer is the numeric oracle, and the
   LLM's job is structure.** Plain layout-text extraction placed every scored
   number correctly. The learned converters lost or corrupted structure; two
   of the weakest scored 0 on the electrical tables outright, and one glued
   digit runs together (10). Claude reading page images transcribed 136/136
   facts (10). A checker that anchors every quote and number to the cited
   page's text caught every injected fabrication (11).
3. **Most MCU knowledge is already structured.** The register-description
   file (SVD or equivalent), the CMSIS-style header and a locally installed
   vendor configurator database cover registers, interrupts, pins and
   alternate functions, DMA and the clock tree. The reference manual is
   needed for field constraints and for the prose of the chapters the build
   uses: roughly a quarter of the manual by page count once the register
   sections are left to the register file (13). The vendor's own
   register-description file has real defects, so register facts need a
   vote across sources (13).

So the pipeline is **deterministic first, with the LLM reading pages**: the
text layer, the outline, the embedded netlist, the register file and the
build artifacts come from scripts; Claude Code distils prose and tables from
page images and the text layer. Scripts then check every number it wrote
against the page.

## What the research settled, per stage

### Intake (17, 12, 15)

- **Inventory by script:** type, part number, document number and revision
  from vendor conventions (multiple vendor conventions covered, 8/8 vendor
  PDFs in the sample), a hash per file and per page. About a dozen files in
  about 5 seconds, byte-identical across runs.
- **The first thing atlas shows the engineer is a question list, not a
  document:** missing datasheets ranked by how directly firmware sees the
  part, the errata sheet, board labels, the connector mating rule. Roughly
  two dozen active manufacturer part numbers on the boards have no
  datasheet, depending on how they are counted across notes (12, 15, 17).
- **Ask the engineer to keep "net information" and "component parameters" on
  in every release PDF** (12, 15, 17). It turns a vision problem into a
  parse.

### Schematics (12, 15)

- **Ingest ladder, first match wins:** native EDA project -> embedded PDF
  netlist (this class of tool's outline plus JavaScript, or a competitor's
  net bookmarks) -> geometric tracing on a vector PDF -> vision on crops.
  Each rung cross-checks the one above; geometry reproduced the embedded
  netlist on 526 of 529 child-sheet nets with 0 false pin pairs (12).
- **Vision reads pin rows, not wires:** 75/75 pins on IC slices, but 20 of 26
  nets right when wires have to be followed (12).
- **Keys are board-qualified** (`A:U3`): a large fraction of reference
  designators and net names collide between the two boards (12).
- **Board-to-board interfaces are engineer-declared records** with a machine
  score. The tool proposes a reversed pin mapping on the main connector (30
  of 31 named signal pairs agree; straight mapping 0) and a reversed mapping
  on a second connector (8/8). The owner confirms (12, 15).

### Datasheets (10, 11)

- **Base layer:** per-page layout-text extraction plus word bounding boxes,
  a few seconds for a whole project's documents. It is the raw tier behind
  every citation and the oracle for every check (10).
- **Records are the source of truth; tier-2 markdown is rendered from
  them**, so it cannot drift. A dozen-plus checks (quote on page, number in
  quote, independent column re-derivation, min <= typ <= max, register widths
  and resets, every markdown number anchored or tagged) run in about 1.5 s a
  part. Injected errors are caught at 88-100% for most classes, but only 25%
  of unit-prefix errors on tables whose units print once per row group, and
  58-65% of changed conditions. Those two, plus figure-only facts, need a
  targeted second look at the page image (11).
- **One deterministic register parser per vendor template.** One vendor's
  template took under an hour, matched the LLM with 0 diffs, and found 6
  inconsistencies inside that datasheet's own bit diagrams and field tables
  (11).
- **Quirks are the product.** 43 found across two parts; 25 would be lost
  without a quirks section (11).
- **A figure-only fact is its own, weakest provenance class** (literature:
  83% for figure digitisation vs 95% for tables) (11).
- **Canonical table form:** one sub-row per line, symbol and unit repeated,
  footnotes attached to the rows they qualify. It fixes packed cells,
  fill-down errors and merged units, and makes a text search return complete
  rows (10, 14).

### MCU (13)

- Register file for registers, **three-source vote** (register file, header,
  reference manual) emitted as `conflict` facts: peripheral-level diffs found
  about two dozen differences, most of them register-file defects. At least
  one peripheral's status/reset behaviour is missing from the register file
  entirely.
- Pins/alternate-functions from the devicetree pin-control layer plus a
  vendor configurator database (near-total agreement with the datasheet).
- "In use" is three-state per build: devicetree-okay / device instantiated /
  touched by code.
- Candidate deterministic reference-manual-to-markdown tool exists in the
  open-source MCU-data ecosystem, permissively licensed.

### Format and serving (14)

- **Markdown answers the owner's question**, but row rules decide whether a
  text search works at all (34/34 rows found vs 0/34 for a structured
  serialization format). HTML is never a reading format. Once the right page
  is in context, format barely matters. The hard part is navigation.
- **Loop:** table of contents -> lexical find -> read the section -> view the
  page to verify. No published method beats it for a tool-using agent; the
  strongest published refinements are variations on it. No embeddings, no
  graph-based retrieval.
- **Every schematic PDF in the sample already has a bookmark outline**
  (dozens to thousands of entries), so navigation is built deterministically
  before any LLM step.
- **A page image via the Read tool costs about 1.5K Claude tokens**, less
  than the distilled markdown of the same page. The tier-3 check is cheap
  enough to run routinely.
- **Budget in real tokens:** measured Claude token counts run noticeably
  higher than a common open tokenizer's count on the same text — plan with a
  measured multiplier, not an assumed one.
- **Layout and tools.** Tier files are plain markdown on disk, read directly.
  MCP carries only what files cannot: 7 tools, mirrored as CLI verbs.

### Joins and the mismatch report (15)

- Pin/net map for all 100 pins of the main MCU package, about 10K tokens,
  with a much smaller always-loadable slice.
- Device identity by address strap: full agreement between extracted
  hardware identity and devicetree declarations for the I2C devices checked.
- **Report checks that passed, not only candidates.** Most raw candidates
  turned out to be "connected, no devicetree entry" (an unused display,
  an unused port), which would bury the valuable output if not filtered.
- More than a dozen mismatch classes catalogued with the sources each needs
  (15).
- **Transparent-part maps are needed even for sums.** A current-monitor
  part's internal shunt hid a significant fraction of a bulk capacitance
  total on one power rail.

### Landscape (16)

- **Unclaimed:** PDF-schematic ingestion (one surveyed embedding-based tool
  takes no PDF at all); reference-manual chapter selection by build;
  requirements-to-existing-firmware tracing; an integrated "fully local
  except the LLM" product.
- **Partly claimed:** board-independent datasheet distillation (one MIT
  project); cross-source mismatch (one competitor claims it, publishes no
  accuracy numbers).
- **Adopt:** one open project's evidence schema and trust gates (MIT);
  another's table-of-contents/read-chapter pattern (Apache-2.0); a
  schematic-extraction project's eval method. **Build:** the manufacturing-
  package-PDF parser; reference-manual chapter selection.

## Where the notes disagree (sort these first)

| Topic | Disagreement | Why |
|---|---|---|
| Reference-manual chapters in use | About a quarter of the manual by page count (13) vs a higher fraction and a fixed set of whole chapters (16) | Different selectors: one maps enabled peripherals to sections, the other maps devicetree-compatible strings to whole chapters plus a fixed core set. One rule has to be chosen |
| Table converter default | A markdown-oriented AGPL converter (best single tool), a permissive mid-tier converter (packed cells), a permissively-licensed reference-manual-to-markdown tool, or text layer plus Claude on page images | Licence is the owner's call (Q1 below). The measured default is the permissive mid-tier converter behind a text-layer check |
| Missing-datasheet count | Different counts across notes | Different denominators (see Intake) |
| Ground-truth-free checks | One note says its checks flag catastrophic failures; its verifier says no thresholds exist yet and one check false-positives | The checks exist but cannot gate a fallback chain until thresholds are set |
| Token factor | An assumed prior multiplier vs a higher measured median | One note measured real token counts on real text; use the measured figure |

## Found on the proof-of-concept product (candidates for the engineer, never facts)

Every item below is extracted or inferred, not confirmed.

1. **One board half has no power-rail source of its own** for a key supply
   rail. Its supply net reaches only the mating connector and loads, which
   mates to the other board's supply pin under the inferred mirror rule.
   That appears to contradict a back-feed warning in an existing hardware
   notes file.
2. **A current-monitor alert signal on one board meets the same signal with
   an added active-low prefix on the other board**, across the connector —
   likely a naming inconsistency rather than a wiring one.
3. **Two driver claims go beyond the datasheet:** one driver comment states
   a register field is "set by an external resistor" where the datasheet
   states a fixed reset value; another driver comment states a fault
   behaviour that matches a figure but contradicts a literal reading of a
   footnote.
4. **Some peripherals are enabled in devicetree with no driver compiled**, so
   their pin control is never applied. The MCU's own boot-time hardware also
   removes certain protective pull-downs automatically.
5. **Several external-interrupt lines are shared** across unrelated signals,
   consistent with existing hardware notes.
6. **Datasheet traps:** an over-voltage-protection part can trip inside its
   own stated operating range; a buck-boost part's over-current mask bit
   resets against its own stated rule; the same part prints binary register
   resets with a hex-looking suffix that is easy to misread.
7. **An existing hand-written pin-extraction script misses several pins**
   (the boot-select pin among them) and a chunk of its output lines carry
   leaked EDA-tool anchor tokens.
8. **A local build directory was not built from the checked-out commit**
   but from an older development-branch tip, while the working tree sits on
   an unrelated feature branch. This is live evidence for the "own worktree
   build at the pinned commit" design decision (15).

## Open questions for the owner

Grouped by what they block.

**Blocked drafting the skills — already answered by the owner, see
[00-scope.md](00-scope.md):**

1. **Licences.** Is a copyleft PDF library acceptable for a local internal
   tool, and a vendor's proprietary configurator database as an input?
2. **Second-look policy.** Should figure-only facts, unit prefixes and
   conditions get a second read from a fresh subagent before they reach a
   card? May the page-image check run automatically, and on how many pages?
3. **Record format.** A schema-checked structured format, or line-delimited
   records? One facts file per project or one per part?
4. **How the map loads.** An always-imported local file every session, or
   only through a skill? Should the MCP server be always-loaded?
5. **Reference-manual selection rule** and depth. Enabled peripherals'
   sections, or whole chapters? Store the full manual text as the raw tier,
   or only chapters in use?

**Hardware facts to declare (engineer):**

6. Confirm the reversed connector pin mapping, and which spelling of the
   cross-board alert signal is right.
7. Is one board half really unpowered on its own, and what did an
   "unpowered" bench observation mean in practice?
8. Confirm unit-to-unit mating order for the mechanical stack, and whether
   the mechanical-connection parts drawn not-fitted in this print are really
   fitted in production.

**Intake:**

9. Supply the missing errata sheet and the firmware-visible missing
   datasheets, or accept "standard function" cards for jellybean logic parts,
   marked engineer-declared?
10. Requirements document vs firmware: at least one numeric spec (an
    operating range or a capacity figure) disagrees between the requirements
    document and the devicetree/firmware.

**Scope of the mismatch report:**

11. Are enabled-but-undriven peripherals, an unused secondary port and
    unused display pins "not yet in firmware" (a count) or candidates for
    the report?
12. Driver-vs-datasheet contradictions: in the part's quirks, in the
    mismatch report, or both?

## Next: the three skills

Owner decisions in [00-scope.md](00-scope.md). What each skill can reuse:

| Skill | Steps it owns |
|---|---|
| **translate-docs** | intake manifest and diff; missing-docs questions; embedded-netlist + BOM parse per board; datasheet distillation (prep -> LLM records -> render -> check -> targeted second look); reference-manual outline + register-file vote; requirements-document extraction with quotes |
| **build-atlas** | worktree at (board, commit); build; extract devicetree/config/compile-commands/ELF; reference-manual chapters in use; joins (pin/net map, device identity, devicetree values); mismatch report with passed checks; a compact always-loaded map, a pin slice, a facts index; a stamp of (board, commit, doc-manifest hash) |
| **update-atlas** | new commit or changed docs -> new atlas; reuse translations by content hash; re-translate only changed pages; diff two atlases |

Design point the research exposed: **reference-manual chapter selection
depends on the build.** Settled: build-atlas asks for the chapters of
peripherals in use, and translate-docs distils them lazily, cached by
content hash.
