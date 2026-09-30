# Ingestion as a product: how supplied documents become trusted atlas tiers

**Status:** draft, 2026-09-29.

Territory: the intake/conversion *process*. The engineer supplies documents, scripts do the
deterministic steps, coding-assistant sessions do the LLM steps, and every fact carries a citation, a
provenance class and a staleness key. Measured on a real project's hardware-document folder (14 files).

## Headline findings

1. **The Altium schematic PDFs carry a complete netlist in their PDF outline, so schematic intake is a
   script, not an LLM step.** A BOM/netlist extractor puts 725/725 board-A and 1,291/1,291 board-B
   component pins on a named net, in 1.7 s, with no OCR.
2. **Printed part numbers attach to designators through Altium's invisible per-designator tokens.** Every
   IC and transistor designator on both boards gets an MPN, and 11/11 checks against part identities the
   engineer stated elsewhere agree. The one error class found (a logic-gate MPN wrongly assigned to a
   1-pin test-point pad) is removed by a pin-count rule.
3. **The missing-documents checklist is 26 of 33 distinct MPNs, and 10 are firmware-visible (9 share a
   net with the MCU, 1 via a resistor) [corrected in verification].** Top two: 13x a small P-channel
   MOSFET (power-enable nets) and 8x a small single-gate demux IC (load-switch direction-select lines).
   The MCU errata sheet is also missing; the engineer gets the list as questions, nothing is fetched.
4. **Vendor document-control conventions make part number, document number and revision deterministic:**
   four vendor conventions cover 8/8 vendor PDFs, Altium title blocks and the PRD header cover the other 3.
   The inventory of 14 files (1,968 pages hashed) runs in 5.4 s, byte-identical across runs.
5. **Independent sources agree, which is the deterministic cross-check the trust bar asks for:** a
   hand-written pin script vs the bookmark outline (88/88 MCU nets), DT comments vs schematic (31/31),
   and a reversed pin map matching net names 30/32 on the main connector and 8/8 on a secondary header —
   supporting the owner's hedged "the two boards make one unit" without proving it.
6. **Quote-anchored LLM extraction can be checked by script; LLM trace-link recovery cannot be trusted
   unconfirmed.** 24/24 PRD statements' quotes were found on the cited page; a paraphrase control failed.
   IDs survived a simulated edit (21 unchanged, 2 revised, 1 retired, 1 new). In the literature, LLM
   requirement-to-code link recovery scores F1 0.24-0.53 on a published benchmark suite.
7. **Determinism has to be tested.** A double-run check caught a set-ordering bug that flipped 1 MPN
   assignment in the extractor script here; LLMs at "deterministic" settings still vary up to 15% in
   accuracy across runs (published study), so LLM outputs must be frozen by content hash and re-verified,
   never regenerated.

## 1. What a prior survey already settled (not re-researched)

- **Per-fact provenance is the gap in every standard:** keep a hardware/software architecture model's
  allowed-vs-actual split, a status beyond okay/disabled (`populated | dnp | defective | reworked`), and a
  scope tuple `(board, rev, build-id)` on every fact.
- **Stamp every fact with a git sha + dirty-tree hash + build-dir hash, and flag a mismatch:** a previously
  measured build was out of sync with its checkout (a few translation units missing); harvested engineer
  comments need a staleness check (§6).
- **The core must be deterministic, with evidence on every node:** LLM-extracted graphs hallucinate edges
  (a repo-comprehension survey's graph-RAG comparison); this note carries that rule to documents — parse
  what can be parsed, and give the LLM only what cannot be.

## 2. Intake: inventory and manifest

An inventory script produces a manifest plus a per-page-hash sidecar file. What the detectors found (all
deterministic, metadata plus first and last page):

| File class | Kind | Doc no. / rev / date | Evidence |
|---|---|---|---|
| charger IC / two other power-IC datasheets | datasheet | vendor doc-numbers / rev letters / dates | pdfinfo author field + p1 banner (same vendor convention) |
| MCU datasheet / reference manual | datasheet / RM | vendor doc-number / rev 6 / date | last-page revision string, revision-history table |
| fuel-gauge IC datasheet | datasheet | vendor doc-number / rev 2 / date | p1 doc-number/rev/date line |
| two load-switch datasheets | datasheet | (no doc no.) / rev letter+number / dates | footer revision string |
| two schematic exports | schematic | rev letter / date, 7 + 10 sheets | Altium title blocks |
| product requirements document | PRD | version 3.0 / date | header + version-history table |

Intake facts the manifest surfaced:

- **Download origin.** 9 of 14 files carry a Windows mark-of-the-web sidecar with a host URL (chip
  vendor's site x3, module vendor's site x2, analog vendor's site, a code-forge spreadsheet, a search
  engine's redirect x2 for the MCU datasheet/RM) — partial provenance for free, without fetching.
- **Image-only pages.** Only the PRD has pages with an image and under 20 words (six pages); every
  datasheet, the RM and both schematics have a full text layer. On one such PRD page: a page render at 80
  dpi was **illegible**, while the embedded image extracted at its native size (~1000x800, ~150 ppi) was
  **fully legible**, at ~980 visual tokens by the vendor's formula. Procedure rule: extract the embedded
  image; never downsample a page render below the image's own ppi.
- **The PRD's own table of contents is stale:** 15 of 30 entries point 1-3 pages early (footers match
  physical pages). Rule: cite the physical PDF page index, never the TOC.
- **Supersession inside a file.** A logic-table review spreadsheet has two tabs with the same truth
  tables but different headers/values; one uses 3 signal names absent from both current schematics, the
  other uses 14 names, all present. The workbook was last modified ~5 months before the current schematic
  revision. Which tab is current cannot be determined from the files — a question for the engineer.
- **Supersession across files.** None today: each doc ID occurs once. The manifest groups by doc ID, marks
  older revisions `superseded_by`, and diffs sha256 per file/page on request; a re-run with no changes
  reported 14/14 unchanged.
- **Cost of reading documents whole.** The vendor's PDF support costs 1,500-3,000 text tokens per page
  plus the image; the RM's 1,384 pages would be ~2-4M text tokens, so the scope's tiering is not optional.
  For the 24-page PRD the whole text is 33 KB (~8K tokens) while a candidate-extraction JSON is *larger*
  (36 KB) — the deterministic pre-pass buys structure, not compression.

## 3. Schematic PDFs: BOM and netlist without an LLM

Altium's PDF export has a "Generate nets information" option. It adds bookmarks for components, nets,
pins, net labels and ports. The docs do not say that this is machine-readable. Measured here, it is,
through two channels:

- **PDF outline** (a Python PDF library): per sheet, `Components -> U7 -> U7-1...U7-n` and
  `Nets -> <net> -> Pins [...] / NetLabels / Ports`; at project level, the inter-sheet net list; auto-named
  and power nets included (ground has 142 pins on board A). Check: every charger-IC pin lands on the net
  its datasheet pin function implies.
- **Invisible text tokens:** component/pin/net-label/port anchor tokens positioned on their object, with
  non-alphanumerics as digit placeholders. A first parser tried to rebuild the netlist from their stream
  order and silently merged unlabelled nets into the next labelled one (a 102-pin net that looked like an
  I2C clock line was really ground plus the clock line) — why the outline is the primary channel; the
  tokens still place each designator on the page.

**Comment (MPN) assignment.** The printed Comment is the MPN-like word below a designator's anchor token
that overlaps it horizontally, else the nearest within 45 pt, else the nearest unused within 200 pt
(flagged `nearest`): 113 assignments, 106 geometric + 7 fallback. **Known error, fixed:** on both boards a
1-pin test-point pad took a logic-gate IC's comment; a rule now rejects IC part numbers on 1-pin parts,
after which every IC/transistor designator has an MPN. **Checks against independent statements: 11/11
agree** (a logic-table spreadsheet names 8 load-switch designators; DT comments name 6 of those parts'
fault/status pins; the wiring notes name the board-B MCU designator). **Not verified:** the other ~100
assignments have no independent source; passives print values, not MPNs. **Assumption:** same-named nets
on different sheets are treated as one, correct for Altium's global/automatic net scopes (the project's
scope setting is not in the PDF). **Scope:** only Altium exports with net information were tested; other
EDA tools' PDFs are **unverified**, falling back to geometry like a hand-written pin-extraction script.

**Revision diff.** A netlist-diff script reports components added/removed/re-MPN'd and nets whose pin
sets changed, deriving stale designators; its self-test (move a pin, change an MPN, delete a test point)
catches all three. It works like Altium 365's own "Components and Nets" comparison, rebuilt from PDFs.

## 4. The missing-documents checklist

A missing-datasheets script joins the BOMs to the manifest. It ranks each MPN by how directly MCU
firmware sees it:

- **MCU-direct:** shares a non-rail net with the MCU.
- **MCU-via-series:** reached through one 2-pin resistor/ferrite-bead/inductor.
- **cross-board:** an A-side net that crosses the board-to-board connector to an MCU net. For now this
  joins by net name, which §8 measures as unsafe (5 false merges in 38 shared names); the next version
  should use the confirmed connector map.
- **indirect:** everything else.

| # | Part class | Qty | Firmware tier |
|---:|---|---:|---|
| 1 | small P-channel MOSFET (power-enable/shutdown nets) | 13 | MCU-direct |
| 2 | small single-gate demux IC (direction-select, two ports) | 8 | MCU-direct |
| 3 | small N-channel MOSFET (inter-board serial RX/TX enable) | 4 | MCU-direct |
| 4 | small dual-buffer IC (inter-board serial buffering) | 2 | MCU-direct |
| 5 | LED-driver IC (display backlight PWM) | 1 | MCU-direct |
| 6 | USB common-mode filter (USB D+/D-) | 1 | MCU-direct |
| 7 | connectors (USB-C, board-to-board, button) | 5 | MCU-direct |
| 8 | small voltage-supervisor IC (one resistor's other end) | 1 | MCU-via-series |
| 9 | logic gates (AND, inverter), one load-switch family, ~16 more | — | indirect |

Covered: MCU (datasheet+RM), current-monitor IC x7, both load-switch families, the buck-boost, charger
and fuel-gauge ICs. Also missing (not an MPN): the errata sheet (document number known, fetch timed out).
One module named in the PRD has no datasheet; two other modules already have hand-written spec
distillations in the firmware repo. **(inference)** The "indirect" logic gates and demux ICs implement the
logic-table spreadsheet's truth tables — the load-switch modes' meaning depends on them even though
firmware never touches their pins directly; the tier ranks visibility, not importance.

## 5. Procedures as coding-assistant skills

What the platform gives: a skill-definition file (name <=64 chars, description <=1,024, body under 500
lines); scripts that cost only their output, since they execute rather than load into context; "low
freedom" wording for fragile steps; checklists copied into the reply and "validate -> fix -> repeat"
loops; a "plan-validate-execute" pattern with verifiable intermediate files (used here for the PRD pass);
frontmatter controls (disable self-invocation, allowed-tools, forked-subagent mode, dynamic context
injection); and subagents in their own context window with a concurrency limit, so a second pass by a
fresh subagent is cheap to set up (**not measured here**: no agent-spawning tool this session).

The draft skill is an eight-step checklist: deterministic steps are exact commands; LLM steps are
bounded (one document per pass, every fact carries doc+page+quote, a second-pass subagent samples the
output); the session stops and asks at two gates (unknown/missing documents, board labels).

**Resumability.** A step-state file records an input fingerprint per step per document (input sha256 +
script version, or skill version + model); a step is done iff its outputs exist and the fingerprint
matches — the same re-run logic a well-known data-versioning tool uses for its lock-file hashes.

**Reproducibility of LLM steps.** Re-running an LLM does not reproduce it: 5 LLMs configured to be
deterministic showed accuracy variation up to 15% across runs in a published study (2408.04667). So LLM
outputs are **frozen**, keyed by (page-text hash, skill version, model); a re-run re-verifies rather than
regenerates, and only changed-hash pages go back to the model, diffed against the frozen facts. The script
side needs the same discipline: the BOM extractor was nondeterministic until a set was sorted (Python's
per-process hash seed flipped one assignment); three runs are now byte-identical.

## 6. Provenance and trust model

| Class | Meaning | Example in the spike | Gate to use it |
|---|---|---|---|
| `deterministic` | Script output, reproducible | netlist from the outline | inputs fingerprint current |
| `deterministic+crosschecked` | Two independent channels agree | MCU nets 88/88 (outline vs hand-written script) | both current |
| `llm-verified` | Quote found on the cited page, plus a second pass or script check agrees | 24 PRD statements (quote check only) | frozen output, fingerprint current |
| `llm-unverified` | Quote failed, or the passes disagree | the paraphrase control | review queue |
| `engineer-declared` | DT/code comments, notes, answers | 31 DT net comments | staleness check vs build |
| `engineer-confirmed` | An engineer accepted a proposed fact or link | (none yet) | recorded decision |
| `measured` | Bench or trace | (later layer) | — |

A well-known provenance ontology (W3C PROV-O) maps cleanly: a fact is an Entity; an extraction run is an
Activity that used the document entity, was associated with the script or coding-assistant session, and
had a plan = the skill's version; document revisions are `wasRevisionOf`, verbatim quotes are
`wasQuotedFrom`, staleness is `invalidatedAtTime` — borrow the vocabulary without implying a dedicated RDF
store **(inference)**. Conflicts are kept, not overwritten, the way a large public knowledge base keeps
preferred/normal/deprecated ranks so a wrong-but-sourced value stays visible (example: one connector pin
has two different net names between the two boards' schematics for the same signal). **Schema:** seven
sample facts illustrate the fields `id, kind, value, class, source{doc, sha256, page, quote|locator},
activity{tool|skill, model}, crosscheck, inputs_fp, status{current|stale|superseded|proposed}`. **Review
queue:** `llm-unverified` facts and every LLM-proposed link go to a review queue file; the engineer's
accept/reject becomes `engineer-confirmed` or `deprecated`, with the decision recorded.

## 7. Incrementality

Per-page text hashes (1,968 pages, 16 hex chars, in 12 sidecar files) mean a datasheet revision re-distils
only changed sections. Schematic revisions go through the netlist-diff script, yielding stale designators
that flag the facts keyed on them (pin/net map rows, DT joins, missing-docs rows). Build changes (git sha,
EDT pickle, `.config`) invalidate only the join layer, since document distillations are board- and
build-independent by the owner's decision. Frozen LLM outputs make re-runs cheap: an unchanged page costs
0 model tokens.

## 8. Multi-board modelling

Measured on the two schematics:

- **Designators are board-local.** 218 of board A's 290 designators also exist on board B; 19 differ in
  printed MPN (17 print one on both boards) [corrected in verification] — board A's MCU-designator
  position is actually the fuel-gauge IC, board B's is the MCU itself. Every ID must be board-qualified
  (`A:U3`).
- **Board-to-board interface.** The two main connectors are the same part on both boards; under "pin n
  mates pin 33-n", 30/32 pins carry the same net name (identity map: 0/32); a secondary 1x8 header pair
  matches 8/8 under "pin n mates pin 9-n". Mating orientation is in no document — the script *proposes*
  the map and the engineer *confirms* it.
- **Name equality is not connectivity.** 38 net names occur on both boards, 33 crossing a board-to-board
  connector; the other 5 (two ground-related, two power-good, two auto-named test-point nets) do not. A
  name join would have made 5 false merges and missed 1 true join. Joins must go through declared
  connector pin maps, with names as the cross-check.
- **Unit-to-unit interface.** Each board has 5 single-pin spring-contact pads for a serial link, ground
  and two USB-like busses, doubled per board side; contact part numbers differ per side, consistent with
  opposite genders of one mating pair, so one board's "side A" contacts mate the next unit's "side B" — a
  PRD architecture diagram is consistent with that.
- **Variants.** The PRD asks for one CAD project whose variants DNP one module's parts. The board
  includes one small devicetree include file per variant (two are empty stubs) **(inference: these map to
  the PRD's module types)**. DNP state is not in the PDF (the outline lists all components); it needs the BOM
  variant or an engineer answer.
- **Models to borrow:** Altium multi-board projects (child boards as modules, connectors as
  connector-role components); Zephyr connector nexus nodes as the DT-side analogue.
- **Proposed store shape (inference):** `product -> unit -> module_type -> board_instance -> component ->
  pin -> net`, plus `interface{kind, pin_map, class}`.

## 9. PRD ingestion and requirements traceability

| Tool | What to take |
|---|---|
| ReqIF 1.2 (OMG standard) | the exchange format, if a client ever ships requirements from a formal requirements tool |
| A YAML-per-requirement traceability tool (open-source) | one YAML file per item, SHA-256 fingerprint of UID+text+refs+link-UIDs; a link stores the parent's fingerprint so a changed parent makes it **suspect** |
| A coverage-tracing tool (open-source) | IDs `type~name~revision`; a coverage link to a lower/higher revision is **outdated**/**predated** |
| A requirements-authoring tool with ReqIF support (open-source) | UID + a machine ID that survives renames; ReqIF import/export; experimental C-comment requirement parsing |
| A Sphinx-based requirements extension (open-source) | JSON export keeping old versions side by side |

**LLM trace-link literature.**

- **A retrieval-augmented traceability paper (ICSE), requirement-to-code, a frontier model, top-20
  retrieval, no preprocessing, best prompt:** F1 ranges .294/.526/.276 across three small student-project
  datasets and .273/.241 on two more; beats two prior non-LLM baselines on average; evaluation cost ~$850.
  Its documentation-to-code task on two large real-world projects got top-40 retrieval recall 0.027 and
  0.061 [corrected in verification].
- **An automotive requirements-traceability paper** (requirement-to-requirement, a mid-size model + RAG):
  *validating* existing links 98.87% accuracy; *recovering* missing links 85.5% correct.
- **Lesson:** checking a proposed link is far easier than finding one. The atlas lets the LLM propose
  PRD-to-DT/code/test links, checks by script that each anchor exists, and leaves "does it implement the
  requirement" to the engineer.

**Spike on the project's PRD:**

- **Candidates:** 30 TOC sections, 181 candidates, extracted deterministically.
- **LLM curation:** 24 statements (17 firmware-relevant, 4 partial, 3 not), each with a stable ID, page
  and verbatim quote. **Quote check:** 24/24 pass.
- **Anchors:** 54/56 proposed anchors exist. The 2 misses are real gaps: a bootloader Kconfig option is
  unset in all 5 measured builds although the devicetree reserves a bootloader partition, so a "firmware
  update via one predefined USB port" requirement has no realized path here (inference); and the display
  module has no datasheet.
- **Stable IDs:** fingerprint match first, then fuzzy match within the section at ratio >= 0.80 — keeps an
  edited requirement's ID, bumps its revision, marks links suspect. A wattage-spec change landed at ratio
  0.97: at that similarity a semantic change looks like an edit, so every revision bump must reach review.
- **Consistency findings (inference, not yet verified):** the PRD's operating temperature range is
  narrower than two component documents' stated ranges (no stated firmware enforcement); the
  capacity requirement ("at least" X) is undercut by the selection matrix and the component docs listing a
  smaller value; one module's name is spelled differently in the PRD and in the docs folder.
- **Image-only architecture pages.** Three PRD page images are legible at native size (block diagrams of
  the product). One of them shows two instances of a port type that the board files define only once in
  DT, while the schematic has two — a gap candidate **(inference)**.

## 10. The end-to-end procedure (draft)

Engineer drops files -> inventory -> diff against the last manifest -> schematic extraction -> board
model (script proposes, engineer confirms) -> missing-documents checklist (engineer supplies files, back
to inventory) -> LLM distillation (one document per run) -> script verification -> second pass by a fresh
subagent -> joins -> engineer review queue. Each step has one output and one gate; the session stops for
the engineer at the diff, board-model and missing-documents steps.

## What this means for atlas

All of these are inferences.

- **Schematic intake belongs to the deterministic core** when the export carries net information: no LLM
  needed for BOM, netlist, the board-to-board map proposal or the missing-documents list. Ask the EE to
  keep "Generate nets information" on in every release PDF — free, and turns a vision problem into a parse.
- **The first user-visible product of atlas is a question list, not a document:** missing datasheets
  (ranked), the errata, which review-spreadsheet tab is current, the board labels and mating orientation —
  the owner's "providing all the datasheets" step, done as a process.
- **"Verified" should mean mechanical checks the atlas can re-run** (quote on page, numbers in quote,
  anchor exists, second channel agrees), keeping semantic correctness at `engineer-confirmed` — the trust
  bar stays honest and cheap.
- **Freeze LLM outputs by content hash**, scheduling a second pass only for changed pages and high-stakes
  classes (limits, quirks, register semantics); re-running the same prompt does not buy reproducibility.
- **The fact store needs board-qualified IDs, declared interfaces and a provenance class per fact from day
  one** — the measured collision rates (218/290 designators, 5/38 coincident net names) would corrupt a
  flat store immediately.

## Open questions for the owner

1. **Board-pairing:** is the mating rule "pin n mates pin 33-n" (30/32) right, and is the net that
   dropped its active-low prefix a rename to fix or two signals?
2. **The review spreadsheet:** which tab is current — one uses 3 net names absent from the current
   schematic revision.
3. **Missing documents:** will you supply the 10 firmware-visible datasheets and the MCU errata sheet, or
   mark some "not needed"?
4. **PRD scope:** when the PRD and a component document disagree on a limit, which one does firmware enforce?
5. **Variants:** which parts are DNP on each module variant, and do the per-variant DT include files map
   one-to-one to the module types?

## Sources

**External:** the Altium Smart PDF wizard, multi-board design and Altium 365 comparison docs; Anthropic's
skill best-practices, coding-assistant skills and subagents docs; vision and PDF-support docs; W3C PROV-O;
a large public knowledge base's ranking-help page; a YAML-based traceability tool's item-reference doc; a
coverage-tracing tool's spec; a requirements-authoring tool's user guide; a Sphinx-based requirements
extension's builder docs; the OMG ReqIF spec; a retrieval-augmented traceability paper and its replication
package; an automotive traceability paper; 2408.04667 (LLM determinism); a data-versioning tool's
project-structure docs. **Not opened:** the MCU errata PDF (timed out; its number comes from a search
result only).

**Local:** the project's hardware-document folder; board devicetree/overlay files; the archived build's
`.config`; the Zephyr shields-porting doc; earlier internal notes on hardware/software models and
flow-tracing plus a repo-comprehension-evidence note; the spike's own output files.

**Could not verify:** MPN assignments beyond the 11 independent checks; Altium net-identifier scope;
connector mating orientation; per-pin (not per-net) agreement of the hand-written pin script; other EDA
tools' PDFs; second-pass subagent agreement rates; whether the traceability-paper accuracy figures
transfer to PRD-to-firmware links.

See also: [13-mcu-reference.md](13-mcu-reference.md), [15-cross-source-join.md](15-cross-source-join.md), [16-landscape-update.md](16-landscape-update.md).

## Verification (2026-09-29, independent)

| Claim | Verdict | Evidence |
|---|---|---|
| 725/725, 1,291/1,291 pins on nets; 88, 31, 30/32, 8/8, 38/33 | confirmed | own outline walk; re-run identical |
| 1.7 s, 5.4 s | unverifiable | load-bound: runs ranged 1.4-4.3 s and 13-20 s |
| MPN coverage; 106+7; 11/11; 19 MPNs differ between boards | corrected | recount cross-checked against DT/notes; 17 differ on both boards, not 19 |
| 10 of 26 firmware-visible missing MPNs | corrected | one more reachable via a series resistor |
| doc no./rev; 9 mark-of-the-web sidecars; 1,968 pages | confirmed | pdftotext re-run; 1,966 pp + 2 sheet-count adjustments |
| PRD 24/24, 54/56, ID fingerprinting, TOC 15/30 stale; image pages ~980 tok; 1.5-3K tok/page general PDF cost | confirmed | re-run; own quote check; image extraction re-run; vendor docs |
| Cited traceability F1s, evaluation cost, cross-project retrieval | corrected | one number came from the documentation-to-code task, not requirement-to-code |
| LLM determinism variance (~15%); automotive paper numbers; skill/subagent limits; errata identity | confirmed | abstracts and docs checked; errata title only, contents unfetched |
