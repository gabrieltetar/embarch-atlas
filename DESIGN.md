# embarch-atlas: design

**Status:** draft, 2026-09-30. This file lives in the repo until the sub-project's
docs move to `embarch-doc/embarch-atlas/`, the suite's home for design docs. The
research behind it is in [research/](research/README.md).

## What it is

A pipeline that lets a coding agent understand a firmware project's **hardware
documents** (schematic PDFs, component datasheets, the MCU reference manual, the
PRD), joined to the **firmware** (a Zephyr build), without reading any of them
whole. Scripts do every deterministic step. The LLM steps run in Claude Code
sessions, driven by skills. Every fact carries a citation and a provenance class.

It is fully local. **The tool never fetches a document**: the engineer supplies
every file, and a missing one becomes a question.

## Principles

1. **Deterministic first.** Text layers, PDF outlines, embedded schematic
   netlists, SVD files and build artifacts are read by scripts. The LLM gets only
   what they cannot give: prose, tables that text extraction mangles, figures.
2. **The text layer is the numeric oracle.** Every number an LLM writes must be
   found on the page it cites, or be tagged `(derived)`, `(figure)` or `(inference)`.
3. **Records are the source of truth; markdown is rendered from them.** The LLM
   writes records (JSON Lines), prose and the card. Tables in the markdown tiers
   are rendered from records, so they cannot drift.
4. **Nothing is deleted for failing a check.** A failing fact is kept and marked
   `llm-unverified`. A disagreement between sources is kept as a `conflict`.
5. **Board-independent vs board-specific.** A datasheet distillation knows nothing
   about any board. Wiring (nets, which pins are used, straps) lives only in the
   schematic analysis. The MPN is the join key between the two.
6. **Every atlas is pinned to one target + one firmware commit** and built from a
   git worktree at that commit. The engineer's working tree and build dirs are
   never touched.
7. **LLM outputs are frozen by content hash.** A re-run re-verifies; it does not
   re-ask the model. Only changed pages go back to the LLM.

## The skills

| Skill | Input | Output |
|---|---|---|
| `atlas-translate-docs` | the documents the engineer dropped | `docs/` (board-independent distillations), `boards/` (schematic analysis), `inventory/` (manifest, questions) |
| `atlas-build` | a target + a firmware commit | `atlases/<target>@<commit12>/`: build artifacts, joins, pin/net map, mismatch report, MAP.md; the second look on facts it uses |
| `atlas-update` | a new commit or changed documents | a new atlas that reuses every unchanged translation and verdict; a diff against the previous atlas |
| `atlas-use` | a hardware question in a repo with an atlas | the protocol MAP → card → section → page, cite everything |

The first three are user-invoked (`disable-model-invocation: true`) and call the
`embarch-atlas` CLI for every deterministic step; `atlas-use` is the one skill an
agent loads by itself. `install.py` copies them to `~/.claude/skills/`.

## On-disk layout (under `<fw>/embarch/atlas/`, git-excluded)

```
atlas.toml                      project config (below)
inventory/
  manifest.json, manifest.md    every source file: kind, doc_id, parts, doc no., rev, date, pages,
                                sha256, per-page hashes, text-layer coverage, net_info; diff vs previous
  missing.md                    questions for the engineer: missing datasheets, errata, declarations
  answers.toml                  the engineer's answers to missing.md (not built yet)
docs/<doc_id>/                  one per datasheet / RM / PRD / SVD, board-independent
  doc.json                      identity (file, sha256, kind, part, doc_no, rev, pages)
  toc.md, toc.json              outline: section id | title | pages | tier-2 file | tokens
  pages/p<NNN>.txt              text layer per page (pdftotext -layout), 1-based physical page
  pages/pages.json              page classes (image-heavy, figure, equation, table-heavy, register, pua-glyphs,
                                overline) and hashes; running headers/footers/logos ignored
  pages/p<NNN>.bbox.json        word boxes, when a step needs positions
  records/<kind>.jsonl          LLM records, one file per kind (schemas/record.schema.json)
  records/<producer>.jsonl      deterministic records (e.g. regparse.jsonl)
  card.md                       tier 1, <= 1.5K tokens
  s/<secid>.md                  tier 2, <= 4K tokens per file, page-cited
  s/invariants.md               the `check` records (expressions, not numeric claims)
  facts.txt                     one fact per line (rendered from records)
  checks.json                   last check report
docs/<rm_doc>/                  the reference manual adds: outline.json, worklist.json,
                                availability.json, rm_regs.json, rm_checks.json, vote.md/.json,
                                s/<ch>.card.md, s/<ch>.must.md, s/<ch>.fields.md
docs/<svd_doc>/                 the SVD adds: peripherals.json, s/<PERIPH>.md
boards/<label>/                 schematic analysis, one per board PDF
  netlist.json, bom.json        from the embedded Smart-PDF netlist (below)
  nets.md, parts.md             compact line formats for the LLM
  geometry.json                 the geometric cross-check of the netlist (rung 3), per sheet and pin
boards/system.json              board instances + interfaces (engineer-declared, with machine scores);
                                power_feeds (engineer-declared: net, from, via, only), shown in MAP.md
boards/straps.jsonl             optional: address/mode straps when no datasheet record exists yet
boards/bindings.json            optional: compatible -> MPN, prop -> pin, extra rails, prop_limits
                                (DT property -> limit record ids, with `times`/`per` for scaled props),
                                dt_intent (node -> why it is okay without a driver: folds dt-no-driver)
cache/llm/<key>.json            frozen LLM outputs; second-look verdicts as sl-<key>.json
.work/<commit12>/<fw basename>  the build worktree
atlases/<target>@<commit12>/
  atlas.json                    the stamp (below)
  build/                        edt.pickle, .config, compile_commands.json, zephyr.elf, zephyr.dts,
                                zephyr.dts.d, zephyr_modules.txt, build_info.yml
  inputs/, inputs.json          netlist and BOM snapshots the atlas was joined against (for diff)
  dt.json                       pins, devices, buses, IRQ/EXTI from the edt
  in_use.json                   peripherals: dt-okay / instantiated / touched-by-code
  pins.md, pins.json            pin/net map
  pin_slice.txt                 always-loadable slice (~1.5K tokens)
  checks.md, mismatches.json    mismatch report: checks run and passed, then candidates by class
  codecheck.json, codecheck.md  driver register constants vs datasheet records
  limits.json                   DT limit properties (SI) vs the part's datasheet limits
  second_look_queue.jsonl       facts this atlas uses that still need a second look
  second_look.jsonl             second-look verdicts
  graph.json                    the whole atlas as one graph, for the embarch-ui Atlas tab (below)
  MAP.md                        tier 0, <= 5K tokens, byte-stable
current.json                    which atlas matches the checkout (target, commit)
MAP.md                          copy of the current atlas's MAP.md: the one stable path the session imports
```

### The stamp (`atlas.json`)

`target`, `board`, `app`, `schematic_boards`, `mcu`, `commit` (full sha), `dirty:
false` (a worktree is always clean), `west` (every project's path, revision and
`head`), `sources` (sha256 of `inventory/manifest.json`), `schematics` (sha256 per
board PDF), `build{...}` with the from-worktree evidence, `tool` (embarch-atlas
version + git sha), `created` (UTC). An atlas is **stale** when HEAD or any source
hash differs; it is never edited, only superseded.

### Building from the worktree

The worktree is `.work/<commit12>/<fw basename>`: Zephyr names a module without a
`name:` after its directory, so the basename must match the fw repo's. The fw repo
is itself a Zephyr module and west lists the *main* checkout, so `build` passes
`-DZEPHYR_MODULES=<every other west module>;<worktree>` and then proves the result:
no TU, include path or DTS input from the main checkout (`atlas.json`
`build.from_worktree`). A leak fails the build.

## Identifiers

- `doc_id` = part or document slug + `@` + revision: `lsw100@04b`, `mcu-rm@6`,
  `bb200@B` (invented examples). Slugs are lower-case, no spaces. No revision gives
  `@norev`; a non-PDF source (an SVD) may omit `@rev`.
- Pages are **1-based physical PDF page indices** (what `Read(pages=N)` takes).
  The printed label is stored only where it differs.
- Section ids are the document's own numbering (`32.9.5`, `7.6.6`); a title slug
  when it has none. Table ids are the caption's number (`T7-10`).
- Fact ids use native names, never ordinals: `<part>.<kind>.<symbol>[.<cond-slug>]`,
  e.g. `lsw100.ec.VOVP.vbus_rising`, `bb200.reg.MODE.OE`.
- Schematic keys are **board-qualified**: `A:U3`, `A:U3-12`, `B:J2-5`, net `B:3V3`
  (invented examples).
- A citation is short and re-openable: `lsw100@04b p8 fn26` in datasheet tiers,
  `[§32.9.5 p.976]` in reference-manual tiers.

## Records (JSON Lines, `schemas/record.schema.json`)

Common fields on every record: `id`, `kind`, `doc`, `p` (int or [int]; null for
`src` `svd`/`header`), `q` (verbatim quote from the text layer, or a list of them;
null when `src` is `figure`), `src` (`text` | `table` | `figure` | `equation` |
`svd` | `header`), `prov` (below), `note`. A text record with no `q` fails
anchoring and stays `llm-unverified`. Extra fields are allowed.

Kinds and their own fields:

| kind | fields |
|---|---|
| `limit` | `group` (abs, esd, thermal, recommended, ec, i2c, register, application, other), `sym`, `param`, `cond`, `min`, `nom`, `typ`, `max`, `unit`, `fn` (footnote ids), `guar` (production-tested / design / characterization). `±X` is `min: -X, max: X` |
| `timing` | as `limit`, plus `edge` |
| `pin` | `pin` (number/ball, or [number, ball]), `name`, `dir`, `type` (od, pp, analog, supply, ...), `io` (an MCU pin's I/O structure as printed: FT, FT_fa, TT_a...), `active`, `pull`, `desc` |
| `rule` | `text`, `cond`, `recover`, `steps` (behaviour rule, "OK goes low on fault, latches until EN toggles") |
| `register` | `name`, `addr` (int or `0x..`), `width`, `reset` (`0b…`/`0x…`), `access`, `summary` |
| `field` | `reg` (the register's record id), `name`, `bits` ([hi, lo]), `reset`, `access` (SystemRDL vocabulary: rw, ro, wo, rclr, woclr, w1s, rsvd), `enum` |
| `quirk` | `class` (table-vs-table, text-vs-table, text-vs-text, text-vs-figure, figure-only, footnote-condition, silent-default, must, do-not, absence, variant, doc-artefact, not-prod-tested, narrow-condition, provenance), `text`, `refs` (other fact ids) |
| `figure` | `fig`, `caption`, `what` (equations: `src: equation`) |
| `strap` | `pin`, `condition`, `value` (address or mode straps; a fixed address has condition `fixed`) |
| `passthrough` | `from_pin`, `to_pin`, `when` (transparent-part map: switches, shunts, buffers) |
| `check` | `expr`, `expect`, `why` (an invariant the checker evaluates) |
| `requirement` | `rid`, `text`, `fw_relevant`, `modality` (shall, should, may, nice-to-have), `priority`, `order` (PRD) |
| `conflict` | `refs`, `sources` (`"<doc> <loc>: <says>"`), `text` |

`prov` classes: `deterministic`, `deterministic+crosschecked`, `llm-verified`,
`llm-unverified`, `engineer-declared`, `engineer-confirmed`, `measured`.

A deterministic record and an LLM record may share an id as **twins**; `facts.txt`
keeps both lines and the `register` lookup merges them. `vote` writes its records
under the **RM** doc (each cites an RM page), with `refs` pointing at SVD ids and
the verdict leading `note` (`svd-defect: …`).

### `facts.txt` line format

One record per line, self-describing, grep-complete:

```
<id> <key>=<value>; <key>=<value>; ...; cite=<doc_id> p<N>[ fn<k>]; prov=<class>
```

### Markdown row rules (tier 2)

One condition per row; no merged cells; the symbol and unit repeated on every
row; footnotes pulled onto the rows they qualify (a `note` column) and listed
under the table; native names verbatim; an `aka:` line under each heading for
alternative names (for example "master/controller"). Rendered tables sit between
`<!-- atlas:table … -->` and `<!-- atlas:end -->`, so the prose around them survives
a re-render; `<!-- atlas:covers … -->` declares what a section file covers (exact file
stems first, then explicit covers, most specific id first, then implicit descent from a
file named after a parent section). Token budgets are counted in o200k (real Claude
counts run ~1.4×). Pages no outline entry covers become a synthetic `front-matter`
(contents and lists) or `unlisted` entry, so they never inflate the section before them.
A page is flagged for an image Read when its text layer cannot carry the fact: little
text, a figure (captioned, or a content image over ~10% of the page, or a vector graphic
that is not a text-covered grid like a bit diagram), a math font, or an overlined name
that also appears without the bar.

The prose check (M) anchors every number in `card.md` and `s/*.md` to a cited page. A
tag (`(derived)`, `(figure)`, `(equation)`, `(inference)`) covers the numbers before it in
its clause and those inside its own parentheses; a number equal to a verified record's
value anchors only when that record is on a page the line cites. Rendered structure
(bits, widths, enum keys, check expressions) is not a numeric claim. A non-PDF source
(an SVD) gets only the record-level checks.

## How a session sees the atlas

- **The map loads every session** (owner decision). `<fw>/CLAUDE.local.md` holds
  `@embarch/atlas/MAP.md`. Claude Code auto-loads `CLAUDE.local.md` from the repo
  root and resolves the import relative to it
  (https://code.claude.com/docs/en/memory.md). The MAP is byte-stable, so it stays
  in the cached prefix.
- When an atlas becomes current (its commit equals HEAD), `embarch/atlas/MAP.md` is
  replaced by that atlas's MAP. When HEAD moves past it (or a source changes), `status`,
  which every atlas skill runs as it loads, puts a `> **Stale:**` line first in that MAP with
  the reasons and `/atlas-update`; the line goes once the MAP's atlas is fresh again.
- **The MCP server** is registered in a project `.mcp.json` with `alwaysLoad: true`
  (https://code.claude.com/docs/en/mcp.md), so its 7 tools skip deferred tool search.
  Claude Code asks the engineer to approve a project server on first use.
- `init` writes both files and adds them to the fw repo's `.git/info/exclude`, so no
  tracked file changes.

## Query surface

`query.py` holds the seven lookups as plain functions returning markdown: `part_card`,
`doc_toc` (`under` takes an id, a title, or a word of a title, which resolves to the one
matching section that holds all the others, else lists the matches), `doc_section`,
`find_fact` (BM25 over every `facts.txt`; a query term naming one part narrows to it like
`part`; with no `limit`, the best 10 hits scoring at least 0.4 of the top one, and the
header names the query terms no fact contains), `register` (MCU `PERIPH.REG` or
`<part>.<REG>`, twins merged, rules/quirks/conflicts and firmware references joined; a
register tabled as `<REG>_MSB` + `<REG>_LSB` bytes answers to `<REG>` as one 16-bit view),
`pin_trace`, `mismatch_report`. Each answer starts with a `src:`
line and is paged at a per-tool token budget with `next=<offset>`. Two doors call the
same functions: `embarch-atlas q <verb>` and `embarch-atlas mcp` (stdio, `mcp>=2.2`,
optional extra). Files stay the primary door: an agent's own `Read`/`Grep` reach
them for free; the server carries what files cannot.

## The whole-atlas graph (`graph.json`)

The query surface answers one question at a time; a map needs everything at once.
`graph.py` writes one file per atlas, read by embarch-ui's Atlas tab and by nothing
that decides anything. It holds no new evidence: every status and citation comes
from a file `join` or `secondlook` wrote.

- **Layers** (served as vocabulary): external, app, middleware, drivers, SoC
  peripherals + MCU pins, components + signals. The devicetree is not a layer; a
  DT node is the label on an edge.
- **Code model, from the build's ELF.** `nm -l` places each function in a file, and
  the file names its module: `apps/<app>/**/<file>` (app), `lib/<name>` (middleware),
  the driver's directory (drivers, named after the compatible it implements), the
  in-tree Zephyr driver file or subsystem (shown only when firmware reaches it or the
  DT instantiates it). `objdump -d` gives direct calls, by address, so two static
  functions sharing a name stay apart; a literal-pool word that points at
  `__device_dts_ord_<N>` is a device use, and one that points at a function is a
  callback registration (a dependency). Kernel, arch and HAL code are not modules.
- **Edges:** `call`, `device` (labelled with the DT node), `bind` (driver to part,
  from the I2C identity join, else from a node whose gpio pins all reach one part),
  `bus`, `periph`, `gpio` (the property and its flags), `pinctrl` (the function),
  `net` (MCU pin to far-end part, with the net), `pad` (MCU pin to its package),
  `mate` (board-to-board), `ext` (external system to its connector).
- **Nets** per board, with `rail`/`ground`, `mates` across a declared interface,
  and `fw` (the MCU port and the DT uses) wherever the MCU drives the net directly,
  through a series part or across a connector.
- **Symbols.** Every part gets a generic shape by refdes prefix and BOM text
  (resistor, capacitor, inductor, ferrite, diode, LED, TVS, fuse, crystal, switch,
  net tie, test point, transistor, IC, connector) and pin offsets: two-pin parts
  upright with ground down and the anode up; a box puts supplies on top, grounds at
  the bottom, an MCU's ports split by letter, otherwise datasheet direction (`out`
  right) or pin order. Same-name same-net pins merge into one stub. A three-pin
  transistor without pin names is a numbered box, never a guessed G/D/S. The part's
  label position and drawn extent are computed here, so the packer and the browser
  agree.
- **Clusters** are feature areas: deterministic Louvain over modules, MCU pins,
  peripherals and anchor parts (ICs, connectors, transistors), weighted by edge kind,
  with signal nets shared by at most six anchors linking parts. Hubs (the MCU,
  mated connectors, in-tree Zephyr modules, peripherals) go to their strongest
  neighbour's cluster; the MCU and mated connectors get the first column. Small parts
  are satellites of one anchor: the one they share a signal net with, else the nearest
  on the same schematic page (`geometry.json`). A cluster is labelled after its
  drivers, else its modules, else its ICs' part numbers: a fact about the graph, not a
  name for a feature.
- **Layout**, keyed by native IDs, so a part keeps its place across rebuilds: one
  column per cluster on every layer; on the components layer one band per board, each
  anchor with its satellites under it, shelf-packed, the plane sized about 2:1.
- **Problems:** `check`-severity candidates (`M<n>`) and, per document, facts whose
  second look ended `unresolved` or `conflict` (`U<n>`), each with the entities it
  marks and one row per evidence line: `{src, says, cite}`. A cite is
  `{t: "code", root, path, line}` or `{t: "doc", doc, p, sec, file, card, box}`; a
  schematic page is the doc `sch:<board>`, its box the part's pin extent in PDF
  points. `info` candidates are listed by class, never as problems.
- **Docs:** every translated document and board schematic with its PDF path, so a
  reader can render a cited page without parsing anything.

## Schematic ingest ladder

First match wins; each rung cross-checks the one above.

1. Native EDA project, when supplied (future adapter).
2. **Embedded PDF netlist**: Altium Smart PDF bookmarks (`Sheet > Nets > NET >
   Pins > REF-PIN`) and component-parameter JavaScript popups (MPN, value, DNI).
   Invisible anchor words (`PI<ref>0<pin>`, `CO<ref>`, `NL<net>`, `PO<net>`, `_`
   written as `0`) give positions. A PDF without net bookmarks makes `schematic`
   exit with a request to re-export with "net information" and "component
   parameters" on.
3. **Geometric tracing on the vector PDF** (`geom.py`: union-find over wires, T-junctions
   connect, crossings never do unless dotted; labels, port edges, power-port groups; the
   house style is a detected parameter set). Today it runs as the cross-check of rung 2:
   each net and pin is marked `crosscheck: agree | disagree | geometry-missing`, `method`
   gains `+geometry` when nets agree and none disagree, and disagreements go to `issues`.
   It is not yet a fallback for a PDF without an embedded netlist: pin identity still comes
   from the anchor words.
4. Vision on crops (Claude Code `Read` of a rendered crop), facts `llm-unverified`
   until matched.

Multi-part symbols (`U7A`/`U7B`) fold into one physical part. Interfaces between
boards are **proposed** with a score (net-name agreement k of n under each
candidate mating rule) and **confirmed** by the engineer.

### `boards/<label>/netlist.json`

```json
{"board": "B", "source": {"file": "...", "sha256": "..."}, "method": "smartpdf-outline",
 "sheets": {"<sheet>": {"page": 3, "parent": null}},
 "components": {"U3": {"mpn": "...", "value": "...", "dni": false, "dni_evidence": [], "parts": ["U3"],
                       "sheets": ["..."], "pins": {"1": {"name": "PA0", "net": "NET_A"}}, "pages": [7]}},
 "nets": {"3V3": {"pins": ["U3-19", "R5-1"], "sheets": ["..."], "labels": 4, "ports": 2, "auto": false}},
 "counts": {"geometry": {}}, "issues": []}
```

`issues` holds oddities worth an engineer's look (e.g. a printed comment naming a
different part than the MPN parameter). `bom.json` rows are `{ref, mpn, value, dni,
description, datasheet_link, comment, manufacturer, footprint, in_bom, pins}`.
`boards/system.json` holds `interfaces` (engineer-declared, never overwritten),
`proposed` (machine, scored), `name_check`, `inputs`, and a `board-to-board-check`
scoring each declared interface.

## `atlas.toml`

```toml
[project]
fw = "/abs/path/to/fw-repo"          # the firmware git repo
west_topdir = "/abs/path/to/ws"      # the west workspace holding .west/
west = "/abs/path/to/west"           # optional: the west executable
sources = "embarch/hardware"         # where the engineer drops documents, relative to fw

[mcu]
part = "<mcu part>"                  # used to pick SVD/RM
svd = "/abs/path/to/part.svd"        # local file, never fetched
ref = "B:U3"                         # optional: the MCU's board-qualified refdes (default: found by MPN)
rm_core = []                         # optional: override the cross-cutting RM chapter list
cubemx_db = "/abs/path/to/db/mcu"    # optional, the engineer's local install

[[boards]]                           # one per schematic PDF
label = "B"
pdf = "<file in sources>"

[[targets]]                          # one per firmware build an atlas can be pinned to
name = "core"
board = "<zephyr board>"
app = "apps/<app>"
schematic_boards = ["A", "B"]        # which schematic boards make up this product
extra_args = []                      # extra `west build` arguments
```

An atlas id is `<target>@<commit12>`.

## CLI (`embarch-atlas <verb>`)

| Verb | Module | Does |
|---|---|---|
| `init <fw>` | `project` | writes `atlas.toml` with defaults; wires `CLAUDE.local.md` and `.mcp.json` |
| `status` | `project` | atlases, which is current, which are stale |
| `inventory` | `inventory` | manifest + per-page hashes + diff vs last |
| `schematic [<pdf>] --board <label>` | `smartpdf` | netlist.json, bom.json, nets.md, parts.md |
| `interfaces` | `interfaces` | propose board-to-board mating rules with scores |
| `missing` | `missing` | missing-documents questions from BOM vs manifest |
| `doc prep <pdf>` | `pdftext` | doc.json, pages/, toc, page classes |
| `doc check <doc_id>` | `check` | checks A–T, writes checks.json, sets `prov` |
| `doc render <doc_id>` | `render` | tier-2 tables and facts.txt from records |
| `doc regparse <doc_id> --template ti\|maxim` | `regparse` | deterministic register records |
| `doc columns <doc_id> <page>...` | `columns` | a table page as rows and cells in reading orientation (sideways tables: rules, else gaps); for reading, never a quote source |
| `doc freeze <doc_id> [--model M]` / `doc thaw-check <doc_id> [--from <old>] [--restore]` | `freeze` | record the LLM-written units in `cache/llm/`; after a new `doc prep`, list which frozen units are still valid and which go back to the LLM |
| `mcu svd <svd> --doc-id <id>` | `svd` | registers/fields as records under the SVD doc |
| `mcu vote --svd-doc <id> --rm-doc <id> [--header <h>] [--periph P]` | `vote` | SVD vs header vs RM: `conflict` / `quirk` (correct omission) / `rule` records under the RM doc |
| `mcu rm-outline <rm>` / `rm-sections <atlas>` / `rm-text <rm> <item>` / `rm-check <rm> [--chapter C]` / `rm-freeze <rm> [--item I \| --sections S \| --retire E]` | `rm` | outline; the worklist for peripherals in use; one work item's text with `[p.N]` markers; verify distilled RM files; freeze them in `cache/llm/` |
| `build <target> [<commit>]` | `build` | worktree, west build, artifacts, stamp |
| `join <atlas>` | `join` | dt.json, in_use.json, pins, checks, mismatches, MAP.md, graph.json |
| `graph <atlas>` | `graph` | graph.json again from what `join` wrote (also run by `join`) |
| `second-look queue <atlas> [--top N]` / `prompt <atlas> <batch>` / `compare <atlas> <readings> -o <verdicts>` / `record <atlas> <verdicts>` | `secondlook` | facts this atlas uses that need a second look, ranked and batched by page; the blind reader's prompt; the deterministic verdict; record and freeze verdicts |
| (in `join`) | `limits` | DT properties with a unit suffix, in SI, vs the part's limit/timing records: declared (`prop_limits`) or proposed by unit and name; abs max = error, recommended = warn, EC = info; a sense-resistor-scaled property is compared only when a record states the formula |
| `codecheck <atlas>` | `codecheck` | driver register constants (addresses, masks, resets, comment claims) vs the datasheet's register/field records, joined by (address, bit range); also run by `join` |
| `diff <atlas> <atlas>` | `diff` | what changed between two atlases |
| `q <lookup>` | `query` | the seven lookups (same answers as the MCP tools) |
| `mcp` | `mcp_server` | the seven lookups as an MCP server over stdio |

## What an LLM step may and may not do

- May: read page images and the text layer; write records, prose, the card, the
  section plan; propose invariants (`check` records); propose interface rules.
- May not: write a number that is not on the cited page (untagged), invent a
  citation, edit a deterministic record, delete a failing record, fetch anything.

## Caching

RM worklist units are keyed by section id + page-text hashes + procedure version;
the model goes into the cache entry, so regrouping work items never invalidates
frozen output. Second-look verdicts are frozen as `cache/llm/sl-<key>.json` and
reused by every later atlas whose fact has the same content hash. When several entries share a
unit key, the one whose recorded file hashes match the files on disk wins, then the newest; a
newer freeze of the same files marks the older entry `superseded` (kept, never deleted).

## Not yet built (deliberately)

A native EDA adapter, geometry as a fallback (without anchor
words) and the vision rung of the schematic ladder, `inventory/answers.toml`, PRD → code
trace links.
