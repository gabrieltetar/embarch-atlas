# embarch-atlas: design (first cut)

**Status:** draft, 2026-09-30. The first port (2026-09-30) added the conventions in the last section; they are binding until folded into the sections above. This file lives in the repo until the sub-project's
docs move to `embarch-doc/embarch-atlas/`, the suite's home for design docs.

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
6. **Every atlas is pinned to one board + one firmware commit** and built from a
   git worktree at that commit. The engineer's working tree and build dirs are
   never touched.
7. **LLM outputs are frozen by content hash.** A re-run re-verifies; it does not
   re-ask the model. Only changed pages go back to the LLM.

## The three skills

| Skill | Input | Output |
|---|---|---|
| `atlas-translate-docs` | the documents the engineer dropped | `docs/` (board-independent distillations), `boards/` (schematic analysis), `inventory/` (manifest, questions) |
| `atlas-build` | a board + a firmware commit | `atlases/<board>@<commit12>/`: build artifacts, joins, pin/net map, mismatch report, MAP.md; the second look on facts it uses |
| `atlas-update` | a new commit or changed documents | a new atlas that reuses every unchanged translation; a diff against the previous atlas |

All three are user-invoked (`disable-model-invocation: true`). They call the
`embarch-atlas` CLI for every deterministic step.

## On-disk layout (under `<fw>/embarch/atlas/`, git-excluded)

```
atlas.toml                      project config: sources dir, west topdir, boards, MCU
inventory/
  manifest.json, manifest.md    every source file: kind, part, doc no., rev, date, pages, sha256, per-page hashes
  missing.md                    questions for the engineer: missing datasheets, errata, declarations
docs/<doc_id>/                  one per datasheet / RM / PRD, board-independent
  doc.json                      identity (file, sha256, kind, part, doc_no, rev, pages)
  toc.md                        outline: section id | title | pages | tier-2 file | tokens
  pages/p<NNN>.txt              text layer per page (pdftotext -layout), 1-based physical page
  pages/p<NNN>.bbox.json        word boxes, when a step needs positions
  records/<kind>.jsonl          source of truth (schemas/record.schema.json)
  card.md                       tier 1, <= 1.5K tokens
  s/<secid>.md                  tier 2, <= 4K tokens per file, page-cited
  facts.txt                     one fact per line (rendered from records)
  checks.json                   last check report
boards/<label>/                 schematic analysis, one per board PDF
  netlist.json, bom.json        from the embedded Smart-PDF netlist (or geometry)
  nets.md, parts.md             compact line formats for the LLM
boards/system.json              board instances + interfaces (engineer-declared, with machine scores)
cache/llm/<key>.json            frozen LLM outputs, key = sha256(page-text hashes + skill version + model)
atlases/<board>@<commit12>/
  atlas.json                    the stamp (below)
  build/                        edt.pickle, .config, compile_commands.json, zephyr.elf, build_info.yml
  dt.json                       pins, devices, buses, IRQ/EXTI from the edt
  in_use.json                   peripherals: dt-okay / instantiated / touched-by-code
  pins.md, pins.json            pin/net map
  pin_slice.txt                 always-loadable slice (~1.5K tokens)
  checks.md, mismatches.json    mismatch report: checks run and passed, then candidates by class
  second_look.jsonl             second-look verdicts for facts this atlas uses
  MAP.md                        tier 0, <= 5K tokens, byte-stable
current.json                    which atlas matches the checkout (board, commit)
MAP.md                          copy of the current atlas's MAP.md: the one stable path the session imports
```

### The stamp (`atlas.json`)

`board`, `commit` (full sha), `dirty: false` (a worktree is always clean),
`west` (every project's path + revision from `west list`), `sources` (sha256 of
`inventory/manifest.json`), `schematics` (sha256 per board PDF), `tool`
(embarch-atlas version + git sha), `skill_versions`, `created` (UTC). An atlas is
**stale** when HEAD or any source hash differs; it is never edited, only superseded.

## Identifiers

- `doc_id` = part or document slug + `@` + revision: `lsw100@04b`, `mcu-rm@6`,
  `bb200@B` (invented examples). Slugs are lower-case, no spaces.
- Pages are **1-based physical PDF page indices** (what `Read(pages=N)` takes).
  The printed label is stored only where it differs.
- Section ids are the document's own numbering (`32.9.5`, `7.6.6`); a title slug
  when it has none. Table ids are the caption's number (`T7-10`).
- Fact ids use native names, never ordinals: `<part>.<kind>.<symbol>[.<cond-slug>]`,
  e.g. `lsw100.ec.VOVP.vbus_rising`, `bb200.reg.MODE.OE`.
- Schematic keys are **board-qualified**: `A:U3`, `A:U3-12`, `B:J2-5`, net `B:3V3` (invented examples).
- A citation is short and re-openable: `lsw100@04b p8 fn26`.

## Records (JSON Lines, `schemas/record.schema.json`)

Common fields on every record: `id`, `kind`, `doc`, `p` (int or [int]), `q`
(verbatim quote from the text layer, or null when `src` is `figure`), `src`
(`text` | `table` | `figure` | `equation`), `prov` (below), `note`.

Kinds and their own fields:

| kind | fields |
|---|---|
| `limit` | `group` (abs, esd, thermal, recommended, ec), `sym`, `param`, `cond`, `min`, `nom`, `typ`, `max`, `unit`, `fn` (footnote ids), `guar` (production-tested / design) |
| `timing` | as `limit`, plus `edge` |
| `pin` | `pin` (number/ball), `name`, `dir`, `type` (od, pp, analog, supply), `active`, `desc` |
| `rule` | `text`, `cond` (behaviour rule, "OK goes low on fault, latches until EN toggles") |
| `register` | `name`, `addr`, `width`, `reset`, `access` |
| `field` | `reg`, `name`, `bits` ([hi, lo]), `reset`, `access` (SystemRDL vocabulary: rw, ro, wo, rclr, woclr), `enum` |
| `quirk` | `class` (table-vs-table, text-vs-figure, footnote-condition, silent-default, must, do-not, absence, variant, doc-artefact, not-prod-tested, narrow-condition, provenance), `text`, `refs` (other fact ids) |
| `figure` | `fig`, `caption`, `what` |
| `strap` | `pin`, `condition`, `value` (address or mode straps) |
| `passthrough` | `from_pin`, `to_pin`, `when` (transparent-part map: switches, shunts, buffers) |
| `requirement` | `rid`, `text`, `fw_relevant` (PRD) |
| `conflict` | `refs`, `sources`, `text` |

`prov` classes: `deterministic`, `deterministic+crosschecked`, `llm-verified`,
`llm-unverified`, `engineer-declared`, `engineer-confirmed`, `measured`.

### `facts.txt` line format

One record per line, self-describing, grep-complete:

```
<id> <key>=<value>; <key>=<value>; ...; cite=<doc_id> p<N>[ fn<k>]; prov=<class>
```

### Markdown row rules (tier 2)

One condition per row; no merged cells; the symbol and unit repeated on every
row; footnotes pulled onto the rows they qualify (a `note` column) and listed
under the table; native names verbatim; an `aka:` line under each heading for
alternative names (for example "master/controller").

## How a session sees the atlas

- **The map loads every session** (owner decision). `<fw>/CLAUDE.local.md` holds one
  line, `@embarch/atlas/MAP.md`. Claude Code auto-loads `CLAUDE.local.md` from the
  repo root and resolves the import relative to it
  (https://code.claude.com/docs/en/memory.md). `CLAUDE.local.md` is added to the fw
  repo's `.git/info/exclude`, so no tracked file changes. The MAP is byte-stable,
  so it stays in the cached prefix.
- When an atlas becomes current (its commit equals HEAD), `embarch/atlas/MAP.md` is
  replaced by that atlas's MAP. When HEAD moves past it, the MAP's first line says it
  is stale and names `/atlas-update`.
- The MCP server, once built, is registered with `alwaysLoad: true` in its
  `.mcp.json` entry (https://code.claude.com/docs/en/mcp.md), so its 7 tools skip
  deferred tool search.

## Schematic ingest ladder

First match wins; each rung cross-checks the one above.

1. Native EDA project, when supplied (future adapter).
2. **Embedded PDF netlist**: Altium Smart PDF bookmarks (`Sheet > Nets > NET >
   Pins > REF-PIN`) and component-parameter JavaScript popups (MPN, value, DNI).
   Invisible anchor words (`PI<ref>0<pin>`, `CO<ref>`, `NL<net>`, `PO<net>`, `_`
   written as `0`) give positions.
3. Geometric tracing on the vector PDF (wires, junctions, pins, labels, ports).
4. Vision on crops (Claude Code `Read` of a rendered crop), facts `llm-unverified`
   until matched.

Multi-part symbols (`U7A`/`U7B`) fold into one physical part. Interfaces between
boards are **proposed** with a score (net-name agreement k of n under each
candidate mating rule) and **confirmed** by the engineer.

## `boards/<label>/netlist.json`

```json
{"board": "B", "source": {"file": "...", "sha256": "..."}, "method": "smartpdf-outline",
 "sheets": {"<sheet>": {"page": 3}},
 "components": {"U3": {"mpn": "...", "value": "...", "dni": false, "pins": {"1": {"name": "PA0", "net": "NET_A"}}, "pages": [7]}},
 "nets": {"3V3": {"pins": ["U3-19", "R5-1"], "sheets": ["..."], "labels": 4, "ports": 2}}}
```

`bom.json` is the list of `{ref, mpn, value, dni, description, datasheet_link}`.

## `atlas.toml`

```toml
[project]
fw = "/abs/path/to/fw-repo"          # the firmware git repo
west_topdir = "/abs/path/to/ws"      # the west workspace holding .west/
sources = "embarch/hardware"         # where the engineer drops documents, relative to fw

[mcu]
part = "<mcu part>"                  # used to pick SVD/RM
svd = "/abs/path/to/part.svd"        # local file, never fetched
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
| `init <fw>` | `project` | writes `atlas.toml` with defaults |
| `inventory` | `inventory` | manifest + per-page hashes + diff vs last |
| `schematic <pdf> --board <label>` | `smartpdf` | netlist.json, bom.json, nets.md, parts.md |
| `interfaces` | `interfaces` | propose board-to-board mating rules with scores |
| `missing` | `missing` | missing-documents questions from BOM vs manifest |
| `doc prep <pdf>` | `pdftext` | doc.json, pages/, toc.md, page classes (image-heavy, table-heavy) |
| `doc check <doc_id>` | `check` | checks A–M, writes checks.json, sets `prov` |
| `doc render <doc_id>` | `render` | tier-2 tables and facts.txt from records |
| `doc regparse <doc_id> --template ti` | `regparse` | deterministic register records |
| `mcu svd <svd>` | `svd` | registers/fields as records under the MCU doc |
| `mcu vote --svd-doc <id> --rm-doc <id> [--header <h>] [--periph P]` | `vote` | SVD vs header vs RM, `conflict` / `quirk` (correct omission) / `rule` records under the RM doc |
| `mcu rm-outline <rm>` / `rm-text` / `rm-check` / `rm-freeze` | `rm` | outline with page ranges; one work item's text with `[p.N]` markers; verify distilled RM files; freeze them in `cache/llm/` |
| `mcu rm-sections <atlas>` | `rm` | RM sections for peripherals in use |
| `build <target> [<commit>]` | `build` | worktree, west build, artifacts, stamp |
| `join <atlas>` | `join` | dt.json, in_use.json, pins, checks, mismatches, MAP.md |
| `second-look queue <atlas>` / `second-look record` | `secondlook` | facts this atlas uses that need a second look; record verdicts |
| `diff <atlas> <atlas>` | `diff` | what changed between two atlases |
| `status` | `project` | atlases, which is current, which are stale |

## What an LLM step may and may not do

- May: read page images and the text layer; write records, prose, the card, the
  section plan; propose invariants (`checks` records); propose interface rules.
- May not: write a number that is not on the cited page (untagged), invent a
  citation, edit a deterministic record, delete a failing record, fetch anything.

## Query surface

`query.py` holds the seven lookups as plain functions returning markdown: `part_card`,
`doc_toc`, `doc_section`, `find_fact` (BM25 over every `facts.txt`), `register` (MCU
`PERIPH.REG` or `<part>.<REG>`, twins merged, rules/quirks/conflicts and firmware
references joined), `pin_trace`, `mismatch_report`. Each answer starts with a `src:`
line and is paged at a per-tool token budget with `next=<offset>`. Two doors call the
same functions: `embarch-atlas q <verb>` and `embarch-atlas mcp` (stdio, `mcp>=2.2`,
optional extra). Register the server per project with
`claude mcp add --scope local embarch-atlas -- embarch-atlas --atlas-dir <fw>/embarch/atlas mcp`
and `alwaysLoad: true`.

## Not yet built (deliberately)

The embarch-ui view, a native EDA adapter, geometry
and vision rungs of the schematic ladder, PRD → code trace links.

## Conventions added by the first port (2026-09-30)

What the four module ports needed beyond the sections above. Fold each into its
section when that section is next edited.

**Build and atlas dir.**
- The worktree is `.work/<commit12>/<fw basename>`: Zephyr names a module without a
  `name:` after its directory, so the basename must match the fw repo's.
- The fw repo is itself a Zephyr module and west lists the *main* checkout, so
  `build` passes `-DZEPHYR_MODULES=<every other west module>;<worktree>` and then
  proves the result: no TU, include path or DTS input from the main checkout
  (`atlas.json` `build.from_worktree`). A leak fails the build.
- `atlases/<id>/` also holds `inputs/` (netlist and BOM snapshots, for `diff`),
  `inputs.json`, `second_look_queue.jsonl`, and `build/zephyr.dts.d`,
  `build/zephyr_modules.txt`.
- The stamp adds `target`, `app`, `schematic_boards`, `mcu`, `build{...}` with the
  from-worktree evidence, and each west project's `head`.
- Optional engineer tables: `boards/straps.jsonl` (address/mode straps when no
  datasheet record exists yet) and `boards/bindings.json` (compatible → MPN, prop →
  pin, extra rails). Engineer answers to `missing.md`: `inventory/answers.toml`
  (not built yet).
- `atlas.toml` may add `[project] west` (the west executable), `[mcu] ref` (the MCU's
  refdes), `[mcu] rm_core` (override the cross-cutting RM core list).

**Schematics.** `netlist.json` components add `parts`, `sheets`, `dni_evidence`;
nets add `auto`; sheets add `parent`; top level adds `counts` and `issues` (e.g. a
printed comment naming a different part than the MPN parameter). `bom.json` rows add
`comment`, `manufacturer`, `footprint`, `in_bom`, `pins`. `boards/system.json` holds
`interfaces` (engineer-declared, never overwritten), `proposed` (machine, scored),
`name_check`, `inputs`, and a `board-to-board-check` scoring each declared interface.

**Manifest.** Kinds add `errata`, `notes`. Entries add `doc_id`, `parts`,
`page_words`, `pages_textless`, `net_info`, `origin`, `superseded_by`,
`duplicate_of`; the diff adds `changed_pages`, `previous`. A document with no
revision gets `@norev`.

**Records.**
- New kind `check` (an invariant): `expr`, `expect`, `why`.
- Quirk classes add `text-vs-table`, `figure-only`.
- `q` may be a list. A text record with no `q` fails anchoring and stays `llm-unverified`.
- `src` adds `svd` and `header`; for those `p` is null and the doc id may omit `@rev`.
- Deterministic producers write `records/<producer>.jsonl` (e.g. `regparse.jsonl`). A
  deterministic record and an LLM record may share an id as twins; `facts.txt` keeps both.
- `field.reg` is the register's record id; `bits` is `[hi, lo]`; resets are `0b…`/`0x…`
  strings; access adds `w1s`, `rsvd`. `±X` is stored as `min: -X, max: X`.
- `vote` writes under the **RM** doc (each record cites an RM page); `refs` point at SVD
  ids; the verdict leads `note` (`svd-defect: …`); `sources` are `"<doc> <loc>: <says>"`.

**Documents.** `docs/<doc>/` adds `toc.json`, `pages/pages.json` (page classes and
hashes). Rendered tables sit between `<!-- atlas:table … -->` and `<!-- atlas:end -->`;
`<!-- atlas:covers … -->` declares what a section file covers. The RM doc adds
`outline.json`, `worklist.json`, `availability.json`, `rm_regs.json`, `rm_checks.json`,
`vote.md`/`vote.json`, `s/<ch>.card.md`, `s/<ch>.must.md`, `s/<ch>.fields.md`; the SVD
doc adds `peripherals.json` and `s/<PERIPH>.md`. RM tier-2 citations are `[§sec p.N]`;
datasheet ones `p.N`. Token budgets are counted in o200k (real Claude counts run ~1.4×).

**Caching.** RM worklist units are keyed by section id + page-text hashes + procedure
version; the model goes into the cache entry, so regrouping work items never
invalidates frozen output. Second-look verdicts are frozen as `cache/llm/sl-<key>.json`.
