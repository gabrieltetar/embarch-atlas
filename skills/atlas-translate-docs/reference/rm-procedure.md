# Distil an MCU reference manual (RM)

An RM is too big to distil whole, and most of it is already structured elsewhere. So:

- **Registers come from the SVD**, never from RM prose. `mcu svd` writes them; `mcu vote` checks
  them against the RM's register sections and the CMSIS header and keeps every disagreement as a
  `conflict` record.
- **Field constraints come from the RM register sections, deterministically.** `mcu vote` lifts every
  "only when", "must", "cleared by hardware", "Note: ..." sentence verbatim into `rule` records keyed
  by SVD register and field names, and renders them to `s/<chapter>.fields.md`. You do not rewrite them.
- **You distil the prose of the sections in use**, lazily: only the items in `docs/<rm>/worklist.json`
  that have no frozen distillation yet. atlas-build writes the worklist from the build (in use =
  instantiated in the ELF, or touched by SoC code, plus the fixed cross-cutting core); a peripheral
  that is only DT-okay is a mismatch item, not in use, and is not in the worklist.
- **The per-page text layer is kept whole** (`pages/p<NNN>.txt`). Nothing is distilled from it that
  the worklist does not ask for.

Output, all under `docs/<rm_doc_id>/`:

| File | Who writes it |
|---|---|
| `doc.json`, `pages/`, `toc.md` | `doc prep` |
| `outline.json` | `mcu rm-outline`: sections, page ranges, token costs, register-section flags |
| `records/conflict.jsonl`, `records/quirk.jsonl` (class `variant`), `records/rule.jsonl`, `s/<ch>.fields.md`, `vote.md` | `mcu vote` (SVD vs RM vs header; each record cites an RM page and `refs` the SVD ids) |
| `worklist.json` | `mcu rm-sections <atlas>` (atlas-build step 4) |
| `s/<item>.md` | you: one tier-2 file per work item, at most 4K tokens |
| `s/<ch>.card.md` | you: the chapter card, at most 1.5K tokens |
| `s/<ch>.must.md` | you: programming sequences and must-notes for the chapter |
| `rm_checks.json` | `mcu rm-check` |
| `cache/llm/<key>.json` (atlas root) | `mcu rm-freeze` |

The distillation is **board-independent**: no net names, no pin choices, no clock frequencies a
board happens to run, no "this build does not use X". Write what the RM says for the part.

## Hard rules

1. **Never fetch anything.** A document you need and do not have (errata sheet, programming manual
   for the core) is a question for the engineer.
2. **Register and field names are SVD names**, written in backticks: `` `REG.FIELD` `` for the
   chapter's own peripheral, `` `PERIPH.REG.FIELD` `` for another block's (a clock-enable bit, a
   remap bit). `mcu rm-check` fails any backticked name the SVD does not have. Where the RM and the
   SVD name a bit differently, use the SVD name and say so; the `conflict` record already holds both.
3. **Every line that states a fact cites** `[§<section> p.<page>]` (or `p.<a>-<b>`): the section as
   the RM numbers it, the 1-based physical PDF page. One cite per source page; a heading's cite does
   not cover its lines.
4. **Every number is on a page the line cites**, or the line is tagged `(derived)`, `(figure)` or
   `(inference)`. A value read from the SVD, not the RM, is cited `[SVD]` and still has to be true
   of the SVD.
5. **Device filter.** The RM describes a whole family. Before writing a feature, check it exists on
   this part: `docs/<rm>/availability.json` (the RM's peripherals-versus-devices table), the SVD
   (is the peripheral, register or bit there?) and the RM's own "only available in <parts>" /
   "reserved in <parts>" notes. A family feature the part lacks is written once, as absent, or not at all.
6. **Behaviour is only what the RM states.** Driver code is compliance evidence, not a source.
7. **Never edit a deterministic record** (`rule` records from `mcu vote`, ids `<rm>.rule.reg.*`), and
   never delete a record or line for failing a check. Fix it, or keep it and mark it `(unverified)`.

## Steps

```
- [ ] 1 prereqs   docs/<rm>/outline.json (mcu rm-outline), the SVD doc (mcu svd), mcu vote run
- [ ] 2 worklist  read docs/<rm>/worklist.json: items with status todo, per chapter
- [ ] 3 read      per item: embarch-atlas mcu rm-text <rm> <item>; image Reads only for figures and
                  tables the text layer mangles
- [ ] 4 write     s/<item>.md
- [ ] 5 chapter   s/<ch>.card.md and s/<ch>.must.md once the chapter's items are written (or update them)
- [ ] 6 check     embarch-atlas mcu rm-check <rm> --chapter <ch>; fix; repeat
- [ ] 7 freeze    embarch-atlas mcu rm-freeze <rm> --item <item> --model <model>   (and the card/must files)
- [ ] 8 recheck   embarch-atlas mcu rm-sections <atlas>: the items you did now show frozen
```

### 2. The worklist

`worklist.json` lists, per chapter in scope: why it is in scope (`UART1: instantiated`, `RCC: core`),
its page range and tokens, and its **units**: the sections to distil, each with its pages and a
content `key`. A section larger than about 6K tokens is split into its own text (`intro: true`) and
its subsections. Register sections the SVD covers are in `excluded`, with the reason. `items`
batches consecutive to-do units into reading sessions of about 4K tokens; an item's output file is
`s/<item id>.md` (for example `s/2.4.3-2.4.6.md`).

Take items in chapter order. A chapter whose card already exists only needs its new items and a
card update.

### 3. Read

`mcu rm-text <rm> <item>` prints the item's text, cut at its first heading and at the next
section's heading, with a `[p.N]` line at every page change. That is the page you cite. It is the
layout text layer: tables come out as columns, and a rotated or dense table can come out scrambled.
For those, and for every figure you need (block diagrams, timing diagrams, flowcharts), Read the page
image (`Read` with `pages=N`, about 1.3K tokens a page). A fact taken from a picture is tagged
`(figure)`.

### 4. Section files (`s/<item>.md`)

```markdown
# <Block>: <topic> (<rm doc id> §<first>-§<last>, pp.<a>-<b>)

Tier 2, distilled. Register and field names are SVD names.

## <Sub-topic> [§<sec> p.<a>-<b>]
- <one fact per line> [§<sec> p.<N>]
```

- One fact per line; a line never mixes two sections' facts without citing both.
- Keep formulas as printed (`tSCL = (SCLH + 1) x tPRESC`), with the RM's symbols.
- Tables: keep the rows you need as lines, symbol and unit on every row; point to the table by its
  number for the rest ("Table 12 lists 8, 16 and 48 MHz examples").
- Name the RM's modes, flags and sequences the way the RM does, and add the SVD name.
- Mark what the RM says a user must or must not do as **must** / **must not**; those lines are
  candidates for the must-notes file.

### 5. Chapter card and must-notes

`s/<ch>.card.md`, at most 1.5K tokens, loads first when an agent touches the block:

- what the block is, in two lines; the instances on this part with base address and IRQ (`[SVD]`),
  and per-instance differences (feature matrix rows, from the RM's implementation table);
- clock and reset: the RCC enable/reset bits and the kernel-clock mux, as `PERIPH.REG.FIELD`;
- the register list (names and offsets `[SVD]`), plus the vote's result for the block (how many
  conflicts, which ones matter);
- **five things to know before touching it**, each cited;
- a section index: section, pages, raw tokens, which `s/` file distils it, or "not distilled yet".

`s/<ch>.must.md` holds what an agent must not get wrong:

- **programming sequences** as numbered steps (init, reset, the transfer flows, recovery), from the
  RM's flowcharts and prose, each step cited;
- **must-notes**: the RM's "must", "only when", "not allowed" statements that matter across
  registers (field-level ones are already in `s/<ch>.fields.md`; do not copy them, point to them);
- **quirks**, each checked against a second source (another RM section, the SVD, the header, the
  `conflict` records): the RM contradicting itself, a mux named after another block, an interrupt
  line shared between instances, vocabulary that differs between RM and SVD (search both). A quirk
  also goes into `records/quirk.jsonl` as a record with `p` and a verbatim `q`, id
  `<rm slug>.quirk.<chapter>.<slug>`.

### 6. Check (`mcu rm-check`)

Deterministic, a few seconds per chapter. On every line of `s/*.md` (the generated `*.fields.md`
excepted) it checks:

1. every backticked `REG.FIELD`, `PERIPH.REG` or `PERIPH.REG.FIELD` exists in the SVD;
2. every field named on a cited line appears in the text of a cited page (names of another block's
   register, written `PERIPH.REG.FIELD`, are exempt: they are facts about another chapter);
3. every `[§S p.N]` has page N inside section S (`outline.json`);
4. every number on a cited line appears on a cited page, unless the line is tagged. 0-3 are not tested.

It exits non-zero while anything is flagged and writes `rm_checks.json`. Typical flags and fixes:
a cite to the section's first page when the fact is two pages on (fix the page); a field the RM
does not name on that page (the SVD named it: cite `[SVD]`, or drop the name); a number restated
from a bit range or computed (tag it `(derived)`); a claim the RM does not make (delete the claim,
not the line's other facts). The checker proves that names, pages and numbers line up, not that a
sentence is true; that is the second look's job, in atlas-build.

### 7. Freeze

```
embarch-atlas mcu rm-freeze <rm> --item <item> --model <model id>
embarch-atlas mcu rm-freeze <rm> --sections <s1,s2,...> --files s/<ch>.card.md --model <model id>
```

`rm-freeze` recomputes each section's key (the section id plus the hashes of its pages' text) and
stores the files' hashes and the last check result in `cache/llm/<key>.json`. From then on the
worklist shows those sections `frozen` in every atlas whose RM pages are unchanged, whatever the
build. A new RM revision, or a changed page, gives new keys and the sections come back as to-do.
A frozen file edited by hand shows `frozen-files-changed`: re-check and re-freeze it.

**Do not re-distil a frozen section** unless its status says the pages or files changed. A re-run
re-checks; it does not re-ask the model.

## Conventions added 2026-09-30

- `rm-text <rm> <item>` prints every page of the item's range and fails loudly if one would be
  lost; trust it, do not re-read pages around it.
- Section files are tier 2 (the card is tier 1).
- Freeze a chapter's card and must file with `--sections <chapter>`; section files with their unit
  ids. A must file has the same 4K-token cap as a section file.
- A newer freeze of the same files supersedes the older entry automatically (kept, never deleted);
  `rm-freeze --retire <entry>` marks one superseded by hand. `rm-sections` lists stale entries.
- Numbers: a tag covers only its own clause (as in `doc check` M); an `[SVD]` cite covers the
  numbers and the field names in its clause. A per-instance field (`TEIF1`) is found on the page in
  the RM's generic form (`TEIFx`), so it needs no extra register-section cite. List markers, `Tier N`, token counts, section references and backticked
  identifiers are not numeric claims. Write ordinals as the page does ("eighth", not "8th").
- A quirk written into a must file also gets a `quirk` record, so the mismatch report and
  `find_fact` see it.
- After `rm-sections` regroups the worklist, re-freeze frozen work with `--sections`, not `--item`.
