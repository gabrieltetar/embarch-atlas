# Schematic PDF to netlist, sheets to boards to product

**Status:** draft, 2026-09-29.

Territory: PDF schematic to netlist/graph, multi-board products. Spike on a
real two-board product's schematic PDFs (one 7-page board, one 10-page board,
both exported by a common EDA tool's "manufacturing package" PDF feature).
Builds on a prior board-EDA survey that ranked "Smart PDF with net bookmarks"
7th of 8 sources and treated PDF as fallback-only. Every board fact below is
**extracted** from the two PDFs, never confirmed by the engineer.

## Headline findings

1. **These PDFs already contain the whole netlist, so no ML is needed to
   read them:** the outline has thousands of bookmarks per board listing,
   sheet by sheet, each net's pins, ports and net labels, and a JavaScript
   popup gives every part's parameters. The reader recovers 793 physical
   parts, 2,016 pins and 365 board nets across the two boards, puts every pin
   on a net and gives a manufacturer part number for all 475 parts that have
   one, in about 5 seconds.
2. **A wire tracer that uses only the drawn geometry reproduces the embedded
   netlist for 526 of 529 child-sheet nets, with 0 false pin pairs.** The 3
   misses are the 3 pins (of 2,016) that have no anchor word. Two caveats:
   the tracer was tuned over 7 iterations on these same two files, and it
   still takes pin identity from the EDA tool's invisible anchor text.
3. **A PDF with no embedded netlist is much harder, and pin identity is
   where it breaks:** 141 of 945 pins (15%) [corrected in verification] on
   parts with 3 or more pins have neither a pin-number nor a pin-name text
   next to them. Published numbers agree: the best vision-language model in
   one public benchmark reaches 0.39 net accuracy on real PCB schematics, and
   a research tool reaches F1 0.42-0.56 on vector PDFs.
4. **Nets must be joined across boards through a declared connector pin map,
   not by net name.** A reversed mapping (pin *n* on one connector to pin
   33-*n* on its mate) explains 30 of 31 named signal-pin pairs on the two
   boards' mating connectors; a straight identity mapping explains 0.
   Joining by net name alone would wrongly merge 5 distinct net pairs that
   happen to share a name, and would miss a genuine cross-board mismatch
   where the same signal is named with and without an active-low prefix on
   its two sides.
5. **Claude vision (the Read tool, page shown at reduced resolution) read
   IC pin-to-net slices perfectly: 75 of 75 pins across three components.**
   It is weaker when a net has to be followed along wires: on the densest
   page, 20 of 26 nets had exactly the right members, and 5 of the 6 wrong
   nets were ones it had itself marked low-confidence.
6. **The compact text formats are small:** the whole product is about 27.5K
   tokens (10.7K for nets, 16.8K for parts), against 731K for the raw
   embedded-netlist JSON. The text for one sheet (2.0-5.4K tokens) costs
   about as much as one page image (2.2-3.7K visual tokens), and a
   representative MCU-pin slice is 2.1K tokens.
7. **The PDF's component popups double as the datasheet checklist:** they
   list 31 distinct active-part manufacturer part numbers, and only 7 of
   them have a datasheet already on hand. The other 24 are missing, 11 of
   them ICs.

## 1. What the two PDFs actually carry

Both are exported by the same EDA tool's "manufacturing package" PDF feature
(large-format pages, embedded JavaScript). Measured with a PDF library:

| Layer | What is there | Board 1 | Board 2 |
|---|---|---|---|
| Outline (bookmarks) | Top sheet with child sheets. Under each sheet: components by reference designator, nets with their pins/ports/net-labels, and ports. Each leaf's action chains a JavaScript find-word call | thousands of entries, 7 sheets | thousands of entries, 10 sheets |
| Invisible anchor words | Hidden text in a specific render mode giving one code per pin, one per component, one per net label, one per port. A pin anchor's bounding box spans the pin line, giving position and direction | hundreds of pin anchors | over a thousand pin anchors |
| Link annotations | One per component body, a script call that pops up manufacturer part number, footprint and value | all matched to their components | all matched to their components |
| Go-to links | Sheet-symbol and port cross-references | present | present |
| Not present | Power-port objects as distinct objects (only as net names), sheet entries, pin names in the outline itself, variant/fitted state, harness objects | | |

- **Pin names are not in the outline.** A separate step takes the visible
  word just past the body end of each pin anchor. The main MCU had 100% of
  its pins named this way. ICs overall were mostly named; small transistors
  showed almost no names, because their symbol style hides them.
- **A few pins have no anchor word.** These are the second pin of a stacked
  same-name pin pair. The outline still lists them in their net, so the
  outline netlist stays complete.
- **Multi-part symbols.** A gate package split across two symbol halves in
  the outline is folded back into one physical component, since nets always
  reference the physical reference designator.
- **Pictures, not vectors.** One board's connector-orientation diagram is a
  raster picture with no text layer; the other board has a broken picture
  placeholder in the same spot. Text extraction ignores both; a vision
  reader could mistake such a picture for a real symbol [inference].
- **The anchors leak into plain text tools.** Roughly 29% of the words a
  bounding-box text extractor returns on one board's PDF are anchor words,
  not schematic content. An existing hand-written pin-extraction script had
  15 of its 88 output lines polluted by a stray anchor token.
- **Other EDA tools.** A competing EDA suite's PDF export has its own
  reference-designator and net bookmarks per its app notes. A third open-source
  tool's PDF export carries a table of contents of sheets, symbols and
  hierarchical labels, with popups resolving symbol fields and net names, but
  no pin list per net — connectivity there needs geometry (from its own docs,
  not tested here).
- **Libraries.** A general-purpose PDF library gave drawings, text with
  render mode and colour, links, the raw outline and JavaScript, fast. A
  table/layout-focused library gave lines, rects, curves and characters but
  no render-mode information, ignored the JavaScript links entirely, and ran
  an order of magnitude slower. A bounding-box text tool was fastest but
  mixed anchor words into its output.

## 2. How schematic objects look in the vector stream (measured here, one EDA tool's defaults)

| Object | Geometry in the PDF | Rule used by the tracer |
|---|---|---|
| Wire | Stroked polyline, one dark colour, thin stroke | Segments; connect only where an endpoint lies on another segment (T or end); plain crossings never connect |
| Junction dot | Filled circle | Counted, not needed (the endpoint rule already covers it) |
| Pin | One thin segment in the symbol's own colour, varying by component class, greyed when not fitted; a test-point pin is very short | Candidate = any non-wire segment in a plausible length range overlapping the anchor; prefer the one touching a wire |
| Pin number/name | Black text above the pin line or inside the body | Name = first word within a small distance past the body end, along the pin axis |
| Net label | Text whose baseline start sits on a wire, colour-coded by signal class on the densest sheet | Any non-black, non-wire-colour word whose baseline start lies on a wire |
| Port | Filled polygon with a coloured border, name inside | Wire endpoint on any polygon edge (matching only at polygon vertices missed a third of connections on one sheet) |
| Power port | Coloured strokes near a coloured name; ground drawn as a stem plus bars that do not touch | Group same-colour strokes within a small bounding-box tolerance; hotspot = a group end on a wire end or pin; name = nearest unused word within a small radius |
| Hidden power name | Some ground symbols on the MCU sheet have no visible text | Named after the visible bar-style ground symbol on the same sheet [inference; scored correct here] |
| Not fitted (do-not-populate) | A cross mark plus the part drawn in a muted colour | Both kinds of evidence recorded; a couple dozen parts flagged across both boards |
| No-connect mark | A small mark at a pin | Counted, not used |
| Net colour highlighting | Coloured strokes under a wire on a few power/signal nets | Ignored (width filter) |

## 3. Geometric extraction, scored against the embedded netlist

- **Method:** union-find over wire segments, pin hotspots, labels, port
  edges and power-port groups; same name on one sheet means the same net.
  Pin identity comes from the anchors.
- **Score, per sheet, with the outline as truth.** Child sheets: 170 of 171
  nets exact on board 1, 356 of 358 on board 2. Pair precision is 1.000 on
  every child sheet (0 false pairs), and recall is 1.000 except for the 3
  anchor-less pins. Net-name-exact match is lower (roughly 96% and 93%), with
  the misses being the EDA tool's own auto-generated net names plus the
  top-level block-diagram sheets, which are not modelled (sheet symbols and
  entries are drawn like ports there, producing the only false pairs seen).
- **Time.** A few seconds per board.
- **What broke during tuning.** Each item below was a wrong rule until these
  PDFs showed otherwise: pin lines in non-black colours; pin lines shorter
  than expected; designer-coloured net labels; ground bars that do not
  touch; port connections in the middle of an edge, not just at vertices;
  power names tens of points away from the symbol.

  A PDF from another tool, or from the same tool with a different house
  style, will break different rules. There is no held-out PDF in this spike
  [inference: expect a similar round of tuning for each new house style].
- **With no anchors** (a separate experiment simulating a PDF with no
  embedded netlist), for pins on parts with 3 or more pins: a pin-number word
  sits within a small distance for a majority of pins, but for a large
  fraction of those a second number is also nearby, because of tight pin
  pitch; 141 of 945 pins have neither a number nor a name nearby at all. So
  without an embedded netlist, pin identity needs a placement rule per
  symbol style, or a datasheet pin table matched by pin name. Two-pin
  passives mostly do not need it.

## 4. Coverage (outline reader plus join)

Across the two boards: 793 physical components (over a third are test
points), 2,016 pins, all placed on a net (all but a handful placed on a
*named* net), 365 board nets (a few dozen single-pin/no-connect nets), 475
parts carrying a manufacturer part number, and no ambiguous within-board
joins — every net name that appears on more than one child sheet either has
a port there or is a recognized power-port name.

## 5. MCU pin slice vs. a hand-written extraction script

A comparison script checked the main MCU (a 100-pin LQFP package) against an
existing hand-written pin-extraction script's 88 output lines:
- Most pins have a GPIO-style name; the great majority agree exactly once
  leaked anchor words are stripped out.
- A handful differ only because the hand-written script appends a
  test-point designator to the net name where the schematic-derived reading
  keeps the bare net name — the designer labels unused pins with their own
  name plus a nearby test point.
- A few pins are missing from the hand-written script entirely: these are
  the boot-select pin and the crystal-oscillator pins, whose longer names
  start to the left of the script's fixed text-column window.

That leaves **0 real net disagreements**. The schematic-derived slice also
returns the far-end parts for each pin, and the cross-board partner where
there is one — for example, one I2C data line traces through a connector pin,
a pull-up resistor and several IC pins on the local board, then across to the
mating board's same-named net.

## 6. Joining sheets, boards and units

**Within a board.** A child-sheet net joins the top-level net through its
port (the EDA tool requires the port name to equal the sheet-entry name). A
net name that appears on more than one sheet without a port joins only when
a power port of that name was seen on that sheet. On these two boards there
was nothing to flag: every child net carrying a port had a port of the same
name.

**Across boards.** An interface is a pair of connectors plus a pin map. Two
hypotheses were tested against the two boards' main mating connector pair
(both the same 32-position connector part, one board's sheet note stating
"pinout is 1:1 with the mating side using top contacts"): an identity mapping
(pin *n* to pin *n*) explained 0 of 31 named signal contacts; a reversed
mapping (pin *n* to pin 33-*n*) explained 30 of 31, plus the two ground
mounting pads either way. The one mismatch is a signal whose name on one
board carries an active-low prefix and on the other board does not — a
genuine naming discrepancy, not decided here which side is correct. A second,
smaller 8-pin hand-wired interface between the boards matched the reversed
mapping 8 of 8.

- The system-level nets that cross the two boards all go through these two
  connector pairs' pin mappings.
- **Reference designators are not unique across boards.** Roughly a quarter
  of all designators exist on both boards, and about half of those carry
  different manufacturer part numbers on each side — the same designator
  label is a completely different component. Every component ID must carry
  its board.
- **Net names are not a join key.** Several dozen net names exist on both
  boards, but a handful of those are genuinely separate local nets that
  happen to share a name (test-point nets and a couple of power-good
  signals), not the same net. The EDA tool's own multi-board documentation
  agrees: net names there are described as local to each child project and
  not carried across automatically; a proper cross-board connection needs an
  explicit mated-pins table.

**Unit to unit (mechanical spring-contact connection, this product only).** The
owner hedged this part explicitly ("I think"). Two families of
spring-contact parts are used, all drawn not-fitted in this particular
print. Pairing the mechanical contacts by their net-name role (rather than by
reference designator) gives a unique partner for each of 5 contact pairs,
including two that cross (a transmit line on one board's contact pairs with
a receive line on the other board's different-numbered contact, and vice
versa). Neither PDF states which side of one unit meets which side of the
next unit in a stack, so this stays a hypothesis.

**Model the spike points to [inference]:**
- Product -> unit instance -> board instance (board, revision, variant) ->
  component (board-qualified reference designator, part number, fitted
  state) -> pin (number, name) -> board net.
- An interface record: kind (flexible-cable, wire, mechanical-pin, direct),
  the two connectors it joins, a pin-map rule (an explicit reversed rule or
  explicit pairs), whether it is engineer-declared or name-inferred, and a
  check score (names agree k of n).
- A unit-to-unit interface is templated over the unit index in a stack.
- System-level nets are computed from board nets plus interfaces, never
  stored by hand.
- Not-fitted is a property of the part in a variant; the part stays in the
  net, and reachability queries treat its pins as open. A zero-ohm jumper
  drawn not-fitted is the case that matters here.
- An open cable/harness description format (YAML-based, GPL-3.0) is a ready
  format if cables or harnesses ever need drawing; the interface record
  itself is simpler than that format.

## 7. Claude vision test (the owner's LLM runner)

Setup: a 150 dpi page render is shown by the Read tool at a reduced
resolution (roughly 2000x1433 regardless of the source render's higher
native resolution), so rendering at 300 dpi and letting the tool downscale
gains nothing over rendering at 150 dpi directly; the effective displayed
resolution is around 129 dpi either way. Reading the PDF itself, rather than
a pre-rendered image, returns an even smaller image and no text layer. A
crop rendered at 300 dpi is shown at its native resolution, since it is
smaller than the display cap.

| Page / task | Result |
|---|---|
| Sparse page (about 106 pins): one small logic gate's pin-to-net map and pin names | 8 of 8 nets, 8 of 8 names |
| Same page, higher-resolution crop | 8 of 8; the crop additionally resolved a visual ambiguity between two collinear wires that the geometry-based tracer settles independently |
| Same sparse page: membership of all named nets | 19 of 19 exact, 67 of 67 memberships |
| Densest page (about 200 pins): one charger IC's pin-to-net map | 33 of 33 (28 previously unseen), 33 of 33 pin names |
| Densest page: one connector's pin-to-net map | 34 of 34 (32 previously unseen) |
| Densest page: membership of 26 nets that must be followed along wires | 20 of 26 exact; high-confidence subset 15 of 16 exact, low-confidence subset 5 of 10; errors included two swapped rows on a divider network, one missed pull resistor, one missed capacitor spanning two nets |

Caveats: this is one design in one house style, where most IC pins carry a
net label right at the pin, so it is the easy case for vision. This is one
run, one model, and the reader also built the extractor, so it is not an
independent evaluation. A public benchmark measuring the hard, general case
found a leading model reaching only 0.62 symbol-detection F1 there.

## 8. Token cost

The raw embedded-netlist JSON for both boards runs to roughly 731K tokens.
A compact nets-plus-parts JSON is about 194K. A minified nets-only JSON is
about 12.6K. Plain "net lines" text (one line per net, members listed) is
about 10.7K; "part lines" text (part number, value, sheet, pin-to-net set)
is about 16.8K (about 9.8K restricted to parts with 3+ pins). A
representative MCU pin slice is about 2.1K, and the interfaces summary about
0.4K. One schematic sheet as lines runs 2.0-5.4K depending on density. One
page image, shown at the Read tool's capped resolution, costs roughly
3.7K tokens; via the PDF-reading path or a smaller crop, roughly 2.2-2.3K.

For comparison, a prior survey measured about 6.3K tokens for an entire
single-board development kit in compact form, with roughly 226 parts. This
two-board product has 793 parts, over a third of them test points.

## 9. Literature and products, 2023-2026

Public research in this period includes: an end-to-end schematic-to-netlist
system on schematic images across 300+ PCBs reaching mid-80s percent
end-to-end connectivity; a newer image-based system combining object
detection with a vision-language model for designators, reaching high-90s
percent accuracy on its own IC benchmark against a much lower baseline, and
strong precision/recall on a 50+ design PCB set; an analog-IC image-to-netlist
system reaching about 80% success; a large public benchmark of real PCB
schematics (nearly 2,000 schematics, hundreds of thousands of pins) where the
best vision-language model reached about 0.56 net coverage at about 0.39
accuracy, far above a classical graph-matching baseline but still far from
solved; a benchmark of textbook-style circuit problems where the best model
reached about 85% with persistent errors binding topology to the right
answer; and a small open-source research tool that parses vector PDFs
(PyMuPDF plus an object detector plus segment tracing) reaching net F1
0.42-0.56 in general and 0.80-0.89 on clean analog boards, though built for a
different EDA tool's PDF output. A prior survey's netlist-reading benchmark
found LLMs reading a plain netlist text file scored far better than reading a
PDF directly.

Commercial products in this space mostly require the native EDA tool running
live, or are cloud services with no offline PDF-ingestion path and, in
several cases, no published accuracy numbers at all.

**No commercial product was found that ingests a schematic PDF offline**,
and no tool at all was found that reads this class of EDA tool's PDF
bookmarks as a netlist. The only offline vector-PDF tools found are the
small research repo above and a second small research project [corrected in
verification: found on a second pass] that extracts the vector layer
deterministically and verifies it with a vision model on rendered tiles.
Neither reads embedded netlist bookmarks.

## What this means for atlas

All points in this section are inferences.

1. **The PDF ingest ladder, first match wins:**
   (a) A native EDA project file, if present.
   (b) An embedded PDF netlist (this EDA tool's outline and JavaScript, or a
       competing tool's net bookmarks). Exact, fast and deterministic; this
       product's PDFs are here.
   (c) Geometric tracing on a vector PDF, tuned per house style, with facts
       tagged as tool-derived rather than tool-verified.
   (d) A vision pass per page or crop, facts tagged as LLM-inferred.

   Each rung cross-checks the one above it; §3 shows (c) checking (b) at
   526 of 529 nets.
2. **Run both (b) and (c) on every PDF that has an embedded netlist.** They
   disagree only where the PDF is odd (stacked pins, a hidden anchor), and
   that disagreement is the deterministic second check the trust bar asks
   for.
3. **Use vision only as a reader of crops, and always against extracted
   text.** Claude read labelled pin rows perfectly but had trouble following
   wires. The useful job is "confirm this pin row" or "read this raster
   picture," not "build the netlist." Render crops at 300 dpi; a 300 dpi full
   page is wasted given the display cap.
4. **Interfaces are first-class, engineer-declared records** with a machine
   check (names agree k of n). The atlas proposes the pin map and the owner
   confirms it. Mismatches such as an active-low prefix that appears on only
   one side go straight into the consistency report.
5. **Board-qualified IDs everywhere**, because reference designators and net
   names both collide across boards in the same product.
6. **The bill of materials from the PDF popups drives the datasheet step:**
   the checklist of active parts with no datasheet on hand is a direct
   output, keyed by manufacturer part number.
7. **Serve lines, not JSON.** An agent can hold the MCU slice and the
   interfaces summary in context all the time. It can load one sheet, one
   net or one part line on demand, and never needs the full raw JSON.

## Open questions for the owner

1. Which side's spelling of a cross-board signal name is right, when the two
   boards disagree only in an active-low prefix?
2. For a multi-unit mechanical stack: does one unit's connector A meet the
   next unit's connector B in a fixed rotation, and can two "A" sides ever
   meet directly?
3. Parts drawn not-fitted in a given print: is that the shipping build
   variant, or hand-assembled parts that should count as fitted?
4. Do other projects' schematic PDFs come from an EDA tool with net
   bookmarks switched on? If some are plain prints from a tool with no such
   feature, which one should be the held-out test for the geometric tracer?
5. Should the datasheet request list also cover discretes (MOSFETs, diodes,
   TVS), or only ICs whose pins the firmware drives?

## Sources

External: the exporting EDA tool's docs for its manufacturing PDF export and
multi-board/variant features; a competing EDA tool's PDF export app note; a
third, open-source EDA tool's PDF docs; a cloud provider's vision docs
(image token cost, resolution tiers); the arXiv preprints and GitHub repos
cited in §9; an open cable/harness format's repository; two commercial
schematic-copilot products' own write-ups.

Local: the project's own two schematic PDFs; its existing hand-written
pin-extraction script; the first page of its MCU's public datasheet (for the
part-family cross-check); prior internal survey notes (not reproduced here).

## Verification (2026-09-29, independent)

Re-runs of the outline reader and join script reproduced byte-identical
output. Confirmed: outline entry counts and anchor-type counts per board; the
raster-picture and broken-placeholder pages; the total parts/pins/nets/MPN
counts and extraction time; the geometric tracer's net-match and false-pair
counts; the reversed-mapping match rate on the main mating connector (with
one correction below); the second interface's 8-of-8 match; the shared
reference-designator and shared-net-name counts; the missing-datasheet count;
the anchor-leak percentage in a plain text extractor and in the hand-written
script; the MCU pin-slice comparison; the vision-test pin and net accuracy;
the Read tool's displayed-resolution behaviour; and the compact-format token
counts. Corrected: the anchor-free pin-identity percentage (141 of 945, not
higher); the reversed-vs-identity match count on the main connector (two
ground pads matched under either mapping; restricted to signal contacts,
identity matches 0, reversed matches 30 of 31); one research tool's detector
architecture (two different object-detection models, not one); and the
claim of only one offline vector-PDF tool existing (a second was found on a
follow-up search). One claim — that a transcription predated the matching
extraction — is unverifiable after the fact, since only the tool's own
internal note records the order of operations.
