# 13 — MCU reference knowledge: reference manual, datasheet, SVD, errata, pins, clocks

**Status:** draft, 2026-09-29.

Territory: what an agent needs to know about an MCU itself, and which structured sources can supply it
without parsing a >1,000-page reference manual. Measured on a real Zephyr build for one project's
main board (a mid-range Cortex-M0+ class MCU in an LQFP100 package, referred to below as `<MCU>`).
Token counts use tiktoken `o200k_base` as a proxy for Claude's tokenizer, matching an earlier survey. See
also [16-landscape-update.md](16-landscape-update.md) and [17-ingestion-procedures.md](17-ingestion-procedures.md).

## Headline findings

1. The reference-manual (`<MCU RM>`) chapters the build actually uses total 388 of ~1,400 pages (235K of
   891K tokens, 26.4%), and 113K tokens (12.7%) once the register sections are left to the SVD (an XML
   register-map file the vendor ships).
2. The vendor SVD is not clean: a bit-level diff against RM register sections for three peripherals
   found 0 / 9 / 15 differences, of which 14 are SVD defects confirmed by a second source and 8 are
   correct omissions of features the family-wide RM describes but this specific part lacks; separately,
   one whole peripheral and one debug-register bit are missing from the SVD entirely.
3. SVD descriptions cannot replace the RM's field text: one peripheral's SVD field descriptions contain
   0 of the 71 constraint sentences ("only when", "must", "cleared by hardware") that the RM's field text
   for the same peripheral has (a second peripheral: 54 vs 143).
4. A Claude-written, page-cited distillation of one peripheral's chapter (card 1,522 tokens; full tier
   10.2K tokens vs 35.6K raw) had 6 distinct citation or faithfulness defects in 135 lines, which a
   deterministic checker flagged (7 flags on 6 lines) in about 3 s; its recall is unmeasured, since the
   checker itself defined the 6 defect classes [corrected in verification].
5. Pin and alternate-function (AF) data needs no PDF parsing: the vendor SDK's generated pinctrl file
   agrees with the datasheet AF tables on 393 of 394 AF entries, and all pinctrl pins the project's board
   uses agree across the datasheet, the generated file and the vendor's IDE database (its plain-GPIO pins
   were not checked) [corrected in verification].
6. The vendor's chip-configuration IDE database is already installed locally (thousands of per-MCU XML
   files). It holds a machine-readable clock tree and the DMA request map, so neither the RM clock-tree
   figure nor the DMA-mux tables needs parsing.
7. The applicable errata sheet (`<MCU errata>`) is not supplied; the Zephyr tree already applies its
   flash-prefetch workaround for this family (the relevant Kconfig default is unset in this board's build).

## 1. What a prior survey settled (not re-researched)

- Build metadata is roughly 85% free: the EDT pickle, config-trace JSON, compile-commands database and
  the per-device-node ELF symbols give the devicetree → driver → ELF join.
- The SVD is the MCU register source. The join key is (instance, address, bit range), never a name. SVD
  slices cost about 1K tokens per instance.
- Deterministic, template-driven extraction beat LLM extraction for register maps on a different part
  (126 of 126 registers width-consistent). LLM output must pass the same checks.
- On a different MCU family in the suite, peripheral base addresses are not unique (20 shared bases). On
  `<MCU>` every peripheral base is unique except one analog-reference peripheral, which sits inside
  another peripheral's 0x100 address block.

## 2. Reference-manual outline and cost

A table-of-contents extractor on `<MCU RM>` found: ~1,400 pages, 1,965 outline entries (43 chapters; 688
are table/figure entries, which must be filtered out or they break section splitting, as they did once
here). The PDF page number equals the printed page. Text extraction of all pages takes about 1-3 s.
Totals: 890,530 tokens, 643 per page on average; the front matter (contents, list of tables/figures) is
98,184 tokens. The largest chapters are the three general-purpose timers and the two serial-communication
peripherals, each 40-100+ pages / 40-57K tokens. Datasheet: 165 pages, 97,822 tokens.

## 3. What the build uses, mapped to chapters

In-use status has to be tracked in three states, because each state gives a different answer:

| State | Peripherals | Evidence |
|---|---|---|
| DT okay **and** device in ELF | two I2C buses, three UARTs, six GPIO ports, external interrupt controller, clock controller (+ reset), flash controller | grep for per-device-node ELF symbols in `nm` output |
| touched by SoC/arch code, no DT device | system config mux, debug block (ID/control), NVIC/SCB/SysTick/MPU | SVD base addresses found in literal pools or const data |
| DT okay, **no driver** | ADC, USB-C port controller, USB device | edt status okay, no device symbol in the ELF |

The client firmware has **0 direct register accesses**. A grep for low-level-library calls, HAL calls,
raw peripheral-struct dereferences, `sys_read32` and literal peripheral-base addresses across the whole
application tree found only a QEMU board's devicetree. Every MCU access goes through 18 compiled
Zephyr/HAL translation units. A literal-pool/const-data scan also shows two traps for "is it used": the
UART driver's literal pools reference every UART/LPUART instance base (an instance-list, not evidence of
use), and libc float tables coincidentally overlap two peripheral base addresses. So a code-side reference
is only evidence when it sits inside a device config struct.

Budget:

| Group | Pages | Tokens | % of RM | Prose only (register sections removed) |
|---|---|---|---|---|
| driver-instantiated (I2C, UART, GPIO, EXTI, clock ctrl, flash) | 295 | 176,667 | 19.8% | 86,513 |
| SoC/arch-touched (sys-config mux, debug, NVIC) | 40 | 25,124 | 2.8% | 5,447 |
| cross-cutting context (memory/bus/boot, power) | 53 | 33,561 | 3.8% | 20,713 |
| **core set** | **388** | **235,352** | **26.4%** | **112,673** |
| DT okay, no driver (ADC, USB-C PD controller, USB) | 146 | 87,233 | 9.8% | 43,168 |
| off, needed once DMA is on | 37 | 25,382 | 2.9% | 11,560 |

Always-needed cross-cutting chapters: clock control, GPIO, external-interrupt, flash and the interrupt
controller are all in the build. The power-management peripheral is touched only through its
clock-enable bit; DMA/DMA-mux are off. The system-config mux shows the limit of "prose only": that
chapter is *entirely* register sections, and its behaviour (a dead-battery strobe, pin-remap bits) lives
in field text.

## 4. Structured sources: what each gives, and what it gets wrong

| Source | Local? | Gives | Quality found here |
|---|---|---|---|
| Vendor SVD (XML register map) | yes, 2.45 MB, 536K tokens raw | 56 peripherals (34 base + 22 derived), 510 registers, 3,344 fields, 3,371 enum values, 31 IRQs | defects in §5; missing one peripheral entirely; compact slices of all in-use blocks total 8,423 tokens |
| Vendor CMSIS device header | yes, 863 KB | base addresses incl. the missing peripheral, bit-position/mask constants for every bit, IRQ enum | agreed with the RM on every SVD defect checked; has its own bug on one bit-flag name; no reset values or access types |
| Vendor chip-configuration IDE database | yes, vendor's own licence, thousands of per-MCU XML files | per-package pins + signals, GPIO alternate-function modes, DMA request map, IP versions, **clock-tree graph** | agrees with the datasheet on 386 of 387 AF cells [corrected in verification]. The multi-hundred-MB installer could not be unpacked, so the already-installed copy was used |
| Vendor's open pin-data project (permissive licence) | clonable | the pin/AF subset of the IDE database, per MCU | generator input for the SDK's generated pinctrl files |
| SDK-generated pinctrl file | yes | 530 pinctrl nodes (394 AF, 136 analog) | 393 of 394 AF entries agree with the datasheet (§7) |
| A community per-chip data project (public JSON, embedded-Rust ecosystem) | public JSON | per chip: peripherals with clock-enable/reset/kernel-clock bits, pins+AF, IRQ signal mapping, DMA request numbers, doc IDs; curated register YAML | fixed one bit-width bug present in the SVD; kept one missing IRQ-enable bit and one register-name typo; lists one clock mux with a value the RM and IDE clock tree contradict |
| A community SVD-patching project (public, patches feeding a register-access-crate generator) | public | patched SVDs | this MCU's patch file has dozens of includes (roughly half are patch files). The clock-control and interrupt patches read are renames and enum additions; they add no missing peripheral and fix none of §5's items (not checked against the generated code) |
| A document-to-structured-data pipeline project (permissive licence) | pip | PDF-to-HTML for vendor RMs/datasheets, HTML-to-SVD, IDE/header/SVD merge into a knowledge graph (peer-reviewed pipeline paper) | on a 65-page RM span: under 15 s, hundreds of MB, dozens of tables, heading tree and page anchors intact, with no manual patch needed for this document (it ships dozens of hand-patch files for other vendor documents) |

Which source replaces PDF parsing, per fact class:

| Fact class | Primary source | Second vote | PDF still needed for |
|---|---|---|---|
| Register offsets, bit positions, reset, access | SVD | CMSIS header (positions) + RM register sections | adjudication only |
| Field semantics and write constraints | none structured | — | RM prose, distilled (generated, not paraphrased) |
| Programming sequences, timing formulas, modes | none | driver code as compliance evidence | RM prose, distilled |
| Which features exist on this part | RM's device-variant table (parsed deterministically) + SVD presence + RM "only available on this part" notes | IDE database's per-MCU IP list | — |
| Bases, IRQ numbers, IRQ sharing | header, SVD | community per-chip data | — |
| Pins, AF numbers | open pin-data project / IDE GPIO data (= SDK pinctrl file) | datasheet AF tables | datasheet pin-table footnotes (dead-battery pull-down, remap notes) |
| Clock tree, kernel-clock muxes | IDE clock-tree XML | RM clock-control chapter | limits (max frequencies) → datasheet |
| DMA request numbers | IDE DMA XML / community per-chip data | RM DMA-mux table | — |
| Electrical limits and AC timing | none | — | datasheet electrical chapter |
| Errata | none (Zephyr source encodes some) | — | errata sheet |

## 5. SVD vs RM register tables (three peripherals)

A deterministic register-section parser handles each RM register section: offset, reset (with unknown
digits masked), per-bit field names (expanding repeated-field notation and parametric register-array
names), and per-bit access taken from the bit-diagram rows. It checks itself: every bit is covered, and
the access-token count equals the non-reserved bit count, for all 50 sections across the three
peripherals (53 register instances once one parametric register expands). A second script diffs them
against the SVD bit by bit, and a third takes a vote from the header and the RM's device-variant table.

| Verdict | Count | Items |
|---|---|---|
| identical (one peripheral) | 11 regs | offsets, resets, all field bits, access |
| SVD defect, header agrees with RM | 5 | two enable/mask registers omit three individual line bits; one clock-control status register omits an interrupt-enable bit; one PLL config field is 8 bits in the SVD where the RM and header say 7 |
| SVD naming defect | 8 | one register's bits named by bit index instead of external line number (×4); three PLL-ready bit names disagree with RM/header naming; one enable-bit name has a typo |
| SVD defect candidate, RM is the only other source | 2 | one mask register's reset value disagrees (RM itself also prints a typo in that value); one clock-ready bit's access type disagrees |
| SVD naming defect, header also wrong | 1 | one flag-register bit name is a copy-paste of its rising-edge counterpart, in both the SVD and the header |
| correct omission (RM is a family superset) | 8 | crypto/RNG-related bits in four registers; the RM's device-variant table marks that functionality absent on this part |
| outside the three peripherals | 2 | a clock-recovery peripheral absent from the SVD entirely (present in the header and the device-variant table); one debug-freeze bit absent from the SVD |

Two consequences. First, the RM describes a whole family of related parts, so every RM-derived fact
needs a device filter: the device-variant table, SVD presence, and per-bit notes such as "only available
on parts X and Y". Second, the SVD is structurally right about most things but thin on meaning:

| Peripheral | RM field text tokens | SVD description tokens | constraint phrases RM / SVD |
|---|---|---|---|
| serial bus 1 | 6,610 | 1,882 | 143 / 54 |
| external interrupt controller | 4,140 | 1,500 | 10 / 0 |
| clock controller | 10,076 | 1,306 | 71 / 0 |

The SVD descriptions for one serial peripheral also still use an older pair of role terms where the
current RM revision uses updated terminology, so text search has to try both.

## 6. A tiered peripheral distillation, checked

Tier 0 is a 1,522-token feature card: instances, bases, IRQs, clock mux, clock-enable bits, DMA-mux
numbers, register list, five must-knows, and a section index with token costs. Tier 1 covers
init/timings/reset, data transfer, controller mode and register-value examples in four files (4,293
tokens), plus a must-notes file (2,170 tokens: six sequences, 8 must-notes, 6 quirks each checked against
a second source). Tier 2 (2,262 tokens) is **generated**: 59 constraint sentences lifted verbatim from
the RM register sections and keyed by `REGISTER.FIELD`. A board-specific join is kept in a separate file,
because the distillation itself is board-independent.

Two optional modes were skipped in this sample because the relevant Kconfig option is unset. Every line
cites a section and page; register and field names are the SVD's own names.

**Cross-check (deterministic, about 3 s [corrected in verification]).** Three checks: every backticked
`REGISTER.FIELD` exists in the SVD; every field named on a cited line appears in the text of a cited
page; every section reference contains its page. A fourth check requires the numbers on a cited line to
appear on the page.

The first pass covered 135 lines, 158 cites and 149 SVD names. It raised 7 flags on 6 lines [corrected in
verification], which were 6 distinct defects: two insufficient page cites; one embellishment (naming a
specific status bit where the RM only describes a general condition, made twice); one SVD-only field name
attributed to an RM page; two flag-clear-bit names that the RM contradicts itself on between its summary
table and its register section (the register section and the SVD agree with each other).

After fixes the checks pass: 0 of 131 field-on-page failures. The number check flags 8 of 96 numbers, all
explained: SVD-sourced numbers on mixed-source lines, derived restatements ("8-bit" from a bit range), and
one board-specific clock value, which was removed. The checker only proves that names, pages and numbers
line up; it does not prove that a sentence is true (inference: that needs a second LLM pass or a human).

**Deterministic tier 2 is possible.** The document-to-structured-data pipeline's HTML of the same
chapter, converted with a markdown converter, is 34,567 tokens, keeps real table structure (one feature
table survives that plain PDF-text extraction mangled), and has page anchors. So "full distilled
markdown, section by section" can be produced without an LLM, and the LLM tier is the compression on top.

**Page-on-demand cost.** Reading a single RM page through the coding assistant's file tool returned one
image and no text layer. By the vendor's own vision-token formula that page costs about 1,260 visual
tokens. Plain text extraction gives 708 tokens for the same page, and the page's facts appear in the
generated constraint file at roughly 150 tokens (inference). Reading the whole RM that way would cost
about 1.74M tokens. Through the API, a PDF page costs 1,500-3,000 text tokens plus the image.

## 7. Datasheet pinout and AF vs generated pinctrl

A table extractor pulled the datasheet's alternate-function tables (the pages are rotated 90°, and
underscores render as separate glyphs, which needed a normalisation step). Result: 94 GPIOs (one has no
AF), 422 pin/AF cells, 466 signals. Against the SDK-generated file's 394 AF nodes: 331 exact, 62
naming-convention equivalents, and **1 real disagreement**: one pin's alternate function for the USB
transceiver's output-enable signal. The generated file, the IDE database and the community per-chip data
project all list it; the datasheet's AF table and its separate pin table do not. The disagreement is
unresolved; it is the kind of fact atlas should emit as a `conflict`. Against the community project, 386
of 387 database cells are in the datasheet; the 36 datasheet-only cells are system signals (event-out,
clock-recovery sync, audio-clock-in, IR-out, debug pins).

All pinctrl pins used by the project's board agree across datasheet, generated file and IDE database (its
plain-GPIO pins carry no AF and were not checked [corrected in verification]): 10 AF pins across the two
I2C buses and three UARTs, and 5 analog pins (one ADC input, two USB-C CC pins, two USB D+/D− pins).

## 8. Errata

The applicable errata sheet covers this part against the RM and the datasheet. A sibling sheet for a
closely related part does not apply here. It could not be opened (two fetches timed out), so its revision
and contents are unverified; it was not downloaded into atlas.

The Zephyr tree already encodes this errata sheet's knowledge:
- A SoC-family Kconfig default disables a flash-prefetch option for this device family (a documented
  prefetch-across-flash-banks failure → hard fault); the project's build has it unset as expected.
- The I2C driver carries two unnamed errata workarounds for spurious bus-error and stalled-transmission
  conditions in controller mode.

These are machine-findable by grepping the compiled translation units for "errata", so atlas can surface
errata the tree already acts on even before the sheet is supplied (inference).

## 9. Findings that should make an agent curious (from the joins)

- **Dead-battery pull-downs on the USB-C CC pins are removed at boot.** The datasheet says a pull-down
  may be present on those pins after reset; the RM says a system-config strobe bit disables them. The
  SoC init code strobes both USB-C port controllers whenever a certain Kconfig option is off, which it is.
  Whether the board relies on those pull-down resistors before that point is unknown (open question).
- **The PLL clock source is marked disabled in the devicetree but works anyway,** because the internal
  oscillator this MCU resets with is already enabled by hardware reset default — inference about why the
  build succeeds despite the apparent devicetree contradiction.
- **One serial-bus kernel-clock mux bit carries a name from an unrelated peripheral family** on this
  chip, and the community per-chip data project gets its selected value wrong; the project's firmware
  programs neither clock mux, so both I2C instances run from the peripheral bus clock at a speed the RM's
  timing tables do not cover (inference from the devicetree).
- A flash-load-offset setting and a code-partition setting appear to disagree with how the devicetree
  declares the bootloader partition. This may be intentional (no bootloader yet); it is noted for the
  mismatch report, not investigated.

## What this means for atlas

(All inferences.)

1. **The MCU layer is built structured-first.** SVD + CMSIS header + vendor IDE database (or the
   permissively-licensed pin-data project where the vendor's own licence is a concern) cover registers,
   bases, IRQs, pins/AF, DMA requests and the clock tree with no PDF parsing. The RM contributes three
   things: (a) field constraints, generated verbatim from register sections; (b) distilled prose of
   in-use chapters only, about 113K raw tokens for this project, compressing to about 10K per peripheral
   at tier 1; (c) page-on-demand at tier 3.
2. **Register facts need a three-source vote, emitted as `conflict` facts.** 14 of 24 differences found
   across three peripherals are genuine SVD defects. The header is the cheap third voter for bit
   positions; the RM is the only voter for reset values and access types.
3. **"In use" is a three-state fact** (DT okay / device instantiated / touched by SoC code), stamped per
   build. The "DT okay, no driver" state is itself a mismatch-report item.
4. **The trust bar works cheaply.** An LLM writes the distillation; deterministic name/page/number
   checks catch the typical defects at no model cost. A second LLM pass is only needed for sentence truth.
5. **Device filtering is mandatory for any RM-derived fact.** Parse the RM's device-variant table and the
   per-bit "only available on" notes, and intersect with SVD presence.
6. **The document-to-structured-data pipeline is the best candidate for a deterministic RM/datasheet →
   section-markdown step.** Its page anchors are kept, its tables are structured, it is permissively
   licensed and it needs no network.
7. **Tier 3 costs about 1.26K tokens per page as an image,** so "raw page on demand" is fine for single
   pages and wrong for whole chapters. Any lexical index over pages should key on page numbers so a hit
   maps to one read.

## Open questions for the owner

1. Can the errata sheet for this MCU (latest revision) be supplied? It is the only MCU document missing.
2. Does this board rely on the MCU's USB-C dead-battery pull-downs (for example, a USB-C source must see
   a pull-down before firmware runs)? The build removes them at boot.
3. Are the ADC, USB-C PD controller and USB device enabled in the devicetree on purpose, as placeholders
   for future drivers, or is that drift? It decides whether they go in the mismatch report.
4. Is using the locally installed vendor IDE database (vendor licence) acceptable as an atlas input, or
   should atlas limit itself to the permissively-licensed pin-data project plus the SVD/header, which lose
   the clock tree and DMA map?
5. For RM distillation, should in-use peripherals get every section (optional modes included), or only
   what the build's Kconfig can reach?

## What I could not verify

- Errata sheet contents and revision (fetch timed out twice).
- Whether the community SVD-patching project's generated register-access code fixes any §5 item.
- Per-call token cost; the figure comes from the documented formula, not a meter.
- The IDE database inside the large installer package. The already-installed copy was used instead.
- Semantic correctness of the distilled sentences beyond the name, page and number checks.
- Tokens are o200k proxies, not Claude's tokenizer.

## Sources

External: a Zephyr issue tracking this MCU family's prefetch erratum; a community per-chip data
project's README/generated JSON; a community SVD-patching project's README and per-chip patch file; the
SVD-patching toolchain's own README ("vendor-supplied, often buggy SVD files"); the permissively-licensed
open pin-data project ("subset of the vendor's internal pin database"); the document-to-structured-data
pipeline project's README and predecessor; an SVD-metadata aggregator project (last push 2025-01-05); a
small MCP server offering PDF-based RM search; Claude PDF-support and vision-token-formula docs.

Local: the vendor RM and datasheet PDFs; the vendor SVD; the vendor CMSIS device header; the SDK's
generated pinctrl file and README; the SoC-family source and Kconfig defaults; the I2C and clock-control
driver source; the project's build artifacts (EDT pickle, `.config`, ELF, devicetree, compile-commands
database); the vendor IDE's local chip/IP/clock database.

## Verification (2026-09-29, independent)

Scripts re-run into a scratch directory (originals untouched); SVD, RM, header and DT read directly;
public sources fetched. Load average about 7.5, so timings are noisy.

| Claim | Verdict | Evidence |
|---|---|---|
| RM: ~1,400 pp, 1,965 entries, 890,530 tok; core set 388 pp/235,352/26.4%/112,673 prose | confirmed | outline/chapter-usage scripts re-run identical; extraction took 3.0 s, not 1.2 s |
| SVD vs RM 0/9/15; 14 defects, 8 omissions; 1 missing peripheral/debug bit; 56 periph, 510 regs, 3,344 fields, 31 IRQs | confirmed | diff re-run byte-identical |
| Constraint phrases RM/SVD | unverifiable | phrase set not scripted; independent regex counts landed in a similar range |
| Checker: 6 defects/135 lines, "all caught", < 1 s; tiers 1,522/10.2K | corrected | re-run: 158 cites, 149 names, 7 flags on 6 lines; after fixes 0/131; 2.8 s CPU; "all caught" is circular; tiers total 10,247 tok |
| Datasheet vs generated file 393/394, one AF conflict | confirmed | 331+62+1 breakdown; datasheet's own tables disagree with each other |
| "All 15 pins" the build uses agree | corrected | 15 is only `pinctrl-0` pins; 20 plain-GPIO pins unchecked |
| IDE database version, MCU-XML/clock-tree counts | corrected | differ slightly once non-MCU housekeeping files excluded |
| Community per-chip data project fixes/bugs; errata identity; Zephyr prefetch default | confirmed | re-checked against live files; closed upstream issue cites the same section |
| Read(page)=image, no text; vision formula; PDF text-token range | confirmed | one page read matches vendor docs |
| Document-to-structured-data pipeline: licence, output, table count | corrected | timing differed (4.9 s vs 14.6 s); output identical; no manual patch existed for this document |
| 0 direct register accesses; config structs hold the bases | confirmed | re-grep: QEMU devicetree only; ELF structs hold expected addresses |

Also confirmed: star counts/last-push dates for cited community projects; PLL source from a
reset-default-enabled oscillator; the dead-battery strobe behaviour.
