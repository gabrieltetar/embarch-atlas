# Cross-source join: a real board's pin/net map and mismatch report

**Status:** draft, 2026-09-29.

Territory: the joins behind the two first-proof artifacts (the pin/net map and the mismatch report).
Builds on an earlier survey, which settled the join key (MCU refdes + symbol pin name <-> Zephyr
`edtlib` port/pin) and the register join (code site -> datasheet register by address). Measured on a
real multi-board project's firmware and schematics; the whole pipeline re-runs in about 9 s wall.

## Headline findings

1. **Two Altium schematic-export PDFs already contain an exact netlist in their bookmark tree:** one
   board's export lists 505 parts / 1,291 pins / 242 nets, the other's 288 / 725 / 123, every pin on
   exactly one net, parsed in under 0.25 s with no geometry and no LLM.
2. **They also contain the whole BOM as JavaScript component parameters** (MPN, value, DNP, description,
   datasheet link): 780 of 793 parts mapped to refdes; a cheap text-layer heuristic agreed on 310/311
   passive values and 48/54 part numbers.
3. **The first automated map covers all 100 pins of the MCU package:** datasheet and symbol pin names
   agree 100/100, most I/O pins reach a part or connector, and a chunk of the pins cross to the other
   board through a connector whose mirrored mating rule was inferred from net-name agreement (30/31 for
   the mirrored rule vs 0/32 for the straight rule).
4. **Every consistency check that has its sources passed:** all I2C devices identified by address strap
   matched, all of their signal GPIOs land on the expected DT pins [corrected in verification], all DT
   resistor values match the BOM, all digital pinctrl functions match their net names, and all net names
   in DT comments match the netlist.
5. **The join reproduced an engineer's hand-written wiring notes mechanically:** signal pins, pull
   directions with refdes, crossed decoder outputs, shared interrupt lines and current-sense setpoint
   values all matched. One capacitance total needed a datasheet pass-through model for a current-monitor
   IC's shunt (a plain per-net sum undercounted it by 10 µF).
6. **The candidate mismatch list holds about 100 items, few of them actionable:** one net name that
   changes across the board-to-board connector, one hand finding the netlist contradicts, a couple dozen
   connected-but-unused pins, and a handful of pins claimed in the devicetree with no compiled driver.
7. **Semantic checks are blocked on document intake, not on algorithms:** only a minority of the roughly
   30 distinct part numbers on the two boards have a supplied datasheet. Missing are all of the small
   discrete-logic part numbers on the switch-control path and every discrete MOSFET.

## 1. Sources, and how trustworthy each join edge is

| Edge | Source | Method | Trust |
|---|---|---|---|
| pin -> net | Smart-PDF bookmarks (Sheet > Nets > NET > Pins > REF-PIN) | netlist parser, merge nets by name within a board | exact (the EDA tool's own compile) |
| part -> MPN/value/DNP | Smart-PDF JS component-property pop-ups + link-annotation position | BOM parser | exact; a small number of parts unmapped |
| pin number -> pin name | text layer: hidden per-pin token at each pin end + nearest same-row word in the body | heuristic | 100/100 on the MCU vs its own datasheet; loses overbars (an active-low bar reads as a plain label) |
| MCU pin number -> port | datasheet pin table, text-layout extraction | table parser | 100/100 rows |
| board-to-board connector mapping | net-name agreement under candidate mating rules | inference | mirrored rule 30/31 on the main connector, 8/8 on a secondary header |
| pin -> DT use | Zephyr EDT pickle pinctrl entries, every GPIO phandle-array, raw cells for binding-less nodes | DT extractor | exact per build |
| DT node -> driver | driver-compatible string in the compiled translation units; alias/nodelabel consumers; per-device-node ELF symbols | regex + nm | exact for "compiled", regex for "consumer" |
| DT prop -> IRQ armed | GPIO interrupt-configure calls plus one pointer hop to the DT-spec macro | regex, one pointer hop | spike tier; a proper AST tool is the real answer |
| part pin -> meaning | hand-transcribed datasheet cards for a current-monitor IC and a buck-boost IC, with page cites | transcribed, with page cites | stand-in for a datasheet-ingestion agent |

Details found on the way:

- **Altium hidden text tokens** carry component/pin/net-label/port anchors, dashes/underscores as digit
  placeholders; a pin token is ambiguous between adjacent designator numbers, checked against the
  bookmark pin list.
- **Multi-part symbols** (e.g. a dual-gate IC split into two halves) list pins with a per-half suffix and
  must be merged, or several logic gates vanish from the part list.
- **Refdes are per board:** the same designator can be a pull-up on both boards. Every key is
  `board:ref-pin`.
- **Build provenance.** The measured build was not built from the checkout's current commit or from the
  commit the spike started on. Its build-info metadata names an overlay file that exists on a different
  branch (also present on two other branches). The checkout used for HEAD comparisons was on yet a fourth
  branch/commit. So the spike joins **two DT snapshots**: the build's own pickle (35 MCU pins in DT) and
  a HEAD EDT regenerated with the SDK's C preprocessor (62 pins, 0.58 s, no full build system needed).
  Only the build has an ELF, so "compiled/instantiated" facts belong to the build, not to HEAD.

## 2. The pin/net map

One row per MCU package pin: number, port, I/O structure class, net, far end, DT use at HEAD (property,
flags or AF, whether the driver arms it as an IRQ, driver file), and the build's DT use when it differs.
There are three tables: the pin table; the I2C identity table (part by address, signals by net); and the
DT-value vs schematic-value table.

Size: 21.9 KB, **9.8K tokens** (o200k proxy). Two representative rows (with signal names replaced by role
descriptions, since the original net names are project-specific):

```
| pin 91 | port b, bit 3 | pull-up to 3V3 via a 10 kΩ resistor -> a small demux IC's enable input,
       reached via the board-to-board connector -> a load-switch driver's enable GPIO (driver not in build) | none |
| pin 99 | port b, bit 7 | pull-up to 3V3 -> two current-monitor ICs' shared open-drain alert pin |
       an I2C sensor driver's alert-gpios (active-low, IRQ armed), two device nodes sharing the line | same |
```

### Tracing the far end

- The trace follows the net.
- It crosses board-to-board connectors using the inferred mating rule.
- It passes one series resistor or ferrite bead (DNP parts block the path).
- It classifies each 2-pin resistor whose other end is a rail as a pull-up or pull-down, with its value.
- For small discretes it records where their other pins go, one hop.

### The I2C identity join

This is the strongest result. For every I2C node:

1. Take the schematic parts whose SDA pin is on the net of that bus's pinctrl SDA pin, including
   parts reached across a board-to-board connector.
2. Compute each part's address from its datasheet strap table and the net its strap pin sits on
   (address-select resistor values, ground/supply/SDA/SCL strap options for one family of parts; fixed
   addresses for two other parts).
3. Match the part to the DT `reg` property.
4. Walk each signal pin to the MCU (alert, fault, enable/UVLO through a FET stage, charge-status, and
   other status lines) and compare the pin it reaches with the DT GPIO.

Result: every I2C node matched to exactly one part, and every signal check agreed [corrected in
verification]. Each current-monitor part's sensed nets carry the same tokens as its DT node name.

**A worked multi-source chain (inference, for the report's style, not a finding):**

- An MCU output pin drives the gate of a small P-channel MOSFET, with the gate pulled to ground by a
  10 kΩ resistor.
- The MOSFET's drain feeds a buck-boost IC's enable/UVLO pin, pulled up by a 1 MΩ resistor.
- So before firmware runs, the enable pin is asserted.
- The output still stays off: the buck-boost IC's datasheet gives its output-enable register bit a reset
  value of 0.

The chain takes three sources: the netlist, the BOM and a datasheet register table.

## 3. The candidate mismatch report

About 100 candidates, roughly 12 KB / 4K tokens. Each carries its evidence, with the DT file:line and the
schematic refdes or net, plus what would settle it.

| Class | n | What it is on this project |
|---|---|---|
| cross-board-net-name | 1 | a net's active-low naming convention is dropped on one side of the board-to-board connector; naming only, DT flags are still correct |
| contradicted hand finding | 1, not in the count | see §4 below (a back-feed warning the netlist does not support) |
| connected-no-dt | ~20 at HEAD, ~50 in the build | a display connector's control pins, USB-PD CC/debug lines on a second port not yet in DT, a handful of one-off enable/mode pins, plus SWD and reset (expected) |
| dt-no-driver (build) | 4 | ADC, a GPIO-key input and the USB peripheral: DT nodes are okay, but no driver translation unit is compiled, so pinctrl is never applied |
| dt-no-binding (build) | 4 | four bare GPIO properties in the application's main function that `edtlib` leaves untyped; found only by reading raw DT cells |
| exti-line-shared | 5 | five external-interrupt lines each shared by two different GPIO ports (the interrupt-controller's line-select register can only pick one port per line) |
| pin-shared-in-dt | 6 | two current-monitor devices per shared alert line (wired-OR open-drain, by design per the netlist) |
| dni-on-path | 9 | several 0 Ω dead-battery link resistors and one divider resistor, all DNP by design |
| polarity-resolved | 1 | one active-low-named GPIO whose net name lacks the active-low prefix; resolved by tracing the LED's other pin back to a pull-up, confirming the pin sinks |
| extractor-missed (tool) | 6 | the owner's original pin-extraction script skips a boot-select pin and several left-column pins outside its assumed pixel window |

Checks that ran and found nothing: polarity vs net name (50 GPIO uses); open-drain far end without any
pull (25 pins, all pulled on the board); DT pull vs board pull conflicts; pull to a rail other than the
main supply; DT pin on an unconnected net; I2C bus-membership and address checks; DT resistor values (6);
pinctrl function (14 digital, 1 analog skipped); DT comments (31).

## 4. Scoring against a hand-written wiring-notes document (as the oracle)

A scoring script recomputes each item in the notes from the sources.

| # | Hand finding | Result | Source that made it possible, or is missing |
|---|---|---|---|
| 1 | MCU pin of each of 38 signals | reproduced 38/38 | bookmarks + datasheet pin table |
| 2 | Pull direction and refdes, 31 signals (both boards) | reproduced 31/31 | bookmarks + connector rule; values from BOM |
| 3 | A small demux IC's outputs cross to two different enable lines, across 5 device pairs | reproduced 5/5 | bookmarks (exact) + pin names. The engineer "verified by zooming each one"; in a 250 dpi render one output label sits on a trace easy to misread after a routing jog |
| 4 | Discharge = DISCHARGE AND NOT-enable, via two AND gates and four load-switch parts | structure 6/6 | gate I/O exact; the AND function needs a small logic IC's datasheet (not supplied; the BOM's own link points at a different manufacturer's part for the same footprint) |
| 5 | One load-switch IC's current-limit setpoint resistor, 5 parts | values 5/5 | current-rating comparison needs the datasheet formula: datasheet-ingestion territory |
| 6 | Port-discharge resistor value on a mid-bus net | reproduced | BOM |
| 7 | ~12 µF total on a mid-bus net (3 capacitors) | reproduced **only with a pass-through** | one capacitor sits behind a current-monitor shunt; per-net sum gives 2 µF, treating the shunt as one node gives 12.0 µF — the "transparent part" model an earlier survey described |
| 8 | A load-switch IC's disable and fault-timer pins grounded on all instances of one family | reproduced | bookmarks + pin names |
| 9 | Shared-interrupt-line table (5 GPIO pairs + shared alert lines) | reproduced | DT + interrupt-controller rule + driver-arming regex |
| 10 | A main-function bare-GPIO node lacks one enable signal and has no compatible string | reproduced | build DT (raw cells) + netlist |
| 11 | Current-monitor IC bus/address per switch block | reproduced (12/12 devices) | strap table from datasheet |
| 12 | "If the second board is connected, enabling the charger input back-feeds its 3V3 rail through the pull-up resistors" | **contradicted, candidate** | the second board's schematic shows no independent 3V3 source; its net holds only the connector pin and loads, mating to the first board's 3V3 under the inferred mirror rule (inference; needs the mating rule confirmed) |
| 13 | A schematic note claims a select-signal's polarity is backwards | surfaced, not judged | needs the demux IC's truth table (not supplied) and a read of the note against the actual trace |
| - | Fault-pin high-Z-while-disabled behaviour; missing enable pulldown on one family; timing constants; thermal margin; discharge-time estimates | not attempted | datasheet-only facts: datasheet-ingestion territory, then a DT-vs-datasheet limit check |
| - | Which of two conflicting review spreadsheets to trust | out of scope | owner: EE review sheets are not an atlas input |

Summary: 11 of the 13 checkable items were reproduced. The wiring items are exact, the values come from
the BOM, and one item needed a transparent-part map. One item was contradicted and one was surfaced
without a judgement. All the behavioural facts (timing, fault semantics, thermal) remain datasheet work.

## 5. Mismatch classes for Zephyr on this MCU family, and the sources each needs

| Class | Check | Sources | Status on this project |
|---|---|---|---|
| pinctrl/AF vs schematic | pinmux pin on the net carrying that function; far-end pin complementary | EDT, netlist, pin names, datasheet AF tables | 14/14 by net name (ADC skipped) |
| *-gpios polarity | active-low naming vs far-end active level, vs inverting stages | EDT, netlist, datasheet pin polarity (overbars lost in text) | 50 checked by name, 1 resolved by structure |
| pull / bias | board pull present/direction vs DT flags; open-drain needs a pull; rail vs I/O structure | netlist+BOM, datasheet I/O column | 25 open-drain lines pulled; one redundant DT bias-pull-up (info) |
| I2C address strapping | strap pin/resistor -> datasheet table -> DT `reg` | netlist, BOM, datasheet table | 12/12 |
| bus assignment | parts per family on the bus vs DT children | netlist, BOM, compatible-string map | 6 bus x family groups match |
| interrupt / EXTI sharing | GPIOs a driver arms, grouped by shared line | EDT, driver source, interrupt-controller rule | 5 shared lines, found by regex |
| register constants vs datasheet | code site -> (part, address, bits) vs datasheet; SVD for MCU | AST tool, datasheet tables, SVD | not re-run (prior: ~90% resolve, one real bug found) |
| DT limits vs datasheet/spec | current/voltage props vs abs-max and sense-scaled ranges | EDT, BOM, datasheet limits, PRD | resistor values agree; limits need datasheet-ingestion work |
| enable / sequence timing | delay props vs datasheet timing tables | EDT, AST, datasheet timing | not run; the notes' example delay is the test case |
| unused-but-connected / vice versa | netlist-reachable pins with no DT use, and reverse | netlist, EDT (per build) | ~20 / 0 at HEAD |
| AF conflicts | one pin in two pinctrl states; one EXTI line double-claimed | EDT | 0 conflicts; 3 shared GPIO pins (wired-OR, by design) |
| power domain / IO voltage | secondary rail; pull rail vs I/O structure; far-end supply vs MCU | netlist power pins, datasheet I/O, VIH/VOH | secondary rail on main supply; far-end check not built |
| DT-declared vs driver reality | node okay, no compiled driver; binding-less; alias-only | EDT, compile-commands, ELF | 4 + 4 in the build |
| hand annotations vs truth | net names in DTS comments | DTS + netlist | 31/31 agree |
| cross-board / cross-unit | net-name change across a connector; pass-through pins | two netlists + engineer-declared mating rule | 1 name change; unit-to-unit not modelled |
| boot-state / default | pulls + inverters + reset values give pre-firmware state | netlist, BOM, datasheet reset values | one buck-boost enable example (§2), by hand |

## 6. Prior art

| Work | What it checks | Board <-> firmware? |
|---|---|---|
| KiCad ERC | pin-type conflicts, unconnected pins, undriven power, within the schematic | no |
| A vendor pin-configuration tool's rule manual | MCU-internal signal/AF conflicts and remapping; no board-schematic input | no |
| A community devicetree lint extension (VS Code) | GPIO pin collisions, bus matching, pin owners from `gpios`/`-pins`/pinctrl | DT only, no schematic |
| Linux devicetree schema / `dtbs_check` | DTS conformance to YAML bindings | no hardware comparison |
| Zephyr edtlib | binding types and required properties | none; a source grep for "collision/conflict/overlap" finds only a hash-collision error |
| A commercial "Embedder"-class product | vendor claim: resolves netlist against datasheet/RM/SVD/schematic/errata; several EDA formats; on-prem | claimed; **no accuracy numbers published** |
| A pre-schematic interface-compatibility paper (2608.25217) | LLM + datasheets, interface compatibility: high accuracy, large context reduction | no firmware or DT |
| A driver-defect study (Ryzhyk et al., 2009) | hundreds of Linux driver defects: device-protocol violations are roughly a third to two-fifths of faults | motivates the code-vs-datasheet classes |
| Altium's Smart PDF export docs | bookmarks for pins/net labels/ports; component parameters | this is the input the spike exploits |

The prior survey found no shipping tool that checks devicetree/pinctrl against an EDA netlist. This pass
found nothing that changes that: the nearest are DT-only linters and one closed vendor claim.

## 7. What the full join additionally needs

- **From the schematic-geometry agent:** active-low overbars on far-end pin names (the text layer shows
  a plain label where the symbol has an overbar); note boxes attached to their nets; pin names for the
  roughly one-third to two-thirds of pins the row heuristic leaves unnamed (mostly passives); which sheet
  region a part sits in, for citations.
- **From the datasheet agent, per part number:** pin direction/open-drain/active-level tables; address
  strap tables; logic truth tables for the small discrete-logic parts; pass-through maps (a shunt
  resistor, mode-dependent switch paths, a buffer IC, FET stages); abs-max/recommended limits; timing
  tables; register reset values. Roughly 20+ part numbers have no supplied datasheet yet.
- **From the engineer (declared facts):** the connector mating rule, bench rework, what "unpowered" means
  for the second board, and which build/branch is the reference.
- **From the build side:** a HEAD build, so compiled/instantiated facts and the DT agree; a proper
  AST-based pass in place of the regex interrupt-arming extractor, over all translation units.

Could NOT verify: the connector mating rule (inferred); any fact about the physical boards; the BOM
mapping for the unmapped parts; a loose shunt-resistor finder with 2-3 candidates near the charger IC and
no way to say which the DT's sense-resistor property means; one buck-boost IC's open-drain-alert claim and
two other ICs' open-drain claims (not checked against their datasheets).

## What this means for atlas

All of the following are inferences.

1. **Make "Smart PDF bookmarks + JS parameters" the first schematic adapter, ahead of geometry or
   vision.** An Altium PDF export with net info gives the exact netlist and BOM in under 2 s. Geometry
   and vision are needed only for what the export lacks: overbars, notes, pin names. Intake should check
   a PDF for bookmarks and ask the EE for "Smart PDF with net information and component parameters" when
   they are missing.
2. **Key every schematic fact `board:ref-pin`, and give the connector mating rule its own
   engineer-declared record.** Show its agreement score when the tool proposes it. Multi-board is not an
   edge case here: a substantial fraction of MCU pins leave the main board.
3. **The mismatch report should report checks run and passed, not only candidates.** The valuable output
   is "12/12 devices, all signals, all comments agree." A list of ~100 candidates, most of them
   "connected-no-dt", would bury it. Rank by class, and fold the expected ones (SWD, reset, test-point
   only) into a count.
4. **Transparent-part maps are needed even for simple sums** (a shunt resistor hid 10 µF of capacitance).
   They belong in the datasheet card, tagged with a source page.
5. **Index two DT snapshots when build and checkout diverge**, and say which one each fact comes from.
6. **Document intake is a join product:** the BOM gives the MPN list, the supplied PDFs give coverage,
   and the diff is the engineer's to-do list with the schematic's own datasheet links. It is the natural
   "step 1" the owner asked atlas to own.
7. **Serve the map at about 10K tokens as an on-demand file, and a much smaller always-loaded slice.** A
   slice of `port net DT-owner first-two-far-parts` for the in-use pins is 3.7 KB / **1.6K tokens**.

## Open questions for the owner

1. The netlist says one board's main supply comes only from the other through the board-to-board
   connector. Is that right, and what did "second board not powered" mean on the bench? The back-feed
   warning in the wiring notes depends on it.
2. Is the inferred connector mating rule correct (the mirrored rule scores 30/31 on net names)? Should
   one net's naming be corrected to match the convention used elsewhere?
3. Which build is the reference for atlas: the branch the archived build was made from, or the current
   working branch? Should atlas trigger a configure-only build to get a fresh EDT per branch?
4. Can the EE export the remaining parts' datasheets (roughly 20+ part numbers, including the small
   discrete logic), or should atlas accept "known standard function" cards for jellybean logic, marked as
   engineer-declared?
5. Should the not-yet-wired-up USB-PD port and the display's unconnected-in-DT pins appear as "not yet in
   firmware" rather than as mismatch candidates?

## Sources

Local: spike outputs (netlists, BOMs, pin map, I2C join, mismatch candidates, notes scoring, datasheet-
coverage report); the project's schematic PDFs (two boards), MCU datasheet and RM, five component
datasheets, the wiring-notes document, the owner's own pin-extraction script; the archived build's
build-info metadata, compile-commands database, EDT pickle and ELF; one board-support overlay at a
specific commit; earlier internal notes on board-EDA joins, register joins, code-flow joins, a hardware
census and Zephyr build metadata.

External: https://embedder.com/ · https://github.com/NordicPlayground/vscode-nrf-devicetree ·
https://github.com/KiCad/kicad-doc/blob/master/src/eeschema/eeschema_design_verification_with_erc.adoc ·
https://www.linaro.org/blog/tips-and-tricks-for-validating-devicetree-sources-with-the-devicetree-schema/
· https://arxiv.org/abs/2608.25217 · http://ryzhyk.net/publications/Ryzhyk_CKH_09.pdf ·
https://www.altium.com/documentation/altium-nexus/workspacemanager-dlg-smartpdfsmart-pdf-wizard-ad

See also: [13-mcu-reference.md](13-mcu-reference.md), [16-landscape-update.md](16-landscape-update.md), [17-ingestion-procedures.md](17-ingestion-procedures.md).

## Verification (2026-09-29, independent)

Whole pipeline re-run into a temp copy of the spike: pin map, I2C join, mismatch candidates, notes
scoring and datasheet coverage came out byte-identical; the netlists differ only in timing metadata. Wall
time 22 s cold and 5.8 s warm, at load average 7.5.

| Claim | Verdict | Evidence |
|---|---|---|
| Netlists 505/1,291/242 and 288/725/123, one net per pin, bookmarks < 0.25 s | confirmed | re-run |
| BOM 780/793; heuristic 310/311 values, 48/54 MPNs | confirmed | recount; the 6 MPN misses are ferrite beads read as a different part family |
| Pin names 100/100; most reach a part; a chunk cross the connector | confirmed | 94 exact, 6 spelling variants |
| Mirror rule 30/31, straight rule 0/32, secondary header 8/8 | confirmed | recomputed from netlists |
| I2C: all nodes matched, all signal checks agree | corrected | 17 signal checks total, all agree, across 14 distinct pins |
| Resistor values, pinctrl, comments, counts (6/6, 14/14, 31/31, 38/38, 5/5, capacitance 12 vs 2 µF) | confirmed | JSON counters; capacitance matches one of 2-3 shunt candidates |
| ~100 candidates include the contradicted hand finding | corrected | the contradicted 3V3 finding is only in the notes-scoring output, not in the mismatch-candidate count |
| Second board has no independent 3V3 source | confirmed | its 3V3 net holds only the connector pin, passives and IC supply pins |
| Overlay file exists on more than one branch | corrected | confirmed on three branches total |
| 4 dt-no-driver items | confirmed | no matching Kconfig options enabled, no driver translation unit |
| Datasheet coverage fraction | corrected | one part previously marked "no datasheet" actually has one supplied but unmapped in the BOM |
| Register-page cites for the current-monitor, buck-boost and charger ICs; the enable-chain example | confirmed | pages and quoted values re-checked |
| Cited external papers' numbers; no DT-vs-netlist tool found | confirmed/unverifiable | abstracts re-checked; none found in this pass either |
