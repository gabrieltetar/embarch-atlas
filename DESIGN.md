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
   A number is read as the page prints it: a minus set off by a space (`- 0.3`) is a sign
   unless a digit, `%` or bracket stands before it (a range, `10 - 90%`); `1,000` and
   `12 000` are one number, but `355 505` (three digits, a space) is two table columns.
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
   never written. `config` and `dt_node` may *read* a build dir the caller names, and
   say which build answered (owner decision, 2026-10-06: branch work is mostly on
   unpinned builds). **A target may state that it has no Zephyr board yet** (`no_board = true`
   in `atlas.toml`): its atlas is then pinned to the target and the firmware commit, made with
   no worktree and no west build, and its stamp says there is no build and why. It holds the
   schematic and datasheet side only; "A target with no Zephyr board yet" below lists what it
   lacks and what it keeps in a reduced form.
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
                                sha256, per-page hashes, text-layer coverage, net_info, kind_classified (when
                                the engineer overrode the kind), needs_export (a Word PRD); diff vs previous
  text/<sha256[:16]>.txt.gz     page text of each PDF: a cache keyed by content, outside the manifest
  matches.json                  every rung result, before any answer, keyed by the documents and the BOM
  missing.md                    the proposed matches to confirm or reject, then questions for the engineer:
                                missing datasheets, errata, declarations, Word PRDs to export; the documents
                                nothing uses
  answers.toml                  the engineer's answers to missing.md (`missing --answer`, `--confirm`,
                                `--reject`, `--kind`; below)
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
                                dt_intent (node -> why it is okay without a driver: folds dt-no-driver),
                                packages (`<ref>` or `<board>:<ref>` -> the part's package, engineer-declared,
                                as the datasheet's `pin` records `package` name it)
cache/llm/<key>.json            frozen LLM outputs; second-look verdicts as sl-<key>.json
cache/kconfig/, cache/dt/       a named build dir's kconfig/dt extraction, keyed by the content it came from
cache/footprint/<elf sha>.json  one ELF's sections (objdump -h) and sized symbols (nm -l -S), for footprint and stacks
.work/<commit12>/<fw basename>  the build worktree
atlases/<target>@<commit12>/
  atlas.json                    the stamp (below)
  build/                        edt.pickle, .config, compile_commands.json, zephyr.elf, zephyr.dts,
                                zephyr.dts.d, zephyr_modules.txt, build_info.yml
                                copied from one image: under a multi-image sysbuild (app plus MCUboot)
                                the application's, picked by `domains.yaml` `default:`, else the app directory's
                                name, else an error naming the pickles found (`kconfig.image_dir`). A build dir the caller names
                                (`buildq.named`) chooses the same way, with the app read from the atlas stamp
  inputs/, inputs.json          netlist and BOM snapshots the atlas was joined against (for diff); the
                                manifest fingerprint and file list, and each netlist's source PDF hash,
                                the join read (`sources`, `source_files`, `schematics`: for staleness;
                                `missing_boards`: declared boards that had no netlist; `answers`: a hash of
                                the confirmed and rejected documents, when there are any)
  dt.json                       pins, devices, buses, IRQ/EXTI from the edt. Pin use is decoded for
                                STM32 (`pinmux`, ports `PA4`) and Nordic nRF (`nordic,nrf-gpio` ports,
                                pinctrl `psels` = `NRF_PSEL(fun, port, pin)`, pins `P0.04`); `pin_decoder`
                                names which, null for any other family, and `checks.md` then says the
                                pin-level checks had no input
  kconfig.json                  every Kconfig symbol: value, assigning fragment, depends on (met?),
                                selected/implied by, the default that applies, where defined.
                                Written by `build` while the workspace's Zephyr is the build's; for an
                                atlas without one, `kconfig-extract <atlas>` writes it from the build dir
                                under the same guard (the stamp's `build.kconfig` keeps the failure text)
  in_use.json                   peripherals: dt-okay / instantiated / touched-by-code
  pins.md, pins.json            pin/net map
  pin_slice.txt                 always-loadable slice (~1.5K tokens)
  checks.md, mismatches.json    mismatch report: checks run and passed, then candidates by class
  codecheck.json, codecheck.md  driver register constants vs datasheet records
  limits.json                   DT limit properties (SI) vs the part's datasheet limits
  second_look_queue.jsonl       facts this atlas uses that still need a second look
  second_look.jsonl             second-look verdicts
  graph.json                    the whole atlas as one graph, for the embarch-ui Atlas tab (below)
  MAP.md                        tier 0, <= 5K tokens, byte-stable: the MCU, boards, parts with their
                                documents, and the documents tied to no part (standards, the PRD)
current.json                    which atlas matches the checkout (target, commit)
MAP.md                          copy of the current atlas's MAP.md: the one stable path the session imports
```

### The stamp (`atlas.json`)

`target`, `board`, `app`, `schematic_boards`, `mcu`, `commit` (full sha), `dirty:
false` (a worktree is always clean), `west` (every project's path, revision and
`head`), `sources` (sha256 of `inventory/manifest.json`), `schematics` (sha256 per
board PDF), `build{...}` with the from-worktree evidence (a target with no board yet has `board: null`
and `build: {built: false, reason, fw, west_topdir}`), `tool` (embarch-atlas
version + git sha), `created` (UTC). An atlas is **stale** when HEAD or any source
hash differs, when a document was confirmed or rejected since its last join, or (an atlas with no
build) when `atlas.toml` now names a board for its target; it is
never edited, only superseded. Documents and schematics are what `join` reads, so the
hashes the last join recorded in `inputs.json` stand in for the build's: an atlas joined
again after a document change is fresh.

### A missing kconfig.json

`build` writes `kconfig.json` while the workspace's Zephyr is the build's; if the extraction fails (the
build's Python lacks kconfiglib, say) the stamp records `build.kconfig: "not extracted: <reason>"` and the
build warns. `build` of that commit then says "already built", so the recovery is `embarch-atlas
kconfig-extract <atlas>`: it reruns the extraction over the build dir the stamp names (`.work/build-<target>-<commit12>-<args8>`, where `<args8>` hashes the
board, app and extra args, so a changed argument list gets a fresh directory: west's `-p auto` does not clear a
sysbuild-to-plain cache),
no worktree rebuild, refusing under the same `workspace_mismatch` guard. If the build dir is gone or the
workspace's Zephyr moved, there is no safe route short of `build <target> --force` (a full rebuild), and
the command says exactly that. `config` on such an atlas quotes the recorded reason and names the command.
The stamp is not edited: `kconfig.json` existing is what `config` reads.

### Building from the worktree

The worktree is `.work/<commit12>/<fw basename>`: Zephyr names a module without a
`name:` after its directory, so the basename must match the fw repo's. The fw repo
is itself a Zephyr module and west lists the *main* checkout, so `build` passes
`-DZEPHYR_MODULES=<every other west module>;<worktree>` and then proves the result:
no TU, include path or DTS input from the main checkout (`atlas.json`
`build.from_worktree`). A leak fails the build.

### Which `fw` a process uses

`[project] fw` names the main clone, but a session may run in a linked git worktree of it (the desk's Start
work makes one beside the clone and links the clone's untracked `embarch/` folder and `.mcp.json` into it).
`project.resolve_fw` picks the checkout per process, `atlas.toml` untouched:

1. `EMBARCH_ATLAS_FW`, when set, wins (an explicit override; there is no `--fw` form of it, `--fw` stays the
   configured repo).
2. Else, when the working directory is inside a git worktree of the configured repo (same
   `git rev-parse --git-common-dir`), that worktree's top.
3. Else the configured `fw`, as before.

Every reader goes through it (`fw_dir`: `status`, the staleness check behind every MCP move, `inventory`,
`live`, `build`), so the HEAD the atlas is compared with is the worktree's branch. `status` names the worktree
(`fw: <path> (worktree; atlas.toml names <main>)`; the JSON has `fw_is_worktree`, `fw_configured`).

`build` takes the commit and makes its build worktree from the resolved checkout, but keeps the *configured*
repo's basename for `.work/<commit12>/<basename>` (a linked worktree's folder is named otherwise), excludes the
configured repo from the west module list (west lists the main clone), and counts an input from either the main
clone or the linked worktree as a leak. `atlas.json` `build.fw_checkout` records the checkout used.

The `embarch/` folder is the main clone's in every worktree, so there is **one atlas folder**: a `build` from a
worktree writes the atlas the main clone's sessions read. It is one atlas set pinned to whichever commit was
built last; `status` in the other checkout then reports the atlas stale (commit differs from HEAD) until it
builds its own. One atlas folder per worktree is not done.

### A target with no Zephyr board yet

Bring-up of a new PCB is when the atlas has most to answer and when no west build exists. A target says so
explicitly: `no_board = true` on its `[[targets]]` entry (or `build --no-board` for one run; `update` reads
the entry). A **missing or misspelt `board` is still an error** that names `--board`; `no_board` is never
inferred, and `no_board` with a `board` is refused (drop it once the board exists). `fw` is the only other
required key: no `west_topdir`, `app` or worktree is needed, though a configured west workspace's project list is
recorded when it answers.

`build` on such a target resolves the commit and writes `atlas.json` with `board: null` and
`build: {built: false, reason, fw, west_topdir}`, and nothing else: no worktree, no `.work/` build dir, no
`build/`. The atlas is `<target>@<commit12>` like any other, pinned to that target and commit. `join` then runs
every step that does not read the build. A normal `build` for the same target and commit (the target gained a
board) supersedes it, replacing the directory, and until then `status`, `MAP.md` and the `src:` line's `stale=`
say the target now names a board. `build` with `no_board` against an atlas that was built with a board keeps
that atlas and says so; `--force` replaces it.

**Absent** (never written; `common.NO_BUILD_ABSENT` is the list `MAP.md` and `status` print): `build/`;
`kconfig.json`; `dt.json` and `in_use.json`; `codecheck.json` and `codecheck.md`; `limits.json`; the reference
manual's `worklist.json` (the chapters in use come from the peripherals in use); and the lookups `config`,
`dt_node`, `footprint` and `stacks` on the pinned build, which answer that the target has no Zephyr board
yet (`build=<dir>` still names a build to read). `codecheck`, `limits` and `mcu rm-sections` say the same.

**Degraded, not absent** (`common.NO_BUILD_DEGRADED`; each says what it lacks, and an empty cell there is
absence, never a pass):
- `pins.json`, `pins.md`, `pin_slice.txt`: the schematic side stands (package pin, net, far end, datasheet
  pin record, interfaces); the DT use and driver columns are empty, and `pins.md` says so.
- `checks.md`, `mismatches.json`: the checks that read only the netlists, the BOM, the interface mating rules and
  the distillations run (the same set `q mismatch_report` runs before any atlas exists, `mismatch.NEEDS_BUILD`
  being the rest); each check that needs a devicetree, an ELF or driver sources is listed under Not run with the
  reason and never counted as passed. The second look still counts, because the atlas keeps `second_look.jsonl`.
- `MAP.md`: a "No build" section names every absent and reduced output; no peripherals-in-use or RM-chapter lines.
- `graph.json`: the schematic, interface and document layers stand; the code layers are empty and `code_note` says why.
- `register`: the datasheet and SVD answer is whole; its Firmware references section says it is absent.
- The second-look queue: ranks the facts the join reads from the schematic and the datasheets, nothing from the DT.

## Identifiers

- `doc_id` = part or document slug + `@` + revision: `lsw100@04b`, `mcu-rm@6`,
  `bb200@B` (invented examples). Slugs are lower-case, no spaces. No revision gives
  `@norev`; a non-PDF source (an SVD) may omit `@rev`. The slug, by kind: a datasheet's
  part when it is a clean part number, else its document number; a reference manual's or
  errata sheet's document number, else its part; an app note's document number; anything
  else, and any of these with neither, the file stem. The **classified** kind decides, so
  an engineer's kind override never moves a doc_id. A package name or a vendor-internal
  number is never a part token. A Word file and the PDF exported from it share a doc_id;
  only the PDF is prepped. `update` finds a translation by content when a doc_id moved
  (`distillation_of`).
- Revision and document number also come from a page-one header block of the form
  `REV. A` / `DOC NO. 001` (upper-case, line-anchored; prose never matches), in the
  manifest and in `doc.json`, when no vendor convention named them. `doc check` prints
  `K did not run` when no record was compared with a column (no MIN/TYP/MAX header).
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
| `pin` | `pin` (number/ball, or [number, ball]), `package` (the package this record's pin is for, as the datasheet names it, when one part's pinout changes with its package; absent: every package; the join reads the records for the board part's `bindings.json` `packages` entry plus those with none, and reports a pin ambiguous (`pin-package-ambiguous`) when no package is declared and the records for its number differ by package, never taking the first), `name`, `dir`, `type` (od, pp, analog, supply, ...), `io` (an MCU pin's I/O structure as printed: FT, FT_fa, TT_a...), `active`, `pull`, `desc`, `flow` (power and supply pins: source, sink, bidir, sense, local, as the pin table states it) |
| `rule` | `text`, `cond`, `recover`, `steps` (behaviour rule, "OK goes low on fault, latches until EN toggles") |
| `register` | `name`, `addr` (int or `0x..`), `bank` (the page or bank on a part with paged register maps; absent for the main map; check G, codecheck and the live module key a register on `(bank, addr)`), `width`, `reset` (`0b…`/`0x…`), `access`, `summary` |
| `field` | `reg` (the register's record id; a field has no `bank` of its own, it takes its register's), `name`, `bits` ([hi, lo]), `reset`, `access` (SystemRDL vocabulary: rw, ro, wo, rclr, woclr, w1s, rsvd), `enum` |
| `quirk` | `class` (table-vs-table, text-vs-table, text-vs-text, text-vs-figure, figure-only, footnote-condition, silent-default, must, do-not, absence, variant, doc-artefact, not-prod-tested, narrow-condition, provenance), `text`, `refs` (other fact ids) |
| `figure` | `fig`, `caption`, `what` (equations: `src: equation`) |
| `strap` | `pin`, `condition`, `value` (address or mode straps; a fixed address has condition `fixed`) |
| `variant` | `mpn` (the orderable part number), `options` (an object of option name to value: `{"vout": "3.3 V", "package": "SOT-23-5"}`; at least one). One record per orderable part, from the ordering table. A `variant` lists the parts; a `rule` is behaviour; a `quirk` of class `variant` flags a datasheet row whose value differs by variant without listing them |
| `passthrough` | `from_pin`, `to_pin`, `when` (transparent-part map: switches, shunts, buffers) |
| `bus` | `iface`, `byte_order` (lsb-first, msb-first), `reg_addr_bytes`, `windows` ([{from, to, addr, base}]: register ranges per bus address, for a part with more than one), `bank_select` ({reg, values}: the 8-bit page-select register and the value that selects each `bank` the register records state, for a paged part) |
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
(bits, widths, enum keys, check expressions) is not a numeric claim, nor is a step marker
`(1)`, `(2)` inside a clause; a real number in parentheses, `(5 V)`, still is. A cite that names another document (`other-doc p.7`, the shape a conflict row's Sources cell has) is anchored on that
document's page, beside this one's; a number on neither cited page still fails. A non-PDF source
(an SVD) gets only the record-level checks.

## How a session sees the atlas

- **The map loads every session** (owner decision). `<fw>/CLAUDE.local.md` holds
  `@embarch/atlas/MAP.md`. Claude Code auto-loads `CLAUDE.local.md` from the repo
  root and resolves the import relative to it
  (https://code.claude.com/docs/en/memory.md). The MAP is byte-stable, so it stays
  in the cached prefix.
- Between `init` and the first atlas the import would resolve to nothing, so `init` writes a placeholder
  `embarch/atlas/MAP.md` (never over an existing one) saying no atlas is built, not to answer hardware
  questions from memory, and to run `/atlas-translate-docs` then `/atlas-build`. Its first line is not an
  atlas header, so `mark_map` leaves it alone and the first `join` that makes an atlas current replaces it.
  The `atlas-use` skill's step 1 knows the placeholder: it says no atlas is built, names the same two
  steps and stops before step 2; `tests/test_project.py` reads both and fails if the steps drift.
- When an atlas becomes current (its commit equals HEAD), `embarch/atlas/MAP.md` is
  replaced by that atlas's MAP. When HEAD moves past it (or a source changes), `status`
  puts a `> **Stale:**` line first in that MAP with the reasons and `/atlas-update`; the line
  goes once the MAP's atlas is fresh again. `status` runs when the MCP server starts and when an
  atlas skill loads, but a session may do neither, so staleness is also on **every lookup's `src:`
  line**: `stale=<reasons> (HEAD <sha12>)` whenever HEAD is not the atlas's commit or a source
  changed (`stale.py`). It is computed per call and cached on the mtime of `.git/HEAD`, of the ref
  it names and of `packed-refs`, plus the stat of the stamp, manifest and board PDFs: one
  `git rev-parse` when HEAD moves, a few `stat`s otherwise (measured well under 1 ms). A
  `current.json` whose commit is not HEAD is no match; the lookups then read the newest atlas and say
  it is stale.
- The MAP's reference-manual line reads the worklist of *this* atlas. `join` writes that worklist
  (`update.rm_todo`) just before it renders the MAP, so `update` and a hand-run `build` + `join`
  both list the chapters in use; a worklist left for another atlas is rewritten, not skipped.
- **The MCP server** is registered in a project `.mcp.json` with `alwaysLoad: true`
  (https://code.claude.com/docs/en/mcp.md), so its 7 tools skip deferred tool search.
  Claude Code asks the engineer to approve a project server on first use.
- A tool declares `readOnlyHint` only when it cannot change the board, because a host (plan
  mode, an auto-approval rule, a permission classifier, a worker's allowlist) trusts the hint.
  The seven static tools and `live_stacks` carry it. `shell` carries it because it runs only
  commands that match the `[live] read_only` list (default: `device list`, `kernel thread stacks`,
  `kernel uptime`, `i2c read *`, `i2c read_byte *`; `i2c scan` stays out, it addresses every device
  on the bus) and refuses any other, naming `shell_run`. `reg_read` carries it and refuses
  `allow_clear=true`, naming `reg_read_clear`. `shell_run` (any command), `reg_read_clear` and
  `reg_read_bank` (a page select is a write) declare no hint, so the host asks. The CLI keeps one verb each (it has no hint). Core's hardware
  lock serializes the live calls Claude Code runs in parallel. (Replaces the 2026-10-06 choice to
  give `shell` the hint; field use showed the shell is mostly a write path.)
- `init` writes both files and adds them to the fw repo's `.git/info/exclude`, so no
  tracked file changes.

## Query surface

`query.py` holds the thirteen lookups as plain functions returning markdown: `part_card` (`board="all"`
adds every board's refs and nets, a label one board's; no tool documents an empty string as a value: a model
writes `"board": }` for it, and the call never reaches the server. **With no atlas** it still answers a board
MPN: the MPN-to-document join `pins.json` would hold is made from the netlists and `coverage.py`
(`Coverage.part_docs`), refs and nets come from the netlists, and the `src:` line says
`(no atlas built: ...)`; an MPN whose document is covered but not distilled answers with the covering file
and "not translated yet"),
`doc_toc` (`under` takes an id, a title, or a word of a title, which resolves to the one
matching section that holds all the others, else lists the matches; no match lists the nearest titles),
`doc_section` (a toc section with no `s/` file answers its title, pages and the next call, and an RM register
section points to `register`; `part_card`'s and `doc_section`'s `src:` line carries `pdf=<path relative to the
firmware repo>`, because a cite gives only a doc id and a page and a guessed path fails the Read),
`find_fact` (BM25 over every `facts.txt` and the distilled prose of `card.md` and `s/*.md`
outside rendered tables, as `kind=prose` lines; a query term naming one part narrows to it like
`part`; with no `limit`, the best 10 hits scoring at least 0.4 of the top one, and the
header names the query terms no fact contains), `find_text` (the step after `find_fact`: plain words
or a case-insensitive regex over every page of a document's text layer, `pages/p<NNN>.txt`, one block per
hit with the physical page, the deepest toc section holding it, `s/<file>` or "not distilled", the line and
`context` lines; `section=` limits it to that section's pages; the `src:` line says
`<doc> text-layer, <n> pages hits=<n>`. Each answer appends `{doc, section, page, tool}` of the hits it
returned to `<atlas dir>/usage.jsonl`, so undistilled sections can be ranked by use; the log is write-only,
never read back into an answer, and a failed write is ignored. A toc gives pages, not lines, so a page
shared by two sections is credited to the deepest, latest-starting one. Measured on a synthetic 400-page
text layer: a symbol, a state name and a table title each answer in 11-14 ms), `register` (MCU `PERIPH.REG` or
`<part>.<REG>`, twins merged, rules/quirks/conflicts and firmware references joined; a
register tabled as `<REG>_MSB` + `<REG>_LSB` bytes answers to `<REG>` as one 16-bit view; firmware's
CMSIS struct member (`UCPD1.CFG1`) finds the SVD register at the same address, read from the device header
that `mcu vote --header` recorded in the RM doc's `vote*.json`: no recorded header, no alias),
`pin_trace` (MCU pins, from `pins.json`), `net_trace` (any net, part pin or part on any board,
read from the netlists through the join at query time: members across connector mates, then
what feeds the net from datasheet pin records with `flow` only, the feeding parts' enables and
their MCU port or pull, and the datasheets still needed; a part the engineer answered with a generic
card is walked through and its hop marked `[generic card, engineer-declared]`), `mismatch_report`
(a stored atlas's report, as written; **with no atlas** it runs, in memory and writing nothing, the checks
that read only the netlists, the BOM, the interface mating rules and the distillations' pin and conflict
records: net names across a board connector, DNI parts on the MCU's paths, pulls against the MCU's I/O
supply, the MCU symbol against its datasheet, far-end output level against pin tolerance, driver claims
against the datasheet. Every check that needs a devicetree, an ELF, driver sources or a build (`mismatch.NEEDS_BUILD`) is
listed under "Not run" with what it lacked, never counted as passed; the `src:` line says
`boards/<labels> netlist.json + BOM + coverage + distillations, no atlas`), `config` (a
Kconfig symbol, its full answer; several names, as a list or comma-separated, or `*` patterns,
one line per symbol: value, the fragment line or the select or default that set it, and an unmet
`depends on`) and `dt_node` (a node by path, label, alias, chosen name or compatible), both
taking `vs=<build dir | pinned>`: only what differs from that build (a symbol; a node only one
build has, or a node's status, compatible, labels, property or pin), with both values and where
each side's was set, under a `src:` line naming both builds (`config` reads absent, unset and `n`
as the same value; no name, or `*`, compares everything), `footprint` (flash image and RAM from
the ELF's sections, against the flash, SRAM
and code partition the devicetree's chosen nodes give; the largest contributors by dir, file or
symbol, or the change against another build) and `stacks` (every thread stack in `noinit`, its
size and defining line, and the `*_STACK_SIZE` option it most likely comes from, by inference),
all four from the pinned build or a named build dir (`buildq.py`, `footprint.py`; a named build's
extraction runs only while the workspace's `git describe` equals the build's `BUILD_VERSION`,
else `config` gives the value and the fragment assignments only, each one-line answer marking
`[not given: ...]` what it lacks, and a `dt_node` comparison names the side that cannot load).
Each answer starts with a `src:`
line and is paged at a per-tool token budget with `next=<offset>`. Every lookup takes that
`offset` (function, `q` verb and MCP tool alike; a test walks `TOOLS` and fails for one that
does not), so a cut answer is always continuable. `offset` counts characters of the body, the
text after the `src:` header's blank line; each page repeats the header and is cut on a line
boundary (mid-line only for a line longer than the budget), so the pages' bodies, followed
through `next=` until no truncation line remains, concatenate to the uncut body exactly once.
Two doors call the
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
                       "sheets": ["..."], "pins": {"1": {"name": "PA0", "name_source": "pdf-text", "net": "NET_A"}}, "pages": [7]}},
 "nets": {"3V3": {"pins": ["U3-19", "R5-1"], "sheets": ["..."], "labels": 4, "ports": 2, "auto": false,
                  "aliases": ["NetC4_1"]}},
 "counts": {"geometry": {}}, "issues": []}
```

**Pin names and where they came from.** A pin's name is the visible word the PDF prints just past the
end of its anchor, on the pin's axis, for parts with 3 or more pins (`MIN_NAMED_PINS`); a module whose pad
names are not in the PDF's text keeps `name: null`. `boards/<label>/pin_names.json` is the engineer-owned
overlay for those: `{"board", "pins": {<ref>: {<pin>: {"name", "prov", "note"}}}}`, written only by
`declare-pin-name`, never by `schematic` (as `system.json`'s `interfaces` are never overwritten). `prov` is
`engineer-declared`, `engineer-confirmed`, or `llm-unverified` (a name read off a rendered crop, ladder rung 4,
until the engineer confirms it). `netlist.json` is the merged view every later step reads: each named pin
carries `name_source`, `pdf-text` or `overlay:<prov>`, so a hand-typed name never looks like a printed one.
A PDF-text name always wins; an overlay entry for a pin the PDF names is left unused. A re-run of `schematic`
re-applies the overlay, so its names stay in effect; `join` reads `netlist.json`, so `part_pin_name`,
`pin_trace` and the pin map see them (`pin_trace` marks an overlay name with `pin_name_source`). `counts`
gains `pins_named_from_overlay` and `pins_unnamed_on_3plus_pin_parts`, and `schematic` prints both beside
`pins_named / pins_on_3plus_pin_parts`.

**How nets join.** Each sheet's `Nets > NET` entry is a sheet net. Sheet nets of the same name are
one board net (Altium joins those through ports, sheet entries and power ports). Sheet nets that carry
a port of the same name on different sheets are one board net too, whatever each sheet calls the net;
the join is transitive. The joined net takes the first of its names, in natural order, that is not
auto-named (else the first name), and the other names are listed in its `aliases`; every query reads
the one net with the pins of all its sheets. A port that sits on a net, has no partner (the same port
name on another sheet, a sheet-level port of that name, or a net of that name on another sheet) is an
`issues` entry. The geometric cross-check (`geom.py`) still compares per sheet and per name, never
across a port, so its `crosscheck` mark on a joined net is the picked name's sheet net only.

`issues` holds oddities worth an engineer's look (e.g. a printed comment naming a
different part than the MPN parameter). `bom.json` rows are `{ref, mpn, value, dni,
description, datasheet_link, comment, manufacturer, footprint, in_bom, pins}`.
`boards/system.json` holds `interfaces` (engineer-declared, never overwritten; `declare-interface` writes an entry there: a rule or a pin map, `prov`, a note),
`proposed` (machine, scored), `name_check`, `inputs`, and a `board-to-board-check`
scoring each declared interface.

## Live (`live.py`)

The live module reads the running board. It is not part of the thirteen lookups: the atlas never
touches hardware, so `reg_read` names addresses and decodes the answer, and embarch-core does the
read (its decision 81) through the `embarch-api mem-read` subcommand, which resolves Core and its
token the way every suite tool does. Core picks the probe and chip from the role's enrollment,
runs the board-identity gate, halts a running core for the read and lets it run again. One-off
reads halt because some MCUs sleeping without a DMA clock answer every running debug read with
0 or a stale word; keeping a DMA clock on would change the firmware for the debugger's sake.

- `reg_read PERIPH.REG` decodes one register field by field against the SVD records, with the
  reset value and the enum name; a register the SVD splits into alternate views at one address
  answers with all of them. `reg_read PERIPH` reads the whole peripheral as one snapshot (one
  request, one halt, ranges merged across up to 8 reserved words) and shows only the fields that
  differ from reset. Bits set outside every field are flagged.
- **Clock gate.** On STM32 a peripheral whose RCC enable bit is clear answers the debug read
  without error but not with its registers. `reg_read` adds the RCC register holding the enable
  field to the same halted read (`clock_gate`): the field is `<PERIPH>EN` in an RCC clock-enable register of the
  SVD (name holds `ENR`, not `SMENR`/`LPENR`; so `BDCR` and the sleep twins never match) (`UCPD2` -> `RCC.APBENR1.UCPD2EN`), with `CLOCK_EXCEPTIONS` for blocks that share one
  (`VREFBUF` -> `SYSCFGEN`; `RTC` -> `RTCAPBEN`, since `RTCEN` gates only the RTC clock source; `DAC` -> `DAC1EN`, `FDCAN1/2` -> `FDCANEN`, `HDMI_CEC` -> `CECEN`). Bit 0 answers "clock off (RCC.X.YEN = 0): these
  words are not the register's contents", prints raw words and marks nothing against reset; bit 1
  prints "clock on"; `NO_CLOCK_GATE` (`RCC`, `EXTI`, `IWDG`, core blocks) says so on
  purpose; any other peripheral says the clock state is unknown. Covered: STM32 families whose SVD
  names the gate `<PERIPH>EN` (the tables were checked against one STM32 vendor SVD (a small-MCU series) and nothing else: 50
  of 56 peripherals map, `EXTI`/`IWDG`/`RCC` have none, `COMP`, `DMAMUX` and `TAMP` answer unknown
  because no RCC field names them; other families are the same rule, unchecked; a spelling the
  tables miss answers unknown, never a guess; `EMBARCH_ATLAS_REAL_SVD=<file>` reruns that check). The clock word is a diagnostic: when adding it would cross `MAX_RANGES`/`MAX_WORDS`, the
  peripheral is read without it and the answer says the clock state is unknown because the read had no
  room, never a refusal. A part with no `RCC`
  peripheral (non-STM32) answers "clock state unknown" and reads as before. Enable bits are not
  verified on hardware (task atlas/013 debt).
- **Read-to-clear guard.** A register is refused by name, and left out of a peripheral read,
  when the SVD marks a field `readAction` (access `rclr`) or the reference manual's field text says
  reading it clears something ("cleared by reading the USART_RDR register", matched by a few
  wordings over `rm_regs.json`). `allow_clear=true` reads it and says what the read cleared.
  Write-one-to-clear (`woclr`, `wclr`: SVD `modifiedWriteValues="oneToClear"`, a printed `R/W1C`)
  describes a write, so it does not guard: reading such a flag leaves it set.
- **Undefined reset bits.** `mcu svd` claims a reset only for bits the SVD defines: a register with
  no `resetValue` anywhere has `reset: null`, and so does one whose `resetMask` is not all ones
  (its fields keep a reset only when every bit of the field is in the mask). The decode marks a
  field against its own reset, so an undefined field is never starred; `mcu vote` compares only
  the bits the SVD defines (the fields' resets when the register's is null).
- **The live shell.** The CLI's `shell <command>` runs one command on the role's console: the direct,
  writable signal the role declares (`[live] console` pins one), through Core's exchange route, the
  port opened and closed per command. The answer is the command's output alone: escape codes out,
  the echo and the closing prompt (the pinned build's `CONFIG_SHELL_PROMPT_UART`) dropped. The MCP
  `shell` tool matches the whole command (whitespace collapsed, a command with `; | & `` $(` or a
  newline never matches) against the `read_only` globs; `shell_run` takes any command.
  **Five answers**, the same for `shell`, `shell_run` and the CLI (`live.shell_status`; the CLI exits
  0, 1, 2, 3 as below, and `live reg-read` and `live stacks` exit 1 on a failed read):
  1. the output (exit 0), with "no prompt within the timeout: the output may be cut; pass a longer
     timeout_ms" only when bytes did arrive and the prompt did not;
  2. the prompt came and the output is empty (exit 0): a second exchange, `retval`, on the console that
     answered, and "the command printed nothing and returned N";
  3. "The command did not run: <Core's error>" (exit 1): the failure was provably before the write;
  4. "Nothing came back, not even the echo" (exit 2): zero bytes. No timeout advice (a longer wait does
     not help a console that sent nothing); it prints whether the line was written, the port Core opened
     (VID:PID, product, serial: a USB-UART bridge is not the DUT's own USB device) and the MCU core state
     (DHCSR flags, or why unavailable), from Core's reply (embarch-core decision 85), which names no cause;
  5. "the command was sent, then the console port went away" (`port_lost`) or "the command may have
     run: <error>" (exit 3), with whatever output arrived; the command is not sent again.
  **The fall-through rule.** `Console.exchange` moves to the next declared console only when the
  failure is provably before the write: an error text saying `nothing was written` (open failed, a
  zero-byte write), `signal-mismatch` (the port is not enumerable, a port wait ran out), an unknown or
  non-writable signal, or embarch-api absent. Any other failure (embarch-api's own timeout, a write
  that took some bytes, a flush failure, an unparsable reply) raises at once, `may_have_written` set,
  and a `port_lost` is an answer, never a retry (a reply with no byte at all moves on only as the *Which console* rule says): the line may be on the wire, and a
  resend would run a state-changing command twice. The classification reads Core's error text, since
  embarch-api forwards no status code, so an error text nobody listed counts as "may have written"; an
  older Core without the `written`/`port_lost`/`echo_seen` fields gets the same treatment on every
  console, the last or not (a silent reply is its answer and says "this Core did not say whether the
  line was written, so it may have run"; it is never passed over, see *Which console*).
  **Which console.** `[live] consoles` maps a devicetree UART node (path, node name, or a label of it)
  to a signal. When it is set and no `[live] console` pins one, `Console` reads the build's
  `chosen` `zephyr,shell-uart` (`dt.json`; the pinned build, or `build=` on `live_stacks`) and, only
  if `image_check` shows the board's flash holds that build, uses the mapped signal alone and says so
  in the `src:` line. A chosen node that maps to no signal answers "the image's shell is on `<node>`;
  no signal is declared for it" and tries nothing. When the build is not confirmed (or `consoles` is
  unset), the declared consoles are tried in name order, and `exchange` moves past one only when
  Core says `echo_seen: false` and not a byte came back (no text, no `port_lost`): nothing written was
  read, so nothing ran. That rests on the image's shell echoing what it reads (Zephyr's does by
  default); for an image with echo off, silence proves nothing, and this rule would resend the line on
  the next console even if it ran, so such an image should be pinned with `[live] console` or mapped
  through `consoles` with its build confirmed. Whether any real image has echo off is not known here.
  A Core that does not send `echo_seen` cannot make that claim, so its silence stops there as an answer,
  on every console. Gating on `written` is no substitute: a USB-UART bridge takes every byte, so
  `written` is true in exactly the case this rule exists for. A reply of any kind, including an error
  line, or a lost port, stops there. Presence of a signal never says which
  console the image uses: a USB-UART bridge is powered by the host, not the DUT.
  **How the atlas knows `retval` is absent.** From the pinned build's `kconfig.json`
  (`CONFIG_SHELL_CMDS_RETURN_VALUE`: set to `n`, or missing from a kconfig.json that lists every
  symbol, means no `retval`); then `retval` is not sent and the answer says the status is unknown and
  why. Without a kconfig.json the atlas asks the board: a `retval: command not found` reply is read
  the same way. Firmware source text is never consulted, and the pinned build is the atlas's claim
  about the board's image, not proof of it, which is why the `command not found` reply is read in
  either case. A command that returned nonzero still exits 0 from the CLI: it ran and answered.
- **External registers.** `reg_read <DT label>.<REG>` (or `<part>.<REG>` when the part has one
  device on the board) finds the device in the join's I2C table (bus, address, part, document),
  the register in the part's records, and its `bus` record, and runs the shell's `i2c read_byte`
  (8-bit registers, and an `_LSB`/`_MSB` pair as two bytes) or `i2c read` (a wider register, its
  bytes in the `byte_order` stated). `windows` send a register outside the part's own range to the
  address that serves it. A wide register with no `bus` record is refused: the byte order is a
  datasheet fact, not a default. The read-to-clear guard reads the part's own records: `rclr`
  access, or field, register or rule text saying a read clears it. A clear-on-read sentence that
  names another register (`REG0x22`) or field (`STAT_COMP`, an underscore name) is charged to that
  one, not to the field carrying the text (a config bit saying a flag "stays set until read" does
  not clear itself), unless the sentence also names the carrying register; the reason quotes the
  sentence it matched. The shell's `i2c: command not found` answers "no I2C shell: build it with
  `CONFIG_I2C_SHELL=y`" with nothing about the bus (read from the failed read's own answer, no
  extra exchange). A device that does not answer
  gets the state the board's `device list` gives it (`off` when its power domain is suspended).
  A whole part is read when it has at most 24 registers, one shell command each.
  A banked register (`<label>.<REG>@<bank>`) is the record of that bank, read from the address
  alone unless the part's `bus` record states `bank_select` (the select register and a value per
  bank). With it, a read is select, read, restore: `i2c read_byte` of the select register (the
  previous page, once), `i2c write_byte` of the bank's value, the reads (consecutive registers on
  one bank share one select), then `i2c write_byte` of the previous page back, which runs even
  when a read failed; a restore that fails is said in the answer. A page select is a write, so it
  needs `allow_bank_select` (CLI `--allow-bank-select`, MCP `reg_read_bank`, which a host asks
  before running), as `allow_clear` gates a read that clears: without it a single banked register
  is refused naming the opt-in and a whole-part read lists those registers under "Not read". A
  part with no `bank_select` reads as before and the answer says the page was not selected.
  A bank with no value in `bank_select`, or an unreadable select register, writes nothing.
- **Live stacks.** `live_stacks` runs `kernel thread stacks` and joins each thread to its stack
  symbol, the line that defined it and the `*_STACK_SIZE` option (the static `stacks` pairing).
  The join needs the ELF the board runs: 16 words at the start, middle and end of the image's
  largest load segment are read through Core (one halted read) and compared with the ELF; on a
  mismatch the peaks are shown unjoined and the answer asks for the build dir. A thread pairs with
  its stack by K_THREAD_DEFINE's `_k_thread_obj_X` / `_k_thread_stack_X`, else by a name word
  shared by the stack and the thread or the symbol holding the thread object, never by size alone
  (a free stack of the right size is as often the returned main thread's). `!` marks 90 %.
- **Core state.** `core_state` (CLI `live core-state`) asks `embarch-api core-state` (Core decision 97) where
  the core is and answers in symbols. `DHCSR` and `SCB_ICSR` are decoded from their raw words by field
  name (`VECTACTIVE 3 (HardFault)`, `S_LOCKUP`, `PENDSTSET`; bits no table names are shown as hex, so a v8-M
  bit is not silently dropped). The default never halts for the read: the `DWT_PCSR` samples become a
  histogram of functions with file:line; `halt` stops the core once (Core lets it run again) and adds
  PC, LR (resolved as the call, `LR-2`, and not at all when it is an `EXC_RETURN`), SP and `ICSR`. Symbols
  are named only after the same flash check as `live_stacks` (`image_check`: 16 words at three places of
  the image through Core, one halted `mem-read`, taken after the state so the sample is not disturbed); on
  a mismatch, a missing ELF or a probe on another chip the answer says "Refused to name symbols: ..." with
  the reason and leaves addresses raw, and the bit names stay. Resolution uses `nm` (`footprint.image`,
  cached) and, when the toolchain has it beside `nm`, `addr2line` for the exact line; without it the
  function comes from the symbol table with the line its definition starts on, marked as such. No ELF-reading
  dependency was added: the suite already calls the build's own binutils. The check costs a halt the
  non-halting read avoids, so it is skipped when no address needs naming (a halted core, no samples).
- **Wrong chip.** Core's answer names the chip it read (`read['chip']`). Before decoding,
  `reg_read` compares it with the atlas's MCU (the `device` of the SVD document) and refuses on a
  mismatch, naming both chips and the role; case, punctuation and a package or suffix variant are
  tolerated (one name a prefix of the other). The image check raises instead of reporting "another
  build", and `live_stacks` then says the console of the role and its probe are different boards.
  An atlas with no SVD device compares nothing.
- Off unless Core answers: the MCP server asks `embarch-api status` once at start and lists
  `reg_read`, `reg_read_clear`, `shell`, `shell_run`, `live_stacks` and `core_state` only then.

## `atlas.toml`

```toml
[project]
fw = "/abs/path/to/fw-repo"          # the firmware git repo (a process in a linked worktree of it uses that worktree: "Which `fw` a process uses")
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

[[targets]]                          # a target whose PCB has no Zephyr board yet: the stated opt-in
name = "bringup"
no_board = true                      # no `board`, no `app`: `build` skips west and everything that reads its build
schematic_boards = ["A"]

[live]                               # optional: the live module
api = "embarch-api"                  # the embarch-api binary
console = "<signal>"                 # optional: the console signal (default: the role's direct writable one)
consoles = { "<DT uart node, label or path>" = "<signal>" }  # optional: the signal for the node the image's chosen zephyr,shell-uart is
api_config = "<fw>/embarch/embarch.toml"  # its config (this is the default)
role = "dut"                         # the enrolled role reg_read reads from
read_only = ["device list", "kernel uptime"]  # globs the MCP `shell` tool accepts (replaces the default list)
```

An atlas id is `<target>@<commit12>`.

## CLI (`embarch-atlas <verb>`)

| Verb | Module | Does |
|---|---|---|
| `init <fw>` | `project` | writes `atlas.toml` with defaults; wires `CLAUDE.local.md` and `.mcp.json` |
| `status` | `project` | atlases, which is current, which are stale; for an atlas with no build, the outputs absent for that reason and the ones reduced |
| `inventory` | `inventory` | manifest + per-page hashes + diff vs last |
| `schematic [<pdf>] --board <label>` | `smartpdf` | netlist.json, bom.json, nets.md, parts.md |
| `interfaces` | `interfaces` | propose board-to-board mating rules with scores |
| `declare-pin-name <board> <ref> <pin> <name> [--prov llm-unverified \| engineer-declared \| engineer-confirmed] [--note <text>]` | `pinnames` | records a name for a pin the schematic PDF left unnamed in `boards/<board>/pin_names.json` and merges it into that board's `netlist.json`, `nets.md` and `parts.md` (default `--prov llm-unverified`); refuses a board with no netlist, a ref the netlist lacks, a pin number the part lacks, and a pin the PDF's text already named; the same board/ref/pin again replaces its entry |
| `declare-interface <board>:<ref> <board>:<ref> (--rule <rule> \| --pin-map 1:6,..) [--prov engineer-declared \| engineer-confirmed] [--note <text>]` | `interfaces` | records the engineer's answer to which connectors mate as an `interfaces` entry in `boards/system.json`, other entries untouched (the same id is replaced); refuses a ref that is not a connector on that board's netlist, a rule outside identity / mirror / row-swap / column-reverse, a pin map naming a contact the connector lacks. It does not promote a proposal by id |
| `missing [--answer <MPN> supplied \| not-needed --why <text> \| standard-function <fn> --pins <map>]` | `missing` | the proposed matches, then the missing-documents questions from BOM vs manifest, open ones ranked by net_trace walks stopped, then the documents nothing uses; `--answer` records the engineer's answer in `answers.toml` |
| `missing --confirm <MPN> [--file <file>]` / `--confirm-all` / `--reject <MPN> --file <file>` | `missing` | accept or refuse a document for a part, recorded in `answers.toml` (`--confirm` with no file takes the part's strongest proposal; `--confirm-all` the first row of each part and the MCU row). `reference-manual` in place of `<MPN>` answers the MCU row |
| `missing --kind <file> <kind>` | `missing` | override a document's kind, recorded in `answers.toml`; the manifest, and a prepped document's `doc.json`, are rewritten at once |
| `doc prep <pdf>` | `pdftext` | doc.json, pages/, toc, page classes |
| `doc check <doc_id>` | `check` | schema first: a schema-invalid record is reported as `V:` (id and field), stays `llm-unverified` and is kept out of every other check; a line that is not a JSON object is reported as `V:` by file and line and written back as it was; a page cite outside 1..pages of `doc.json` (a record's `p`, a `p.N` in card.md or s/*.md) is reported as `P:` naming the unit and the page and fails, as a wrong-page quote does (the same test `doc freeze` records as `out_of_range`); then checks A–T on the rest (U finds the unit on the cited page as a token, so `ua` inside `guaranteed` is not `µA`, and reads a CamelCase symbol such as `LoadReg` whole rather than by its first letter), writes checks.json, sets `prov` |
| `doc render <doc_id>` | `render` | tier-2 tables and facts.txt from records |
| `doc regparse <doc_id> --template ti\|maxim` | `regparse` | deterministic register records |
| `doc columns <doc_id> <page>...` | `columns` | a table page as rows and cells in reading orientation (sideways tables: rules, else gaps); for reading, never a quote source |
| `doc freeze <doc_id> [--model M]` / `doc thaw-check <doc_id> [--from <old>] [--restore]` | `freeze` | record the LLM-written units in `cache/llm/`; after a new `doc prep`, list which frozen units are still valid and which go back to the LLM |
| `mcu svd <svd> --doc-id <id>` | `svd` | registers/fields as records under the SVD doc |
| `mcu vote --svd-doc <id> --rm-doc <id> [--header <h>] [--periph P]` | `vote` | SVD vs header vs RM: `conflict` / `quirk` (correct omission) / `rule` records under the RM doc |
| `mcu rm-outline <rm>` / `rm-sections <atlas>` / `rm-text <rm> <item>` / `rm-check <rm> [--chapter C]` / `rm-freeze <rm> [--item I \| --sections S \| --retire E]` | `rm` | outline; the worklist for peripherals in use; one work item's text with `[p.N]` markers; verify distilled RM files; freeze them in `cache/llm/` |
| `build <target> [<commit>] [--no-board]` | `build` | worktree, west build, artifacts, stamp; a target with `no_board` writes the stamp alone (below) |
| `kconfig-extract <atlas>` | `build` | writes kconfig.json for an atlas that lacks it, from its build dir: no rebuild, and only while the workspace's Zephyr is the build's |
| `join <atlas>` | `join` | dt.json, in_use.json, pins, checks, mismatches, MAP.md, graph.json |
| `graph <atlas>` | `graph` | graph.json again from what `join` wrote (also run by `join`) |
| `second-look queue <atlas> [--top N] [--diff-from <atlas>]` / `prompt <atlas> <batch>` / `compare <atlas> <readings> -o <verdicts>` / `record <atlas> <verdicts>` | `secondlook` | facts this atlas uses that need a second look, ranked and batched by page (`--diff-from`: only those another atlas's queue did not hold under the same `(id, key)`, as `update` batches; a fact that kept its id but changed a checked field is batched and counted as changed); the blind reader's prompt; the deterministic verdict; record and freeze verdicts |
| (in `join`) | `limits` | DT properties with a unit suffix, in SI, vs the part's limit/timing records: declared (`prop_limits`) or proposed by unit and name, which must also be the same quantity and role (else "mapping uncertain", not compared); abs max = error, recommended = warn, EC = info; a sense-resistor-scaled property is compared only when a record states the formula |
| `codecheck <atlas>` | `codecheck` | driver register constants (addresses, masks, resets, comment claims) vs the datasheet's register/field records, joined by (address, bit range); also run by `join` |
| `diff <atlas> <atlas>` | `diff` | what changed between two atlases |
| `update <target> [commit]` | `update` | bump: stop while a translation is behind its source (below), build, join, RM worklist, a second-look queue whose batches hold only the diff (new or changed facts, told by `(id, key)`; only the previous atlas's unchanged backlog is counted), the diff written into the new atlas |
| `q <lookup>` | `query` | the thirteen lookups (same answers as the MCP tools) |
| `live reg-read <PERIPH[.REG] or LABEL[.REG]> [--allow-clear] [--role R]` | `live` | an MCU register or peripheral (SWD), or an external part's register (`i2c read` on the shell), read on the board through embarch-core and decoded |
| `live core-state [--halt] [--samples N] [--build DIR] [--role R]` | `live` | where the core is: DHCSR and ICSR by field name, PC samples (or with `--halt` PC, LR and the registers) as functions and lines once the flash matches the ELF, else raw with the refusal named |
| `live shell <command> [--role R] [--timeout-ms N]` | `live` | one command on the board's shell through embarch-core |
| `live stacks [--build DIR] [--role R]` | `live` | every thread's stack peak on the board, joined to its stack symbol and option once the flash matches the ELF |
| `mcp` | `mcp_server` | the thirteen lookups as an MCP server over stdio, and `reg_read` when Core answers |

### When `update` stops

`update` stops only for a translation that exists and is behind its source, and names the fix:

- a `docs/<id>` whose `doc.json` hash is not the manifest file's (`doc prep` has not run on the new
  file). The distillation of a manifest file is `docs/<doc_id>`, else the one prepped from the same
  content under another `--doc-id`, else the one prepped from a file of the same name;
- a distillation whose newest freeze has units on pages that changed (`doc thaw-check`). The
  reference manual's units are the worklist's, which `update` writes itself;
- a board whose `netlist.json` was made from another PDF than the one `atlas.toml` names.

A declared board with **no** netlist does not stop it. `update` cannot make the netlist (the schematic
step is the engineer's, from a PDF), so stopping would block every firmware bump on a board that is not
ready, and the next `update` would stop the same way. The atlas says it covers less instead: `join` logs
the board, lists it in the MAP's boards section ("NO netlist"), and records `missing_boards` in
`inputs.json`; `project.atlas_state` turns that into a stale reason, so `status`, the MAP's `> **Stale:**`
line and every lookup's `src:` line carry it until a `join` finds the netlist (`stale.py` is unchanged).
A netlist made from the wrong PDF still stops `update`, because that one is a wrong answer, not an absent one.

A new or changed document with no distillation, of any kind, does not stop it: `update` lists the
documents added, changed or removed since the previous atlas (from that atlas's `source_files`,
else the inventory's own diff) with the distillation each one has, and names the ones without
(notes and spreadsheets have no distillation: an agent reads them as they are).

## What an LLM step may and may not do

- May: read page images and the text layer; write records, prose, the card, the
  section plan; propose invariants (`check` records); propose interface rules.
- May not: write a number that is not on the cited page (untagged), invent a
  citation, edit a deterministic record, delete a failing record, fetch anything.

## Caching

RM worklist units are keyed by section id + page-text hashes + procedure version;
the model goes into the cache entry, so regrouping work items never invalidates
frozen output. Second-look verdicts are frozen as `cache/llm/sl-<key>.json` and
reused by every later atlas whose fact has the same key: a hash over **the fields the second look
checks for that kind** (`secondlook.CHECKED`: a pin's number, name, direction, type, active level,
I/O and power flow; a limit's symbol, condition, values and unit; ...) and the text of the cited
pages. A field the look does not check (a description, a note, a quote) can change without voiding
the verdict; a verdict found under the older whole-record key is aliased to this one on first use.
The compare checks every value the reader is asked for that the key covers: a limit or timing's `cond`
by meaning (`text_value_verdict`: reworded is confirmed, clearly different words or
quantities are a conflict, an ambiguous overlap is unresolved, a reader that states none is silent), a
strap's `condition` by the pin state it names (`pin_states`: the same state, or both saying the address is
fixed, matches; different states conflict; high-impedance, hi-z and high-Z name the floating state, open-drain names none, and a negated word ("not floating") is not counted; an address strap whose value agrees but whose wording names no
state is unresolved as "values agree, wording differs", never a conflict; a mode strap keeps
`text_value_verdict`), a
register's `width` numerically. This tightening did **not** bump `CHECKED_VERSION`: verdicts already
frozen under a current key stay frozen, including a `confirmed` that never looked at the condition or
width, because a bump re-reads every fact in every atlas. They are corrected when the fact's checked
fields change, or by a deliberate `CHECKED_VERSION` bump if a full re-look is wanted. When several entries share a
unit key, the one whose recorded file hashes match the files on disk wins, then the newest; a
newer freeze of the same files marks the older entry `superseded` (kept, never deleted).

## Document kinds and coverage

### Kinds

`inventory.KINDS`: `schematic`, `datasheet`, `reference-manual`, `errata`, `appnote`, `prd`, `spreadsheet`,
`notes`, `other`. The classifier reads the PDF's metadata and first three pages:

- **`schematic`** needs real evidence (`project.schematic_evidence`, the one test, for `inventory` and for
  `init`'s board hint): an EDA tool named as a word in the Creator or Producer, `.SchDoc` in the text, or net
  information in the outline. `Acrobat Web Capture` and `Allegro MicroSystems` are not EDA tools.
- **`appnote`**: `application note`, `application report`, `app note`, `user guide`, `technical note` or `tech note` in the title,
  the file name or the top of page 1, or an ST `AN`/`UM`/`TN` document number. It is tested before the datasheet
  rule, and before a "reference manual" mentioned in the first page's text, which a note may well cite; a
  title saying reference manual comes first, and so does a user's guide that is one (below). What a document calls itself beats what its first page cites: a
  title or file name saying datasheet, or a first page saying so before it names a note, keeps it out of this
  kind, since a datasheet's front page often lists its companion note. An app
  note may name parts and never by itself covers one: where a token or a rung finds one it is **supporting
  material**, shown beside the part in `missing.md` and `MAP.md`, and the join reads no pin record or limit from
  it. Its translation uses the datasheet procedure, and its card says which part it supports. `--confirm` refuses
  one and names the way: `--kind <file> datasheet`, if the kind is wrong.
  **A user's guide that is a reference manual is `reference-manual`**: "user's guide" is the only note wording
  it has (no application note or report, no technical note, no `AN`/`UM`/`TN` number) and a line at the top of
  page 1, at most 80 characters and not citing one ("see", "refer", "in the" ...), calls it a reference manual.
  Chosen because a kit's plain user's guide often cites the MCU's manual on page 1, but as a sentence, not as
  its own title line; a citing line keeps it `appnote`. Tested before the appnote rule
  (`inventory.guide_is_reference_manual`).
- **A `.docx` or `.doc`** whose name or title says PRD or requirements is `prd` with `needs_export: true`, and
  `missing.md` asks for it as a PDF. The tool does not convert it. Any other Word file is `other`. Once a PDF of
  the same stem is in the sources folder the Word file is `superseded_by` it and the question goes away.

`--kind <file> <kind>` overrides the classifier, in `[documents."<sha256[:12]>"]`. It applies at inventory
time, `manifest.md` shows it beside the kind, and it ends when the file's content changes. The manifest stays
byte-identical across runs with an override present. `missing --kind` (and `--confirm reference-manual`) also
rewrites `kind` in the `docs/<doc_id>/doc.json` of an already-prepped document with the same sha256 and says so;
a document not prepped yet gets the manifest's kind from `doc prep`.

### Covered, proposed, missing

`coverage.py` is the one reader: `missing`, the join and what the join feeds (`net_trace`, `pins.json`,
`MAP.md`, the graph) all ask it which document covers an MPN. A part is **covered** by a document whose part
tokens match its MPN, or by one the engineer confirmed, or, when neither holds, by a rung that says covered. It
is **proposed** when only a weaker rung ties a document to it, and **missing** with none. A proposal is one row
of the ✓/✕ table that opens `missing.md`, so a file already supplied is confirmed, not asked for again.

**A part named in passing is not covered.** A datasheet names its own part in its title, keywords, subject or on
page 1; a comparison table or a companion-part sentence on page 2 names another. So a document's part tokens
(`inventory.detect_ids`, up to `PART_LIMIT`) are read from those and page 1 only (`FRONT_PAGES = 1`), and `exact`
covers on page 1 or an ordering-information page alone. A part named anywhere later is at most proposed, a row to
confirm; `missing --reject` stays for a mention that still lands on page 1. Chosen over keeping pages 1-3, which
silently stopped asking for the datasheet of any part a neighbour's page 2 mentioned. Measured only on synthetic
fixtures: a real datasheet whose own part first appears on page 2 (a long cover page) is proposed, not covered.
A manifest written before this keeps its old tokens until `inventory` runs again.

Part tokens (`family_regex`) require two digits on purpose, so a bare vendor family never covers every part, and
that means they cannot read every shape: `QRS7TUV`, `88LOG2G11`, a mixed-case `abC54X20`. The rungs go the other
way round. The BOM's MPNs are known, so each is looked for in the supplied documents (kinds `datasheet`,
`reference-manual`, `other`; not a superseded or duplicate file). Strongest first:

| Rung | Matches | Counts as |
|---|---|---|
| `exact` | the normalised MPN as a whole token in the text, on page 1 or an ordering-information page | covered |
| `link` | the schematic's datasheet-link parameter: its URL basename is the file's name, ignoring case and query, or it equals the file's Zone.Identifier HostUrl | covered |
| `exact` | the normalised MPN anywhere else | proposed: a datasheet can name another part in a typical-application note |
| `stem` | a stem of the MPN (packaging cut after `,` `+` `/` `#`, then trailing characters one at a time, down to 5, holding a letter and a digit), as a whole token or token prefix, ignoring case, in the file name, the PDF title or pages 1-2 | proposed: a stem also matches a sibling part |
| `series` | the title or page 1 says `<prefix> Series` or `Series <prefix>`, and the MPN starts with that prefix | proposed |

An MPN with no digit, or under 5 characters, has no `exact`, `stem` or `series`: `BARREL` on a page says nothing.
Each result is `{mpn, file, rung, evidence}`, `evidence` saying what matched and where. Rungs run only for a part
no token or confirmation covers; they never add a second document to a part that has one.

**What the join reads pin records from is narrower than what stops a question**, because a wrong document there
is a wrong fact and not a question left unasked. `part_docs` holds the distillations whose own identity matches
(`part` and card `aka`), the distillation of a confirmed document, and, only when those give nothing, the
distillation of a covering `exact` or `link`. A part token in the manifest alone never gives a pin map: it can be
a mention on page 2. A part covered by a file with no distillation that speaks for it is `part_supplied`:
`net_trace` lists it as "supplied, not translated yet", not under "Datasheets needed", and `MAP.md` apart from the
parts with no document. `walk_stops` does not count it.

**Confirmed and rejected** documents are tied to the file's sha256. A rejection takes a document off a part
whichever way it got there, in `missing` and in the join, and holds until the content changes; a rename changes
nothing. A confirmed file whose content changed goes back to proposed, with evidence saying so; a confirmed
file that is gone is asked for again. Answers apply over the stored rung results, so a confirmation never
rescans. The join picks them up at the next `join`, and `status` says the atlas is stale until then; `net_trace`
reads them at once.

Page text comes from `inventory/text/`, written by `inventory` (or read once from the file, for a manifest older
than the cache); `missing` runs no poppler over a document it has. Rung results are kept in `inventory/matches.json`.
Neither is in the manifest.

### The MCU's own documents

`[mcu] part` is matched as a token or token prefix, ignoring case, against a document's title, file name and
pages 1-2. A `reference-manual` or `errata` document that names the MCU this way is the MCU's, whatever its part
tokens say. Some vendors publish one datasheet with the full register documentation and no separate manual, so
when there is no reference manual and a `datasheet` or `other` document of 300 pages or more names the MCU, the
reference-manual question becomes a row of the table: `<file> serves as the reference manual`. Confirming it sets
the document's kind through the override and rewrites the manifest; its doc_id does not move. When `[mcu] ref`
names a module whose MPN differs from the SoC, the module's own datasheet question stays open.

### Documents nothing uses

`missing.md` closes with `## Supplied documents nothing uses`: documents covering no BOM part (passives included)
and no MCU role, proposed for none, supporting none. A schematic, PRD, spreadsheet or notes file has its own role
and is left out, and so is a superseded or duplicate file. This is how a wrong file or a mis-typed document shows
up. Not measured on a real board in this unit (synthetic fixtures only).

## Answers to missing.md and generic function cards

`inventory/answers.toml` holds one table per part number (`[parts."<MPN>"]`), written by
`embarch-atlas missing --answer <MPN> ...`, `--confirm` and `--reject` (which rewrite the file, dropping
hand-written comments) and read by `missing` and the join. `answer` is one of:

- `document` with a `file`, a `sha256` and the `rung` that proposed it (`engineer` when none did), from `--confirm`;
- `supplied`: the engineer will put the file in the sources folder; `missing.md` lists it as answered
  until the manifest covers the part (then it is simply covered) or a rung proposes a file for it;
- `not-needed` with a `why` (required);
- `standard-function` with a `function` and a `pins` map from the part's pin number or netlist pin name
  to the card's pin (`{ "1" = "G", "2" = "S", "3" = "D" }`; several part pins may name one card pin, a
  MOSFET's drain tab). The map must cover every non-optional pin of the card and is checked against the
  pins of the parts that carry the MPN. A malformed file is reported (`missing` and `inventory` exit 2;
  `net_trace` says it ignored the file) and never half-applied.

A table may also hold `rejected = [{ file, sha256 }, ...]`, from `--reject`, with or without an `answer`. A
`not-needed` or `standard-function` part gets no proposal: the engineer decided.

`[documents."<sha256[:12]>"]` holds what the engineer said about one document: `file`, `kind` (the override) and
`not_reference_manual = true` (`--reject reference-manual`). Keyed by content, each ends when the file changes.

A **generic card** is a function template in `answers.CARDS`: n-mosfet, p-mosfet, diode, schottky, tvs,
thermistor, inverter, and, nand, buffer-oe, decoder-1of2. Each pin has `dir`, `type` and, where current or
a signal passes, `flow` (a MOSFET's drain and source are `bidir`, its gate a `sink` marked `control`;
logic inputs `sink`, outputs `source`; a TVS or thermistor has none). The join turns a card into
datasheet-shaped pin records with `prov = engineer-declared`. A card states **no limit, timing or part
fact** and is used only for an MPN the engineer answered with `standard-function`, only through that pin
map (a function or pin map is never read from netlist names, decision 2026-10-06), and never over a
datasheet: a part that gains a document stops using its card.

`net_trace` treats a card's pin like a datasheet pin: a generic feed (a FET's channel, a diode's cathode,
a gate's output) is walked and its line ends `[generic card, engineer-declared]`; a `control` pin is the
part's enable, and a net driven by a generic logic output reads `driven by <part> Y [generic card,
engineer-declared] (input A on <net>: MCU PA3 ...)`, chained up to three gates deep. An answered part is
not under "Datasheets needed".

`missing.md` ranks open questions by **walks stopped** (`nettrace.walk_stops`: one upstream walk per
non-ground rail net, each MPN counted once per walk, a documented or answered part never counted), then by
firmware visibility, and shows the count (`Stops N net_trace walks`). Answered items move to an
"Answered" section. Its summary line counts covered, proposed and missing part numbers apart. The count is a ranking aid over rail walks, not a coverage figure: a part reached only
from a signal net is not counted. Not measured on a real board in this unit (a synthetic two-board
fixture only).

## Not yet built (deliberately)

A native EDA adapter, geometry as a fallback (without anchor
words) and the vision rung of the schematic ladder (automatic crop and read: today the skill renders a symbol's
crop and reads it, and `declare-pin-name` records the result as `llm-unverified`), PRD → code trace links.

Reading a board part's package from the BOM's footprint string, or from `variant` records, to fill
`bindings.json` `packages` (today the engineer states it).
