# Datasheet distillation: tiered, cited, cross-checked

**Status:** draft, 2026-09-29.

Territory: board-independent distillation of component datasheets into tiers
(card, per-section markdown with page citations, raw page on demand), plus
structured records for limits, timing, pins, behaviour, registers and quirks,
with a deterministic cross-check. Spike: a draft Claude Code skill applied to
two real component datasheets — a small analog load-switch-class part (27
pages, placeholder `<load switch>`) and a buck-boost regulator (55 pages,
placeholder `<buck-boost>`). Token counts use a common open tokenizer as a
proxy for Claude's own tokenizer.

## Headline findings

1. **Quote anchoring plus an independent word-position column extractor
   verified all 426 record quotes and all 206 checkable table rows, with 0
   min/typ/max disagreements.** The first pass had 8 anchoring failures on
   the load-switch part: 1 wrong page written by the LLM, and 7 caused by
   symbol-font glyphs that the Read tool renders as nothing.
2. **Error injection shows where the checker is strong and where it is
   blind.** It catches 88-100% of column slips, 95-98% of value typos, and
   100% of page shifts and of self-consistent hallucinations (value and quote
   both rewritten). It catches only 58-65% [corrected in verification] of
   changed conditions, and only 25% of unit-prefix errors on the part whose
   units sit one per row-group (95% on the other part).
3. **Across both datasheets the LLM wrote no wrong value, but 25 of 1,708
   numbers in the markdown tiers (1.5%) were mis-cited or untagged.** The
   number check caught all of them: 6 cited to the wrong page, 2 cites the
   checker could not parse, 1 unit conversion, 3 untagged derivations, 12
   untagged values read off figures, and 1 parameter name the checker read as
   a number.
4. **Size per tier: the card is 1.4-1.7K tokens, and the full sections are
   11.7-13.8K.** Against the text layer (13.6K and 34.0K) that is 5-11% for
   the card, and 0.4-0.9x for the sections. Both parts together took 14 Read
   calls. The load-switch part needed about 12K visual tokens of figures on
   top of its text, because a third of its pages have almost no text layer.
5. **A vendor's register-description template parses deterministically.**
   Result: 8 registers, 33 fields, 0 diffs against the LLM records once
   reserved-bit granularity was normalised. The parser also found 6
   inconsistencies inside the datasheet between its bit diagrams and its
   field tables.
6. **The quirks section holds most of what a driver writer needs to be
   warned about.** Of the 43 quirks found across both parts, 25 would be
   lost without the section, 14 partly lost, and 4 are carried elsewhere
   anyway. 17 of the 25 are about the part rather than the document.
   Examples: an over-voltage-protection trip point that can occur inside the
   part's own stated operating range; an over-current-protection mask bit
   that resets to the wrong state for its own stated rule.
7. **Coverage against real drivers: the records held 17 of 18
   datasheet-derived facts the buck-boost driver relies on, and 5 of 7 the
   load-switch driver relies on.** The buck-boost miss was the absence of an
   ID register. Two of the five load-switch facts exist only in figures, and
   one more was partly covered. Two driver claims are not supported by the
   datasheet text, which makes them consistency-review candidates.

## 1. Method

- **Scripts:** a page-prep tool (per-page layout text, outline, tokens and
  images per page); an independent register parser for the vendor template
  used; a renderer that produces section tables from records, so the
  markdown tier cannot drift; a checker running checks A-M (see §4); an
  error-injection tool.
- **LLM part**, done in-session: read the text layer in chunks, render or
  crop images only where the text fails, write records, prose, section plan
  and card.
- **Wall time was 31 minutes**, from the first datasheet read to all checks
  green on both parts. That includes writing the five scripts for the first
  time and about 5 minutes of interleaved web research. A second datasheet
  with the tooling in place should take about 10-15 minutes of session time
  [inference; not measured].

**Read calls:**

| Part | Text-layer reads | Image reads | What the images were for |
|---|---|---|---|
| Load-switch part | 1 (971 lines, 13.6K tokens) | 2 PDF-page reads + 7 raster images at 100 dpi | timing diagrams, typical application, block diagram, one labelled figure, typical curves |
| Buck-boost part | 3 (about 29.8K tokens) | 1 stacked crop of the datasheet's equations | the text layer renders the vendor's math font as glyph soup |

- **Image cost** [inference, arithmetic]: a 100 dpi page is roughly 1,240
  visual tokens by the standard patch formula; the equation crop, 841. Public
  guidance gives a rough 1,500-3,000 text tokens per page plus the page image
  in visual-plus-text mode [corrected in verification: that public figure is
  specific to one cloud provider's document-ingestion mode, not a general
  rule].

## 2. Schemas and standards to borrow

| Standard | What it models | Fit for datasheet facts | Take |
|---|---|---|---|
| An industrial data-specification template (property + min/nom/typ/max levels) | property: name, symbol, unit, data type, definition, value list; **level type MIN/NOM/TYP/MAX** | level type is exactly a datasheet's column set, NOM included | the min/nom/typ/max names; no opaque identifiers, since the prior encoding rules ban them |
| A related conceptual model with "depending properties" under "conditions" | properties conditional on named conditions; property levels min/max/typ/nom | the "condition" idea is theirs | model conditions as data, not only as strings (a later step) |
| A parts-marketplace spec format | value, SI value, units, display value, value type; one value per attribute, no test conditions | too flat for electrical-characteristics tables | the SI-value idea, for machine comparison |
| A schematic-tool's symbol field model | reference, value, footprint, datasheet link + free user fields | board BOM, not part facts | the datasheet-link field as a schematic-to-part join key |
| A register-description DSL (from a prior note) | software/hardware access, reset, on-read clear, on-write clear, single-pulse, encode enums, register/access width | registers yes; no page or provenance | vocabulary for access and on-read semantics |
| A widely-used chip-vendor register XML format | access, reset value + reset mask, read-action clear, one-to-clear write, write constraint, alternate register | assumes memory-mapped peripherals | reset-mask idea for undefined reset bits |
| A newer IP-description standard | registers/fields, read-action, modified-write-value; mode-dependent access | expresses "these bits only matter when this other bit is set" directly | the mode-dependence concept; the XML itself is overkill |
| A Rust hardware-driver crate's manifest format | registers (address, access, reset, fields), plus commands and buffers | the only format seen designed for I2C/SPI external parts | the export target for external parts |
| A Linux kernel register-map abstraction (from a prior note) | register defaults, volatile, precious (clear-on-read) tables | a second, independent semantic source when a reference Linux driver exists | the cross-check source |

Considered and skipped as poor fits: a materials-declaration standard (no
electrical parameters), a board-description XML standard (schematic
territory, not datasheet), an open MCU-register-data project's YAML (MCU
side only, no reset values).

**Decision (spike):**
- **Normal form.** YAML records per class. Registers use the register-DSL
  vocabulary: `access: RW|RO`, `onread: rclr`, binary resets.
- **Fields none of the standards has.** Each record also carries page,
  quote, footnotes, a production-tested-vs-design-only flag, a figure-source
  flag, and conditions.
- **Export targets.** A register-description format could be generated from
  the records; no new format is needed for the LLM to read.

**Lesson: YAML 1.1 turns `yes`/`no` into booleans.** A field meant to hold
the literal string "yes" came back as `True`. Records need a schema, or
quoted strings.

## 3. Literature 2023-2026: LLM extraction from datasheets and tables

| Work | Numbers | Failure modes and mitigations |
|---|---|---|
| Datasheet-to-model extraction pipeline | EM 0.86, F1 0.92, exact 0.96; large gain over best baseline | unstructured documents, inconsistent naming; fixes: per-document focus, structure-aware retrieval, name normalisation |
| Datasheet-aware compatibility checking (7 designs, 34 datasheets) | 97.5% verification accuracy; much smaller context than upload-and-query | retrieves only needed properties, avoids LLM numerical computation — matches this note's checks-as-data split |
| Priority-based materials extraction (~12K points, 30 papers) | 94.69% overall; **figure digitisation 83.49%**, lowest of all sources | figures are the weakest source, supporting a separate figure-provenance class |
| Historical tables to structured data (285 tables) | 95.4% exact cell match | shifted rows/columns, skipped wrapped columns; fixes: row-count check, totals cross-check |
| Multimodal LLMs vs OCR on table structure | vision model slightly better on structure; **LLMs "far better"** on cell content | extract structure and content separately, matching this note's deterministic column check |
| A grounded-extraction library | each extraction has a character interval; ungroundable ones filtered | matches this note's anchoring, done in-pipeline |
| A cloud citations API | cited text guaranteed to point into the source; image citations unsupported | same limit as this note's figure-only facts; not in the owner's stack, so anchoring is local |

**Not found:** a datasheet-specific electrical-characteristics benchmark,
after three searches. §4's error classes come from general table-extraction
literature, not datasheet-specific studies.

## 4. Cross-check methods, and what each one catches

| Check | Mechanism | Cost | Blind spot |
|---|---|---|---|
| A: quote anchoring | normalised quote must occur on its cited page in the layout text; symbol-font glyphs matched both mapped and stripped | fast | anything only in an image |
| N: number in quote | each numeric min/typ/max must parse out of its quote | trivial | a hallucination that rewrites the quote too (A catches that) |
| K: independent columns | word x-positions under the nearest MIN/TYP/MAX header re-derive the columns and the unit column | fast | single "Value" columns |
| C: condition numbers | every number in a condition string must be on the page | trivial | a changed number that happens to appear elsewhere on the page |
| U, O: unit and ordering | unit dimension against the symbol; min <= typ <= max | trivial | same-dimension prefix errors (mV vs V) |
| X: invariants | LLM-proposed expressions, each with an expected pass or fail | trivial | only as good as the proposals |
| G: registers | widths cover the register, no overlap, field resets compose to the register reset, unique addresses, summary-table coverage, diff against the deterministic parse | trivial | needs a template-specific parser |
| M: markdown numbers | every number in card and sections must be on a cited page, in a record, or tagged derived/figure/inference | fast | a correct number cited to a wrong page that happens to also contain it |

**Error injection** (40 injected errors per class):

| Injected error | Load-switch part caught | Buck-boost part caught | Caught mainly by |
|---|---|---|---|
| column slip | 35/40 (88%) | 40/40 | K |
| value dropped | 30/40 (75%) | 40/40 | K |
| value typo (x10, /10, +1) | 38/40 (95%) | 39/40 (98%) | N, K |
| sign flip | 40/40 | 40/40 | N, O |
| value and quote both rewritten | 40/40 | 40/40 | A |
| unit prefix (mV to V) | 10/40 (25%) | 38/40 (95%) | K unit column |
| page shifted by 1 | 40/40 | 40/40 | A |
| condition number changed | 23/40 (58%) | 26/40 (65%) [corrected in verification] | C |

The load-switch part's lower rates come from its "Value"-column tables (10 of
98 rows not column-checkable) and units printed once per row group (35 of 98
rows show one). Unit and condition errors on such datasheets need a targeted
second pass: an image re-read of those rows, or a second independent
extraction diffed [inference].

## 5. Spike results

### 5.1 Facts extracted per class

| Class | Load-switch part | Buck-boost part |
|---|---|---|
| Limits and thresholds (rows) | 109 | 110 |
| Timing records / sequences | 25 / 2 | 27 / 3 |
| Pins | 11 names, 20 balls + a 3-variant enable-pin truth table | 23 records, 26 pins + an 8-row mode-strap table |
| Behaviour rules | 23 | 30 |
| Registers / fields | - | 8 / 31 (33 in the vendor's own split) |
| Quirks | 20 | 23 |
| Invariants (checks) | 12 (2 expected to fail) | 13 (2 expected to fail) |
| Figures and equations indexed | 15 | 15 figures + 9 equation entries |

### 5.2 Checker results: first pass and final

| Check | Load-switch first pass | final | Buck-boost first pass | final |
|---|---|---|---|---|
| A anchored | 178/186 | 186/186 | 239/239 | 240/240 |
| N | 119/119 | 119/119 | 128/128 | 128/128 |
| K agree | 88 (1 false positive) | 88 of 98 (10 not checkable) | 118/118 | 118/118, units 118/118 |
| X as expected | 12/12 | 12/12 | 13/13 | 13/13 |
| G diff vs deterministic parse | - | - | 1 (reserved granularity) | 0 |
| M unanchored numbers | 21 (17 LLM-origin, 4 checker bugs) | 0 of 708 | 12 (8 LLM-origin, 4 checker bugs) | 0 of 1,000 |

**First-pass failures and their causes:**

1. **Hidden glyphs (tooling).** 5 quotes failed because the text layer holds
   an ohm symbol and a less-than-or-equal symbol in private-use codepoints,
   which the Read tool renders as nothing. Fix: the checker now matches both
   a mapped and a stripped form.
2. **LLM citation errors.** Several values and one behaviour record were
   cited to the wrong page by one or a few pages, including one formula cited
   to a page that only holds an addendum date.
3. **Untagged numbers.** A resistance value written in the wrong unit
   prefix; several derived values (time, voltage, a numeric code) left
   untagged; several typical-curve values read off figures and left untagged;
   one parameter name misparsed as a number.
4. **Checker bugs, fixed.** A percentage range parsed as a negative number; a
   step reference parsed as a page citation; heuristics misfired on a
   capacitor's voltage rating and on an LSB unit; the register
   summary-coverage key was mismatched.

### 5.3 Tokens per tier

| Tier | Load-switch part | Buck-boost part |
|---|---|---|
| Raw text layer (all pages) | 13,580 | 33,977 |
| Figures needed as images | about 12.4K visual tokens (10 page images) | 841 (one equation crop) |
| Tier 1 card | 1,452 | 1,711 |
| Tier 2 sections | 11,692 (10 files) | 13,775 (11 files) |
| Records (YAML) | 20,638 | 23,758 |

- **Markdown is cheaper to read than the records:** the same limits table
  costs less than half as many tokens rendered as markdown compared to its
  YAML records; quotes and IDs live only in the records. So markdown for the
  LLM, structured records behind the query tool [inference; agrees with the
  prior encoding note].

### 5.4 Deterministic register parse (buck-boost part)

- **What the parser reads.** It parses the vendor's headings, bit diagrams,
  field tables and summary table in under 1 second.
- **Diff against the LLM records.** 0 diffs over reset values, bits, access
  and clear-on-read behaviour, once adjacent reserved fields the datasheet
  splits into 3 rows were merged into the single field the LLM had written.
- **Inconsistencies inside the datasheet (6):** a field marked read-write in
  the bit figure but read-only in the field table (twice, on different
  registers); a status field's reset width disagreeing between the figure
  (1 bit) and the table (2 bits); one register's multi-byte split showing
  more tokens in the figure than rows in the table.

  All three kinds were noticed while reading by hand, but the parser found
  them without reading, and that scales to register maps too long to read.

## 6. Every quirk found, and whether it would be lost without the quirks section

`missed`: yes = lost, partly = the facts survive in other records but the
consequence does not, no = carried elsewhere.

**Load-switch part (20 quirks), representative examples:** variant-dependent
enable-pin decoding; an over-voltage trip point inside the part's own stated
operating range; a fault-flag behaviour a footnote states more strongly than
the timing figure shows; a soft-start current limit stated differently in
prose than in the table; several parameters guaranteed by design only, not
production tested; reverse-leakage specs given only under a narrow "dark"
condition; a discharge path and an enable-vs-disable behaviour shown only in
figures; a current-limit spec exceeding the absolute-maximum continuous
rating in another table; three different reverse-current thresholds
scattered across text and two tables; three timing parameters that appear
only in figures, never in text; and footnote numbers glued to values plus
symbol-font glyphs that break naive text extraction. 14 of the 20 would be
lost or partly lost without the quirks section.

**Buck-boost part (23 quirks), representative examples:** binary register
resets printed with a hex suffix that looks like a different base; a mask
bit that must be cleared around certain transitions but resets enabled; a
mask-bit naming convention meaning "enabled" rather than "masked out"; a
figure-vs-table disagreement on access and reset for reserved bits; a stated
output range that exceeds what the internal feedback path can reach, by
calculation; a thermal-shutdown threshold above the absolute-maximum
junction temperature, itself above the recommended operating temperature;
an enable-low reset that clears most registers but leaves the
input-undervoltage case unstated; mode-select bits ignored while another bit
is unset; three different voltage thresholds and three different
inductor-value ranges given across table and prose in different units;
equations unreadable as plain text and with an implicit unit; a status
register that resets to "reserved" and clears its own fault latches when
read; several apparent name and direction typos in the pin/signal table; an
application-information section explicitly marked "not part of the
specification"; a hiccup condition needing two simultaneous states, only one
obvious; and the absence of any device-ID register, so undefined addresses
simply do not acknowledge. 17 of the 23 would be lost or partly lost without
the quirks section.

## 7. Coverage against real drivers

The drivers are used here to judge coverage only, never as ground truth.

- **Buck-boost driver.** Covered 17 of 18 datasheet-derived facts the driver
  relies on: register addresses and fields for the relevant registers; a
  split-byte encoding order; a fixed voltage step and floor; a sense-voltage
  ceiling; the mask-bit reset rule; clear-on-read status behaviour; an
  active-low fault output and a stated discharge current; hiccup and
  slew-rate encodings. Missed: "the part has no ID register," added as a new
  quirk after the comparison. The driver's own voltage model and the
  datasheet's stated formula agree at the low and mid points and are within
  one encoding step at the top of the range. One driver comment claims a
  mode-select behaviour that the datasheet's own text does not support as
  written, flagged as a consistency-review candidate — it could still be
  correct measured behaviour.
- **Load-switch driver.** Covered 5 of 7 datasheet-derived facts the driver
  relies on, including a timing bound, a debounce interval, a figure-only
  discharge-resistance value, and a figure-only claim about a fault-flag
  reading while input voltage is below the undervoltage-lockout threshold.
  Partly covered: a disabled-part flag reading, visible only in a figure.
  Derivable but unstated: an approximate internal dissipation figure near the
  part's thermal limit. One driver comment (about fault-flag behaviour below
  undervoltage lockout) matches one figure but contradicts a literal reading
  of a footnote — another consistency-review candidate.
- **Would a distillation without figures and quirks have covered this?** Not
  fully. Several driver-relevant facts would be missing entirely, and a
  couple more would survive only inside field descriptions, without their
  consequence stated. These are exactly the places where the drivers carry
  their own warning comments.

## What this means for atlas

All inferences.

1. **Make records the source of truth and render the markdown from them.**
   The LLM writes only records, prose and the card. The number-anchoring
   check then polices prose alone.
2. **Ship the checker as the gate; mark red records `unverified`, never
   delete them.** It costs about 1.5 s a part and catches the classes the
   literature reports. Unit-prefix and condition errors, and figure-only
   facts, need a targeted second pass: an image re-read by a fresh subagent,
   diffed.
3. **Figure-only is a provenance class of its own, the weakest tier.**
   Literature: 83% accuracy for figure digitisation against 95% for tables.
   This spike: 2 of 7 driver-relevant facts on one part exist only in
   figures. A figure fact reaches the card only after a second look.
4. **The quirks section is the product, not an appendix.** 25 of 43 quirks
   would be lost without it. A checklist-driven pass (a fixed set of quirk
   categories to look for) is what found them; a generic "summarise the
   datasheet" pass would not.
5. **Write one deterministic register parser per vendor template** (one
   vendor's template took well under an hour), with the LLM as fallback,
   validated the same way. The parser also audits the datasheet itself.
6. **Reading cost scales with figures, not pages.** One part needed 10 page
   images for 27 pages; the other needed 1 crop for 55. A page-prep step
   should flag image-heavy pages up front so a skill can budget image reads
   per datasheet.
7. **Some driver claims contradict or go beyond the datasheet text.** A
   mismatch report should include "driver says X, datasheet says Y or
   nothing" rows fed from quirks, not only devicetree-versus-schematic
   mismatches.
8. **Records need a schema** (the YAML 1.1 yes/no trap hit this spike); a
   checked JSON Schema closes it.

## Open questions for the owner

1. **Figure-only facts.** Accept one LLM read tagged "figure," or require a
   second independent read by a fresh subagent before a figure fact may
   appear in the card?
2. **Driver-versus-datasheet contradictions.** Recorded in the part's
   quirks, in the mismatch report only, or both?
3. **Variants.** Is keeping every ordered variant in the datasheet
   distillation and leaving the fitted one to the schematic analysis the
   right split?
4. **Application sections.** A vendor's own "not part of the specification"
   section: distill it under a separate provenance class, or leave it on the
   raw page?
5. **Record files.** One file per class, or one per part? YAML with a
   schema, or JSON Lines?

## Could not verify

Per-call token cost inside a live session; a genuinely independent second
LLM extraction (no subagent tool available); several figures indexed only by
caption; the newer IP-description standard's exact changes (read from a
slide deck); whether other datasheets in the wider project share this
part's register template.

## Sources

External: an industrial-parts data-specification standard and its
conceptual-model relative; a parts-marketplace spec-field reference; a
chip-vendor register XML format's public docs; a newer IP-description
standard's public change summary; a Rust register-manifest crate's docs; an
open MCU-register-data project's format repository; a schematic tool's
field-reference docs; the arXiv preprints and one ACL workshop paper cited
in §3; a grounded-extraction library's repository; a cloud provider's
citations, vision and PDF-support documentation.

Local: the project scope note and prior internal research notes; the two
component datasheets used in the spike; the two drivers and their
devicetree bindings used for the coverage comparison in §7; the project's
existing hand-written distillation style, used as a reference for tone.

## Verification (2026-09-29, independent)

An independent pass re-ran the checker and mutation scripts on copies and
opened the same sources. Confirmed: the anchoring counts, the "no wrong
value" claim (sampled by hand), the tokens-per-tier figures, the near-empty
page count and image-token estimate, the vendor register parse and its 6
internal inconsistencies, the quirk counts and classification, the driver
line-by-line coverage and the voltage-model arithmetic, and the literature
numbers in §3. Corrected: one mutation rate in §4 (a stored report predated
the final version of the checker; other 15 cells matched); the public
guidance figure in §1 (specific to one cloud provider's ingestion mode, not
general); and one standard's versioning detail (the newer version of the
Rust register-manifest crate dropped its older manifest formats entirely).
One claim (that the 25-of-1,708 mis-citation count came from the very first
pass) is unverifiable, since first-pass reports were not kept, though the
final sums hold.
