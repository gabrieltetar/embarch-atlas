# Distil one datasheet

Output, all under `docs/<doc_id>/` (layout: `DESIGN.md`):

| File | Who writes it |
|---|---|
| `doc.json`, `pages/`, `toc.json`, `toc.md` | `doc prep` |
| `records/regparse.jsonl` | `doc regparse`, when the register map follows a known template |
| `records/<kind>.jsonl` | you: the facts, one JSON object per line |
| `s/<secid>.md` | you: prose and headings, with table markers; `doc render` fills the tables |
| `card.md` | you: tier 1, at most 1.5K tokens |
| `facts.txt`, the tables inside `s/` | `doc render` |
| `checks.json`, `prov` on every record | `doc check` |

A datasheet distillation is **board-independent**. It never names a net, a reference designator,
which pins a board uses, or which variant or strap value a board fits. Keep every orderable variant.
The schematic analysis joins the part to the board later, by MPN.

## Hard rules

1. **Never fetch anything.** Not the datasheet, not an errata sheet, not an application note. A
   document you need and do not have is a question for the engineer (`inventory/missing.md`).
2. **Every record carries `p` and `q`.** `p` is the 1-based physical PDF page, the number
   `Read(pages=N)` takes, never the printed page label. `q` is a verbatim quote from that page's text
   layer. For a table row, quote the numbers line (the line with the values), not the
   parameter-name line. A quote may skip text with `...` between two verbatim parts.
3. **A fact that exists only in a picture** gets `src: figure` (or `src: equation`) and `q: null`, or
   the caption as `q`. It stays `llm-unverified` until a second look confirms it. A caption or a
   label that is raster (the page's text layer does not hold it) is a picture too: tag its numbers
   `(figure)`; citing the page cannot anchor them.
4. **Keep the datasheet's units and spelling.** Write `0.22 kΩ`, not `220 Ω`. A converted value is a
   derivation, so tag it. Keep footnote markers out of values: in `1.0A17`, the 17 is footnote 17.
5. **Tag every number the cited page does not literally hold:** `(derived)` for arithmetic,
   `(figure)` for a value read off an image, `(inference)` for reasoning. The tag goes on the same
   line as the number. Untagged numbers must be on a page the line cites.
6. **Cite in machine form:** `p.8`, `p.4-5`, `p.9, p.20`. A heading's cite covers its lines.
   `§7.3` is a section, not a page.
7. **Behaviour is only what the document states.** Never infer device behaviour from firmware, from
   another part, or from what a sensible design would do.
8. **Never edit a deterministic record** (`records/regparse.jsonl`, anything with
   `prov: deterministic*`). **Never delete a record for failing a check.** Fix it, or leave it
   `llm-unverified` with the reason in `note`.
9. **Never invent a citation.** A fact you cannot place on a page is not written.

## Steps

```
- [ ] 1 prep      embarch-atlas doc prep <pdf>
- [ ] 2 plan      read toc.md: sections, token sizes, the pages that need an image Read
- [ ] 3 registers embarch-atlas doc regparse <doc_id> --template <t>   (only if the template matches)
- [ ] 4 read      text layer first, section by section; image Reads only for the flagged pages
- [ ] 5 records   records/<kind>.jsonl
- [ ] 6 sections  s/<secid>.md: prose + table markers
- [ ] 7 card      card.md <= 1.5K tokens
- [ ] 8 render    embarch-atlas doc render <doc_id>
- [ ] 9 check     embarch-atlas doc check <doc_id>; fix; repeat until it exits 0 or only
                  llm-unverified items with a stated reason remain
- [ ] 10 freeze   embarch-atlas doc freeze <doc_id> --model <model>   (so a later revision re-asks only changed pages)
```

### 1-2. Prep and plan

`doc prep` writes the text layer of every page (`pages/p<NNN>.txt`, `pdftotext -layout`), the
outline as sections with page ranges and token counts, and a class for every page. `toc.md` has
three parts:

- **the outline**: section id (the document's numbering, or a title slug when it has none), title,
  pages, tier-2 file, tokens;
- **pages that need an image Read**: `image-heavy` (little or no text layer: raster figures, scans),
  `figure` (a figure caption over graphics), `equation` (a math font, whose text layer is glyph
  soup);
- **page classes**: also `table-heavy`, `register`, `pua-glyphs` (Symbol-font characters that
  `Read` shows as nothing) and `overline` (overlined names; the "Overlined names" table lists
  them, and the pages where a name also appears without the bar are in the image-Read list);
- **synthetic entries**: `front-matter` (contents, lists) and `unlisted` (an index, a notice) for
  pages no outline entry covers. Do not distil them.

Plan the reading from it: which sections to read in which order, how many image Reads (about 1.5K
tokens a page), and which s/ files you will write. Datasheets with raster figures need many image
Reads; a datasheet with vector figures and a clean text layer may need one crop of its equations.

### 3. Registers

If the register map follows a template that `doc regparse` knows (`--template ti`: headings
`x.y.z NAME Register (Address = ...) [reset = ...]`, bit diagrams, `Bit Field Type Reset
Description` tables, a summary table), run it first. It writes `register`, `field` and `conflict`
records with `prov: deterministic`, and reports where the document disagrees with itself (bit
diagram vs field table, heading vs field resets, heading vs summary table).

Then write your own `register` and `field` records **from the page, not from `regparse.jsonl`**,
with the same ids. They are the independent twin: `doc check` compares the two (G), and agreement
turns the deterministic record into `deterministic+crosschecked`. Your records add what a parser
cannot: meaning (`desc`, `enum`), mode dependence, write order, side effects.

With no template, your records are the only extraction; `doc check` still checks widths, overlap,
reset composition and unique addresses. Templates: `ti` (register-description headings, bit
diagrams, `Bit Field Type Reset Description` tables) and `maxim` (`Name Register (NNNh)` headings,
D15..D0 bit diagrams, grid summary tables). **If `doc regparse` finds 0 registers it exits non-zero
and prints the patterns it tried**: the template does not fit this document. Continue with your own
records as the only extraction, expect G to report no twin, and say "template mismatch" in the
report so the template can be extended.

### 4. Read

- **Text layer first.** Read `pages/p<NNN>.txt` section by section, at most about 1,100 lines per
  Read. It holds every printed number exactly; it is also what the checker compares against.
- **Image Reads only where the text layer fails:** the pages `toc.md` flags, and a figure,
  timing diagram, block diagram or equation that a fact depends on. `Read(pages=N)` on the PDF, or
  render one page (`pdftoppm -r 100 -f N -l N -png`) and Read the PNG. Stack several equation crops
  into one image.
- Note while reading: footnotes and which rows they qualify, conditions that override the table
  header, and every quirk candidate (checklist below).

### 5. Records

One JSON object per line, in `records/<kind>.jsonl`. Schema: `schemas/record.schema.json`; unknown
fields are errors. Common fields: `id`, `kind`, `doc`, `p`, `q`, `src` (`text` | `table` | `figure`
| `equation`), `note`. Leave `prov` out: `doc check` sets it.

Ids are native names, never ordinals, so they survive a revision that moves pages:

| kind | id | example |
|---|---|---|
| limit | `<part>.<group>.<sym>[.<cond>]` | `demo-switch.ec.VOVP.vbus_rising` |
| timing | `<part>.timing.<sym>[.<cond>]` | `demo-switch.timing.tDEB.fon0` |
| pin | `<part>.pin.<NAME>` | `demo-switch.pin.FLT` |
| rule | `<part>.rule.<slug>` | `demo-switch.rule.otp` |
| register / field | `<part>.reg.<NAME>[.<FIELD>]` | `demo-conv.reg.MODE.OE` |
| quirk | `<part>.quirk.<slug>` | `demo-switch.quirk.ovp_min_below_operating_max` |
| figure | `<part>.fig.<slug>` | `demo-switch.fig.figure-1` |
| strap | `<part>.strap.<PIN>.<slug>` | `demo-conv.strap.MODE.r_6k19` |
| check | `<part>.check.<slug>` | `demo-switch.check.thermal_pd` |
| conflict | `<part>.conflict.<slug>` | `demo-conv.conflict.fscl_min` |
| bus | `<part>.bus.<iface>` | `demo-gauge.bus.i2c` |

`<part>` is the part of `doc_id` before the `@`.

```jsonl
{"id": "demo-switch.ec.VOVP.vbus_rising", "kind": "limit", "doc": "demo-switch@A", "p": 8, "src": "table", "q": "VBUS rising threshold 22 23 25 V", "group": "ec", "sym": "VOVP", "param": "internally fixed OVP", "cond": "VBUS rising", "min": 22, "typ": 23, "max": 25, "unit": "V", "fn": [24], "guar": "design"}
{"id": "demo-switch.timing.tDEB.fon0", "kind": "timing", "doc": "demo-switch@A", "p": 7, "src": "table", "q": "FON = 0 10 16 25", "group": "ec", "sym": "tDEB", "cond": "FON = 0", "min": 10, "typ": 16, "max": 25, "unit": "ms", "edge": "EN valid -> output starts to rise"}
{"id": "demo-switch.pin.FLT", "kind": "pin", "doc": "demo-switch@A", "p": 3, "src": "table", "q": "Fault output, active-low, open-drain", "pin": "D2", "name": "FLT", "dir": "out", "type": "od", "active": "low", "desc": "any fault; held low during soft-start"}
{"id": "demo-switch.rule.otp", "kind": "rule", "doc": "demo-switch@A", "p": 21, "src": "text", "q": "When the junction temperature exceeds 150°C, the switch turns off.", "cond": "TJ > 150 C", "text": "switch off (fault)", "recover": "TJ < 130 C, then soft-start"}
{"id": "demo-conv.reg.MODE.OE", "kind": "field", "doc": "demo-conv@B", "p": 32, "src": "table", "q": "7 OE R/W 0b Output enable", "reg": "demo-conv.reg.MODE", "name": "OE", "bits": [7, 7], "reset": "0b0", "access": "rw", "enum": {"0": "output off", "1": "output on"}}
{"id": "demo-switch.quirk.ovp_min_below_operating_max", "kind": "quirk", "doc": "demo-switch@A", "p": [5, 8], "src": "table", "q": ["Operating Voltage 3 to 23 V", "VBUS rising threshold 22 23 25 V"], "class": "table-vs-table", "text": "VOVP min is 22 V, inside the 3..23 V operating range.", "why": "A part at the low corner faults inside its own operating range.", "refs": ["demo-switch.recommended.VIN", "demo-switch.ec.VOVP.vbus_rising"]}
{"id": "demo-switch.check.ovp_vs_operating", "kind": "check", "doc": "demo-switch@A", "p": [5, 8], "src": "text", "expr": "R['demo-switch.ec.VOVP.vbus_rising'].min >= R['demo-switch.recommended.VIN'].max", "expect": "fail", "why": "the quirk above; must stay failing or the quirk is stale"}
```

Field notes:

- **limit / timing**: one record per table row **and** condition. `group` is `abs`, `esd`,
  `thermal`, `recommended`, `ec`, `i2c`, `register`, `application` or `other`. `src: table` for a
  value in a MIN/TYP/MAX table (the column check K runs on it), `src: text` for one in prose. A value
  printed as an expression (`VBUS`, `SW1+6`) stays a string. `±2` is `min: -2, max: 2`. `guar:
  design` when a footnote says "not production tested". A second value column (an EVM or a second
  package) is a second record with that column in `cond`.
- **register / field**: bus-addressable bytes; a logical register over two addresses is
  `<NAME>_LSB` / `<NAME>_MSB`, and a split field `<FIELD>_<hi>_<lo>` (absolute bits). `reset` in
  binary (`0b...`, the field's width) or hex (`0x...`), `null` when undefined. `access` in SystemRDL
  terms: `rw`, `ro`, `wo`, `rclr` (clears on read), `woclr`, `w1s`.
- **quirk**: a sentence that takes back a general statement nearby ("no restrictions" followed by
  an exception), and any condition under which a read or an access silently returns a wrong value
  instead of failing, are always quirks: the facts an agent debugging hardware needs and will not
  think to look up. `class` is one of `table-vs-table`, `text-vs-table`, `text-vs-figure`, `figure-only`,
  `footnote-condition`, `silent-default`, `must`, `do-not`, `absence`, `variant`, `doc-artefact`,
  `not-prod-tested`, `narrow-condition`, `provenance`. `refs` point at the records it compares; the
  checker resolves them.
- **check**: an invariant you propose, which the checker evaluates: `R['<id>'].min/.nom/.typ/.max`,
  arithmetic, comparisons, `abs/min/max/all/any/round/sum/len`, comprehensions. Nothing else runs.
  `expect: fail` records a known inconsistency of the document; the quirk it backs should say so.
- **pin**: every power or supply pin gets `flow`, which is what lets `net_trace` name the part
  that feeds a rail. `source`: the row says output (regulator, LDO, switch output). `sink`: input or
  supply. `bidir`: the part moves power either way by mode (a bidirectional switch; cite the page
  that says so). `sense`: the pin only measures the rail. `local`: a node of the part's own (switch
  node, bootstrap, an internal regulator bypassed to a capacitor). A row that says none of these
  gets no `flow`; never infer one from the pin's name. Ground and signal pins get none.
- **strap**: address or mode straps (a resistor table on a MODE pin): `pin`, `condition`, `value`.
- **bus**: one per serial interface with registers wider than a byte, or with more than one bus
  address: `iface` (`i2c`, `smbus`, `spi`), `byte_order` of a multi-byte register on the bus
  (`lsb-first` or `msb-first`, from the sentence or the frame figure that shows it),
  `reg_addr_bytes` (pointer bytes before the data), and, when the part answers at more than one
  address, `windows`: every register range with the 7-bit address that serves it (`from`, `to`,
  `addr`, `base` when the byte sent is not `register - from`). Convert a printed 8-bit address and
  say so in `note`. The live module reads registers with it, so a wrong byte order decodes every
  wide register wrong; a page that does not state the order gets no record.
- **rule** with `steps`: `q` is a list in step order, one verbatim quote per step that has one, and
  `p` every page a step came from. Numbers in `steps` and `text` are not column-checked, so each must
  be on a cited page or tagged.
- **figure**: an index of figures and equations that facts depend on: `fig`, `caption`, `what` (what
  you read in it). Say in `note` how you saw it (`read: pdf page`, `read: png 100 dpi`, `not read`).

### 6. Sections (tier 2)

One `s/<secid>.md` per section or group of sections, at most 4K tokens each; split larger ones. The
file name is the section id from `toc.md`. A file that covers several outline sections says so, and
`toc.md`'s tier-2 column follows:

```markdown
# DEMO-SWITCH: Absolute maximum, ESD, thermal, recommended operating (p.4-5)

<!-- atlas:covers esd-ratings thermal recommended-operating-conditions -->

aka: absolute maximum ratings, abs max, stress ratings

Only one absolute-maximum rating applies at a time (fn 4, p.4). Two short lines of prose at most per
table: what the header conditions are, and which footnotes change a row's meaning.

## Absolute maximum (p.4)

<!-- atlas:table kind=limit group=abs -->
<!-- atlas:end -->
```

You write the prose, headings, `aka:` lines and marker pairs. `doc render` fills every marker pair
from the records it selects and keeps everything outside byte for byte. Selectors: `kind` (or a bare
first word), any field (`group=ec,abs`, `id=demo-switch.ec.V*`), exclusion (`id!=...`), pages
(`p=5-9`: any cited page), and the first cited page only (`p0=5-9`: a row's own page, when its later
cites are a footnote page shared with other tables). Values render as the quote prints them
(`1.50` stays `1.50`), so quote the numbers line. **Never write a table of records by hand**: it would drift from the records, and the row
rules (one condition per row, no merged cells, symbol and unit on every row, footnotes in a Note
column and listed under the table) are the renderer's job.

A section of quirks (`s/quirks.md`, `<!-- atlas:table kind=quirk -->`) is always written. Most of
what a driver writer needs to be warned about is in it.

### 7. The card (tier 1)

`card.md`, at most 1.5K tokens: identity (part, variants, document and revision, page count), what
the part is in one line, a variant warning if variants differ, pins in one line, 10-15 key limits,
one timing line, 5 behaviour bullets, the quirks to check first, and an index of the s/ files. Every
number cited or tagged, as everywhere.

### 8-9. Render, check, fix

```
embarch-atlas doc render <doc_id>
embarch-atlas doc check <doc_id>
```

`doc check` sets `prov` on your records (`llm-verified` when every check passes, `llm-unverified`
when one fails), re-renders the tables so the Note column shows it, and exits 1 while anything
fails. What the letters mean and what to do:

| Check | Finding | Fix |
|---|---|---|
| V | the record does not validate | the field name or type the message names |
| A | `not-found` / `wrong-page: quote found on pN` / `no-quote` | re-read the page and copy the line; fix `p`. A quote that the text layer really lacks means the fact is from a picture: `src: figure` |
| N | a value is not a number of its quote | quote the numbers line, or fix the value |
| K | the independent column assignment disagrees | a column slip or wrong unit: re-read the row |
| C | a number of `cond` is not on the cited page | fix the condition, or cite the page it is on |
| U | unit does not fit the symbol, or is not on the page | keep the printed unit; tag a conversion |
| O | min > typ or typ > max | usually a column slip |
| R | a ref resolves to nothing, or an id repeats | fix the id |
| X | an invariant did not give its `expect` | a wrong value, or a wrong invariant |
| G | register structure, or your twin disagrees with the parse | re-read the bit table; if the document disagrees with itself, there is a `conflict` record, so write a quirk. Field names match without case or separators (`Filt Empty` = `FILT_EMPTY`); what the parser could not read (no access column, no reset in a heading) is not compared; `rclr` refines a printed `R`. A register whose printed reset disagrees with its fields' resets, with a `conflict` record naming it, is a warning |
| M | a number in card.md or s/ is on no cited page | cite the right page, or tag it `(derived)`, `(figure)`, `(inference)` |
| S | glued, packed or fused cells in a markdown table | a table written by hand: replace it with a marker |
| T | card or section over budget | cut or split |

Stop when `doc check` exits 0, or when every remaining finding is an `llm-unverified` record whose
`note` says why (a figure fact, a synthesis across pages without one quotable sentence). Report:
records per kind, anchored count, what is unverified and why, card and section token sizes, and the
number of image Reads.

## Provenance

| prov | Meaning | Set by |
|---|---|---|
| `deterministic` | a parser read it from the text layer | the producing verb |
| `deterministic+crosschecked` | and an independent extraction agrees | `doc check` (G) |
| `llm-verified` | you wrote it, and every check passes | `doc check` |
| `llm-unverified` | you wrote it, and a check fails or cannot run (figures, equations) | `doc check` |
| `engineer-declared` / `engineer-confirmed` / `measured` | from the engineer or the bench | never you |

**`(figure)` is its own class, the weakest.** Values read off images are wrong far more often than
values read off tables, and no deterministic check can confirm them. A figure fact stays
`llm-unverified`, is tagged `(figure)` wherever it appears in markdown, and reaches a card only
marked as such. Its second look happens in atlas-build, not here.

## Where the quirks are (checklist)

Walk it for every datasheet; a generic "summarise the document" pass finds few of these.

1. Every footnote: does it narrow a condition, say "not production tested", or redefine a symbol's edges?
2. Any row whose condition overrides the table header (a single temperature, "dark", a narrower supply).
3. The same quantity in two places (features vs table, text vs table, figure vs table, two tables): compare the values.
4. Thresholds against ranges: protection thresholds vs the operating range, OCP vs the abs-max current, TSD vs the abs-max TJ.
5. Limits with no max (or no min) that someone will want to budget with.
6. "must", "do not", "should not", "recommended", "not recommended" sentences.
7. Reset or default values that violate a stated rule, or that do not reflect the real state (strap-controlled bits).
8. Read or write side effects: clear-on-read, latch-on-write, write order of multi-byte registers.
9. Pin meaning that depends on the variant or the mode.
10. Absences: a pin with no pull, no ID register, behaviour not stated for a state (disabled, UVLO).
11. Figure-only facts: timing diagrams, block diagrams, typical-application values.
12. Document artefacts: notation traps, name typos, symbols defined nowhere, a section marked "not part of the specification".

## Conventions (from the first real batch, 2026-09-30)

These settle cases the rules above leave open. They were each hit on a real datasheet.

**Values**
- `±X` printed in a MIN or MAX column is `min: -X, max: X`. `±X` printed in TYP stays the printed
  string (`"typ": "±0.48"`); the column check skips string values.
- An accuracy row that prints a setpoint in TYP and a `±%` tolerance on the next line is two
  records: the setpoint `<id>`, and `<id>.tol` with `unit: "%"`.
- One value cell merged across two rows (two symbols sharing `0 to 5.5`): both records carry the
  value, quote across the rows, and `note: "merged cell with <other id>"`.
- A reset that depends on a variant or a strap is `reset: null` with both values in `note`. On
  parts with non-volatile restore (a RAM register loaded from an NV copy): use the printed initial
  value for RAM registers and the factory default for NV registers, and say which in `note`.
- A field reset derived from the register reset (the document prints none per field) is allowed,
  with `note: "reset derived from register reset"`. When the page also prints the field's own
  power-up value and it agrees: `note: "reset printed per field (p.N), agrees with the register reset"`.
- Clear-on-read that depends on a mode bit has no SystemRDL spelling: `access: ro` and `note:
  "clears on read when <condition>"`. Use `rclr` only when it always clears.
- Register `width` usually comes from the summary table: cite that page and the register's own page
  (`p: [summary, own]`).

**Names**
- Keep native names verbatim in `name`, spaces, commas and slashes included; the `id` uses the
  name with runs of non-alphanumerics turned into `_`. A register or field with a deterministic twin
  takes the twin's id (`doc regparse` upper-cases: `INPUT_CURRENT_IN_DPM`).
- A value field printed as per-bit rows (`Input Current in DPM, bit 6` ... `, bit 0`) is one field:
  `name` is the printed words without `, bit N`, `bits` the union, the per-bit weights in `desc`.
- An unnamed field (an ADC result byte) is named after the table caption.
- An overlined name: `name: "<NAME>-bar"` (`FLT-bar`, `FB/INT-bar` when only INT carries the bar:
  say so in `note`). The id takes a `_bar` suffix only when another record already has the plain id
  (EN vs EN-bar). `active` follows the pin's printed wording ("active-low"), not the bar alone: an
  overlined gate drive that states no polarity gets no `active`, and a dual-function pin gets
  `active` only when every function is active-low (FB/INT-bar: none; say which in `note`). `doc prep`
  flags pages with overlines (`overline`): read those rows from an image, because the text layer
  drops the bar.
- A bit diagram shared by a non-volatile register and its RAM copy ("nX (1C6h)/X (013h) Format")
  names the field once: both registers' field records take the name the diagram prints, and a
  `doc-artefact` quirk says the RAM field carries the NV name.
- A pin table with two packages: `pin: [<number>, <ball>]` in one record per name.
- An MCU pin's I/O structure (`FT`, `FT_fa`, `TT_a`, `RST, FT`) goes in `io`, exactly as printed;
  `type` stays for od/pp/analog/supply/ground. The join reads `io` for 5 V tolerance.

**Straps**
- An address strap's `value` starts with the 7-bit address in hex (`0x44`), the 8-bit or binary
  form in `note`. A fixed address (no strap pin) is `pin: ""`, `condition: "fixed"`. A variant
  without an address (a 1-Wire variant) gets `condition: "none"` and says so in `note`.

**Quirks and checks**
- Two prose statements that disagree: quirk class `text-vs-text`.
- `check` records render only in `s/invariants.md` (`<!-- atlas:table kind=check -->`), never in
  `s/quirks.md`; their expressions are not numeric claims. A check can read `min/nom/typ/max/value`
  and a register's or field's `reset` and `bits`.

**Files and budgets**
- A section over 4K tokens with no subsections to split on: `s/<secid>.md`, then
  `s/<secid>-part2.md`, `-part3`..., each with `<!-- atlas:covers ... -->` for the ids it holds.
  The same for `s/quirks.md` (`quirks-part2.md`). Do not name a file after a parent section to
  make it an umbrella; list the ids it covers instead.
- A field-less register map (a summary only) renders as one compact row per register; do not add
  empty field tables.
- **Card drop order** when 1.5K tokens cannot hold everything: keep identity, what the part is,
  the variant warning, the quirks to check first and the s/ index; then cut the limits to the
  8 a driver writer budgets with, the pin line to a pointer (`pins: s/pins.md`), and the register
  summary to a count plus a pointer. Cut in this order until it fits; do not restore an earlier cut.
- `doc check` M counts numbers in prose. A count you computed (fields per register, requirements
  per section) is `(derived)`. An inline cite on a line (`(p.12)`) **replaces** its heading's
  cite for that line; it does not add to it.
- `guar`: `production-tested`, `design` ("guaranteed by design, not tested"), or
  `characterization` ("based on characterization results, not tested in production").

## Known traps in datasheet text layers

- **Symbol-font glyphs in private-use codepoints** (common in word-processor exports): Ω, ≤, µ.
  `Read` shows them as nothing, so a quote typed from it lacks them; the checker accepts the
  quote with them mapped or dropped. `toc.md` lists the pages (`pua-glyphs`).
- **Footnote markers glued to values or symbols** (`1.0A17`, `Time21`).
- **Raster figures**: timing diagrams and typical curves as images; the text layer has the caption
  or nothing.
- **Math fonts**: equations come out as glyph soup; read them from an image.
- **Binary reset values printed with an `h` suffix** in some register headings (`11100100h` is
  0xE4). `doc regparse` reads them as binary when there are 8 digits of 0/1 per byte and notes it.
- **Multi-byte logical registers**: store bus-addressable bytes, as above.
- **Bit diagram vs field table disagreements** inside one register description: `doc regparse`
  writes them as `conflict` records.
- **Footnote numbering that restarts per table**: cite the footnote of the table the row is in.
- **Soft hyphens and hyphenated line breaks**: the checker joins them, so quote the word whole
  ("requested" for `re-` / `quested`). A compound broken at its own hyphen is quoted with it
  ("auto-retry"); the checker compares the page both ways.
- **Two-column layouts**: `pdftotext -layout` can interleave columns; split a multi-line quote with
  `...` into parts that each sit on one line.
- **Tables set sideways** (a pinout with one column per package, on a page turned 90°): the text layer
  scrambles them. `embarch-atlas doc columns <doc_id> <page>` prints the page as rows and cells in
  reading order, cut at the table's rules. Read the cells from it, then quote the text layer as usual
  (a cell's words are on the page); check a surprising cell against the page image.
- **Table-of-contents pages without outline entries** are grouped as `front-matter` in `toc.md`;
  they are not part of the section before them.

## Forbidden

- Untagged numbers that are not on a cited page.
- Invented citations, or a page you did not read.
- Fetching any document, or sending document content anywhere but this session.
- Board or firmware facts (nets, reference designators, the fitted variant, strap values chosen on a
  board, a limit the firmware applies).
- Editing `regparse.jsonl` or any deterministic record; deleting a failing record.
- Tables written by hand in `s/` where a marker can render them.
