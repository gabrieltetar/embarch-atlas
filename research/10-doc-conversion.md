# PDF to LLM-ready text for datasheets, reference manuals and requirements docs

**Status:** draft, 2026-09-29.

Territory: turning component datasheets, an MCU reference manual and a product
requirements document into text an agent can read in slices, with numbers it
can trust. Schematics are another note's territory (`12-schematic-pdf.md`).
Builds on a prior survey's register-map template parser and encoding rules,
and its "deterministic core first" evidence. Those are not re-argued here.

Spike: 15 pages from three public documents (two component datasheets and
excerpts of a public MCU reference manual), 3 tables with hand-built ground
truth, 12 converter configurations plus Claude reading the page images.

## Headline findings

1. **The PDF text layer already holds every number exactly, and converters
   fail on structure, not characters:** `pdftotext -layout` placed 29/29 and
   64/64 electrical-characteristics values and 80/80 reference-manual timing
   cells correctly at under 0.01 s per page, losing only merged cells (units
   0.71, symbols 0.78/0.82).
2. **The learned converters are not safer than the text layer:** one fast
   local converter scored 0/49 and 0/87, a basic OCR pipeline scored 6/29 and
   0/64 values (with glued numbers such as `350450350450`), a small
   vision-language model scored 0/87 at 196 s per page, and a mid-tier
   document converter scored 0.94/0.95, with most of its correct values in
   packed cells that are right only by order.
3. **No converter wins all three tables.** One markdown-oriented converter
   leads the smallest table at 0.96; three different tools tie on the second
   table at 87/87, but the markdown-oriented one files only 49% of those rows
   under the right symbol.
4. **Claude reading a 150-dpi page image transcribed both
   electrical-characteristics tables with 49/49 and 87/87 facts at 18-22 s per
   page and wrote no number missing from the text layer,** at about 2,700
   visual tokens per page against 500-950 text tokens. The result is
   contaminated, because the reader had seen both pages first (see §4).
5. **Five checks that need no ground truth flagged every catastrophic
   failure in the spike:** number-multiset recall fell to 0.660 for the
   weakest converter, one text-based extractor had 57 orphaned subscripts, and
   fused headers, glued numbers and packed cells were all caught. They missed
   one converter's 0.49 symbol-misfiling rate. [corrected in verification: no
   pass/fail threshold is set, so "flagged" is a post-hoc reading.]
6. **A reference manual's register tables are redundant with its
   machine-readable register-description file (SVD or equivalent):** a
   40-line `pdftotext` parser matched all 76 fields of one peripheral's
   register table to the vendor's SVD, names and bit positions, while no
   converter kept the reference manual's own bit-grid figure intact.
7. **Licence and privacy, not accuracy, are what disqualify some tools
   here:** two of the four strongest local tools carry a constraint — one is
   AGPL-3.0, another ships with local parsing disabled by default and a
   remote endpoint plus telemetry that runs unless explicitly disabled.
   [corrected in verification: accuracy does disqualify the weakest two tools
   outright (finding 2); licence and privacy constrain the accurate ones.]

## 1. What was measured

**Pages.** Subsets were cut and rendered at 150 dpi:
- One component datasheet: an ordering table, pin table, absolute-maximum
  table, operating-conditions and electrical-characteristics tables.
- A second component datasheet: an electrical-characteristics table, a
  register summary table, and one register's bit figure and field table.
- Public reference-manual excerpts: two I2C timing tables, a register-field
  description, and a register-map table with a rotated bit grid. Located
  through the document's own outline (1,965 entries in the full manual).

**Ground truth** comes from reading the page images by hand. Every value and
row key was then confirmed present in the text layer (0 missing).
- **A:** a supply-specifications table, 49 facts (29 values, 20 units). Merged
  symbol and unit cells, 2-4 sub-rows per cell, one footnote.
- **B:** an electrical-characteristics table, 87 facts (64 values, 23 units).
  Merged parameter cells, tolerance rows with no condition, section rows.
- **C:** two reference-manual timing tables, 80 cells. Two-level header,
  footnote markers on values.

**Metrics:**
- **Correct:** the value is in the right row and column. A packed cell counts
  as correct only when its token count equals the number of sub-rows.
- **Symbol:** the row's symbol can be recovered by filling down within the
  table.
- **Reading order:** 11-12 anchor strings per document found, and in order.
- **Tokens:** a common open tokenizer, as a proxy for Claude's own tokenizer.
- **Wall time and peak memory:** measured directly, with 6 threads.

## 2. Results

Accuracy as correct/total. "all" = values + units; "val" = min/typ/max only;
"sym" = symbol recovered.

| Tool (class) | A all | A val | A sym | B all | B val | B sym | C | s/page | Peak RSS |
|---|---|---|---|---|---|---|---|---|---|
| Plain-text layout extraction (poppler) | 0.71 | **1.00** | 0.78 | **1.00** | **1.00** | 0.82 | **1.00** | 0.003 | 17 MB |
| Markdown-oriented PDF-to-LLM converter, +layout model | **0.96** | 0.97 | 0.88 | **1.00** | **1.00** | 0.49 | **1.00** | 0.57-0.75 | 358 MB |
| Table-focused text extractor | 0.06 | 0.07 | 0.00 | 0.20 | 0.00 | 0.75 | 0.90 | 0.06-0.15 | 76 MB |
| Table-lattice extractor | 0.92 | 0.93 | 0.78 | 0.00 | 0.00 | 1.00 | 0.90 | 0.7-0.8 | 320 MB |
| Tagged-PDF structure tree reader | 0.80 | 0.72 | **1.00** | untagged | | | roles rejected | 0.57 | 19 MB |
| Mid-tier document converter, accurate table model | 0.94 | 0.93 | **1.00** | 0.95 | 0.95 | 0.95 | **1.00** | 6.6-13.1 | 2.5 GB |
| Fast local layout converter (CPU default) | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.77 | **1.00** | 1.7-3.4 | 669 MB |
| Basic OCR pipeline (ONNX) | 0.53 | 0.21 | **1.00** | 0.00 | 0.00 | 0.63 | 0.90 | 4.4-7.0 | 3.0 GB |
| Standard-tier pipeline (+small local vision-language model) | 0.53 | 0.21 | **1.00** | **1.00** | **1.00** | **1.00** | 0.90 | 5.8-102 | 4.8 GB |
| Small vision-language model, direct (CPU) | not run | | | 0.00 | 0.00 | 0.02 | not run | 196 | 2.8 GB |
| Claude reading the page image | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** | not run | 18-22 | n/a |

Notes:
- The lattice extractor's B "sym 1.00" comes with no values: the source table
  has no vertical rules between MIN, TYP and MAX, so lattice mode fused them
  into one column.
- The tagged-PDF row is on a full datasheet (15.3 s total). 8 of its 10 errors
  come from one row shifted by one column after a vertical merge.
- The mid-tier converter's HTML export costs 1.6x its markdown and scored the
  same or lower.
- The standard-tier pipeline's small-VLM stage runs only on some regions; on
  one datasheet its output was byte-identical to the basic tier, and on the
  other it took 407 s wall for 4 pages, the run that scored 87/87.
- A bare table-finder API (no dedicated model) scored 0.00 on A and 0.20 on B,
  orphaning subscripts and fusing rows the same way the table-focused
  extractor did.
- The small vision-language model took 785 s wall for 4 pages on CPU. On the
  electrical-characteristics table it dropped a column, so every value
  shifted one column left; it also merged conditions and mangled a
  greater-or-equal symbol into plain text.

Footnotes and reading order: three tools kept footnote text intact; one
document converter dropped one footnote; the standard-tier pipeline kept 5 of
7. Value-level footnote markers were fully recovered by the markdown-oriented
converter and the mid-tier converter (10/10); the plain-text extractor got
2/10, because it prints superscripts on the line above. Anchor order was at or
near perfect for the plain-text extractor, the markdown-oriented converter,
the mid-tier converter and the standard-tier pipeline.

## 3. Failure modes seen (what an agent would misread)

- **Packed sub-rows** (mid-tier converter). Four conditions sit in one cell
  and a run of concatenated digits in the next; the right reading depends on
  matching blank-count to sub-row count.
- **Row split with the symbol mid-group** (markdown-oriented converter). The
  symbol lands on the middle line of a merged cell, so rows above it inherit
  the wrong symbol by fill-down.
- **Orphaned subscripts** (table-focused extractor). A subscript letter and
  its base symbol land on separate lines, destroying condition strings.
- **Fused columns** (fast layout converter, basic OCR pipeline, lattice
  extractor on one table). The header becomes one cell (`Min Typ Max`), so
  values can no longer be attributed to a column.
- **Glued and split numbers** (basic OCR pipeline): one 8000-unit value split
  into two tokens that individually look like other real values on the same
  page, so only a structure-aware check catches it.
- **Invented characters** (basic OCR pipeline). Inline formula recognition ran
  on born-digital text and inserted stray glyphs into otherwise normal
  numbers and units.
- **Rotated bit-grid** (reference-manual register-map table, every tool). The
  plain-text extractor drops vertical field names; the mid-tier converter
  merges neighbouring fields; the standard-tier pipeline transposes the
  table; the markdown-oriented converter collapses it. Not scored, because the
  vendor's register-description file makes the table redundant (§6).

## 4. Claude reading pages natively

| Page | Facts | Wall s | Output tokens | Numbers not in the text layer |
|---|---|---|---|---|
| Datasheet A, table page (ground truth A + operating conditions) | 49/49 | 17.8 | 1,071 | 0 |
| Datasheet B, table page (ground truth B) | 87/87 | 21.8 | 1,413 | 0 |
| Datasheet A, unseen page, 3 tables | not in ground truth | 20.6 | 1,195 | 0; only missed numbers are the page footer |

- **Contamination.** The reader had viewed two of the three pages, and several
  converters' output of them, before transcribing, so those two results
  overstate the accuracy of one blind pass. The third page was transcribed
  blind and had no ground truth, but a check found no number absent from the
  page's own text layer.
- **Merged cells.** The transcription resolved merged cells into one row per
  sub-row with the symbol repeated — an interpretation of the layout, not a
  copy of it, and marked as such.
- **Image cost.** Claude views images as fixed-size patches and costs a
  token count proportional to width times height in patch units. A letter
  page at 150 dpi costs about 2,700 visual tokens; an A4 reference-manual page
  is similar. [inference, arithmetic] A page image is therefore about 3-5x the
  text-layer token cost, and reading every page of a 100+ page datasheet as
  images is roughly 300K input tokens; reading only the pages flagged as
  table-heavy is roughly 55-80K.

## 5. Checks that detect mangling without ground truth

| Check | Rule | What it caught |
|---|---|---|
| Number multiset | Numeric tokens in the output against a raw-text extraction of the same pages: recall (dropped/split) and precision (glued/invented) | The weakest converters fell to 0.66-0.76 recall; the plain-text extractor and Claude-native both scored 0.95-1.00, where every Claude miss was a page-footer number |
| Glued numbers | A cell matching a digit-dot-digit-dot-digit pattern with no words | Caught several instances in the basic OCR pipeline's output |
| Packed cells | More than one number in a cell with no line break marker | Caught tens of instances in the mid-tier converter and standard-tier pipeline outputs |
| Min <= typ <= max | Per row, where all three are numeric | Caught one real cross-row misplacement in the mid-tier converter's output |
| Fused header | `MIN TYP MAX` in one header cell | Caught in the fast layout converter, basic OCR pipeline and lattice extractor outputs |
| Orphaned subscript | A cell that starts with one capital letter followed by a line break, or an equation fragment with no subscript | Caught 57 and 19 instances in the table-focused extractor's output |
| Register-file agreement | Field name and bit range against the SVD | 76/76 agreed for the one peripheral checked |

Not implemented, and needed [inference]: scoping the number-multiset check to
a table's bounding box instead of the whole page; a row-structure check
comparing each value's y-position to its symbol's text-layer line; a
cross-tool vote between two independent extraction routes; a units whitelist;
and the register-table width-sum and reset-alignment checks from the prior
note.

## 6. Reference manual specifics

- **Size.** A public STM32-family reference manual runs to roughly 1,400
  pages and just over 1M tokens as layout text, convertible in under 2
  seconds. It has an outline nearly 2,000 entries deep, which is a
  deterministic slicer: chapter -> page range -> per-section text with page
  cites. One peripheral's chapter ran to about 40K tokens as layout text.
- **Registers.** The 76/76 register-file agreement means the reference
  manual's own register pages mostly add prose, not new structure. The
  distillation should come from the register-description file, with the
  reference manual's field prose attached by (register, field) and a page
  cite.
- **Tagged PDF.** The public reference manual's own structure tree was
  rejected by a standard PDF tool, so the tagged-table route does not apply
  to this vendor's reference manual without a role-map-aware reader.

## 7. Document corpus facts (general shape, not project-specific)

Across a representative 9-document set (a product requirements document, an
MCU datasheet, two small analog-part datasheets, a fuel-gauge-class datasheet,
three power-management datasheets, and the reference manual), all documents
converted to layout text in about 3 seconds combined. The requirements
document, being largely figures, had far fewer text tokens per page than the
datasheets and needs the page-image route for its image-heavy pages. The
datasheets' text is dense; their figures are mostly characteristic curves. A
majority of the datasheets were tagged PDFs; the vendor-produced power-chip
datasheets in this set were not.

## 8. Survey: tools, licences, CPU cost, published accuracy

| Tool class | Licence shape | CPU cost here | Published accuracy |
|---|---|---|---|
| Plain-text layout extraction (poppler) | GPL (the tool itself) | 0.003 s/page | none; it is the text oracle |
| Markdown-oriented PDF-to-LLM converter | AGPL-3.0 or commercial | 0.6-0.75 s/page | one public table-extraction benchmark scores it well below a leading cloud model |
| Table-focused text extractor | MIT | 0.1 s/page | none found |
| Table-lattice extractor | MIT | 0.7 s/page | none found; needs ruled tables |
| Java-based table extractor | MIT, needs Java | not run | not verified |
| Mid-tier document converter | MIT / permissive table model | 6.6-13 s/page | a public OCR benchmark places it respectably but not top-tier |
| Small vision-language document model | Apache-2.0 | 196 s/page; 0/87 here | strong on its own model card's benchmark, poor here |
| Fast local layout converter | Apache-2.0 / modified-OpenRAIL, free under a revenue threshold | 1.7-3.4 s/page | mid-tier on public OCR benchmarks |
| Basic-then-VLM OCR pipeline | Open-source-with-thresholds licence | basic 4-7 s/page, standard 64-102 s/page | near the top of a public OCR benchmark leaderboard |
| GPU-only document OCR model | Apache-2.0 | not run (needs 12+ GB VRAM) | near the top of the same leaderboard |
| Additional OCR/table tools surveyed but not run | mostly permissive | not run | not verified |

Cloud-only tools were excluded by the owner and are reference points only:
the leading closed models score well on general OCR benchmarks, but none of
those benchmarks contains datasheets — they cover papers, financial reports,
newspapers, textbooks and notes. [inference] A published score says little
about MIN/TYP/MAX tables with merged sub-rows; the spike shows the ranking
can invert, with some open tools scoring above others here that outrank them
on public leaderboards.

## What this means for atlas

All of this section is inference from the measurements above.

1. **Keep a per-page text layer as the base of the conversion**, split on
   page boundaries, with word bounding boxes kept alongside. It serves as the
   raw tier behind every citation and the numeric oracle for every check. It
   costs about 3 seconds for a typical 9-document project.
2. **Datasheet stack.** Classify pages from the outline and header patterns,
   then pick a route per page type: prose pages to markdown from the text
   layer; register pages through a per-vendor-template deterministic parser;
   table pages through a structurer, then checks, then a fallback chain.
3. **Table fallback chain:** a tagged-PDF table reader when the tags exist;
   a permissively-licensed document converter as the default, splitting its
   packed cells back into sub-rows using text-layer line positions; a
   markdown-oriented converter if its licence is acceptable; Claude on the
   page image, given the page's text-layer token list and told every number
   must come from it; otherwise mark the table unverified and point at the
   page. Each step must pass the deterministic checks; any mismatch between
   two routes becomes a `conflict` fact.
4. **Canonical table form in the distilled markdown:** one sub-row per line
   with the symbol and unit repeated, and conditions made explicit. This
   removes the three reading hazards measured here: packed cells, fill-down
   errors and merged units.
5. **Reference manual stack.** The register-description file is the register
   source of truth, joined to reference-manual field prose by (register,
   field) with page cites. Reference-manual prose is sliced by outline, only
   for enabled peripherals. Figures (flowcharts, timing diagrams) go to Claude
   page images on demand. Never convert bit-grid figures with a generic
   table tool.
6. **Requirements-document stack.** Use the text layer plus its outline for
   structure, and Claude page images for the image-heavy pages. Distil
   requirements as cited lines with a page and heading.
7. **Do not adopt the weakest local converters as the default on CPU**,
   because of their measured value corruption and, for one of them, its
   remote and telemetry defaults. A GPU-accelerated tier can be perfect on a
   good table but costs 100+ seconds per page; Claude does the same job in
   about 20 seconds and is already the owner's runner.

## Open questions for the owner

1. Is a copyleft licence acceptable for a local, internal atlas tool? If yes,
   the markdown-oriented converter is the best single tool on two of the three
   spike tables. If no, the mid-tier MIT converter is the default.
2. What is the budget for the Claude image pass? About 2.7K input tokens per
   page; every page of a 100+ page datasheet is roughly 300K, only the
   flagged pages roughly 50-80K.
3. Is "one sub-row per line, symbol and unit repeated" acceptable as the
   canonical table form in distilled docs? It is an interpretation of merged
   cells, so each such table would carry a "written, then verified"
   provenance tag.
4. Can the project supply its MCU's errata sheet? None was available at scope
   time.
5. Should the full reference-manual text layer (roughly 1M tokens) be stored
   as the raw tier, or only the chapters for enabled peripherals?

## What could not be verified

Several converter modes and a few competing OCR tools were not run at all
(GPU-only tiers, one Java-dependent tool, a couple of niche extractors).
Reported benchmark numbers for the public leaderboards were read through a
summarizing fetch rather than re-checked against raw pages. The spike sample
is 15 pages and 3 tables, so a one-table difference between tools is not
statistically meaningful. Network traffic from one pipeline's remote default
was inferred by reading its code, not by capturing packets.

## Sources

- Public OCR/document-conversion benchmark leaderboards (OmniDocBench-class,
  olmOCR-bench-class): general document-conversion accuracy tables, not
  datasheet-specific.
- Tool project pages and model cards for each converter surveyed (licence
  terms, supported modes, published accuracy).
- A semantic table-extraction accuracy paper comparing PDF parsers by
  LLM-judge score.
- Claude vision documentation (image token formula, resolution tiers).
- Claude PDF-support documentation (text-plus-image token cost per page).
- Local package metadata for licence terms, and one tool's own source for its
  remote-endpoint default and telemetry gating.

## Verification (2026-09-29, independent)

An independent pass re-ran the scoring, checking and timing scripts from
copies, re-measured the document corpus with standard PDF tools, and opened
every cited page directly. Fixes are marked "[corrected in verification]" in
the body above. Confirmed: the plain-text extraction accuracy figures, the
converter failure-mode examples, Claude's native-reading accuracy and timing,
the register-file agreement count, all licence claims, and the one
pipeline's remote-default and telemetry code paths (traffic itself not
captured). Corrected: the claim that checks flag every catastrophic failure
(no threshold is set), the claim that licence/privacy rather than accuracy
disqualify tools (both matter), and a reference-manual per-chapter token
estimate that was low by a modest margin. One claim (reading order being
genuinely blind) is unverifiable after the fact.
