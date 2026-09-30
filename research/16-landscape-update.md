# Document-side landscape update: who reads datasheets, schematics, RMs and PRDs for firmware agents

**Status:** draft, 2026-09-29.

This note updates an earlier survey, covering only what is new on the **document** side; everything
below adds to or corrects those notes.

## Headline findings

1. **No surveyed product ingests a PDF schematic as connectivity** (a leading commercial tool accepts six
   native EDA/netlist formats and no PDF), and the open PDF routes score **27.44% net F1** by vision (one
   schematic-vision paper) and **0.42 F1** by vector geometry (an open-source schematic extractor).
2. **A real project's Altium Smart PDFs already carry a netlist and a BOM that no surveyed tool reads**:
   bookmarks put **719/725 and 1,288/1,291** pins on named nets, agreeing **88/88** with a hand-written
   pin-extraction script on the MCU, and JS pop-ups hold an MPN for **287/290 and 505/509** components.
3. **The nearest adoptable datasheet distiller** (MIT, 1,300+★, coding-assistant skills, page-cited
   values) reads only **9-53%** of seven real component datasheets' pages and **6 of 91** register-
   candidate pages [corrected in verification].
4. **Every vendor assistant that answers from datasheets is remote and single-vendor** (one vendor's
   documentation MCP allows **40 requests/hour** behind a login; another needs a community-site login),
   and one major MCU vendor still ships **no MCP server**.
5. **Nobody selects reference-manual chapters from the build**: on a real project the enabled SoC nodes
   pick **5** chapters beyond a fixed core set (**534 of ~1,400 pages, 39%**).
6. **One commercial product remains the only one claiming the full datasheet + schematic + code join**,
   with **zero** published accuracy numbers, while a competing datasheet-to-code mapping approach now has
   a funded commercial owner (a $2M-seed, on-premise startup).
7. **Doc-to-code link recovery reaches F1 0.86** (architecture docs, not requirements) [corrected in
   verification] on non-firmware projects, and shipped products only create links in new code, so
   PRD-to-existing-firmware traceability is unclaimed.

## 1. Products

### 1.1 A leading commercial firmware-assistant tool: what its docs say (read 2026-09-29)

- **Schematics:** several native EDA formats (KiCad, one major EDA vendor's format + project file,
  Eagle, PADS, Xpedition, EDIF). **No PDF.** Components/nets/power-domains referenced by structured IDs;
  a board revision uploads as a "new version" vs the previous; free tier covers "only the EDA formats in
  their plan".
- **Custom peripheral:** "select a PDF datasheet"; where it is parsed is not stated. SaaS default;
  on-prem/air-gapped are enterprise options.
- **Registers:** address, width, access, reset, fields; compares source code against clock-control
  fields but "does not establish that clocks, synchronization, or power sequencing are correct".
- **Local models:** any OpenAI-compatible endpoint; catalog/document index likely stays cloud-side on SaaS
  (inference).
- **Recent changes:** a JTAG-tool partner integration (demo webinar, no GA date/pricing) and an undated
  RTOS-project membership item. Catalog: 500+ platforms, 5,500+ peripherals (up from 3,000+).
- **No PRD or requirements ingestion** anywhere in its public documentation.

### 1.2 The company behind a competing datasheet-to-code mapping approach (new)

Two related research papers are authored at the same company. The mapping approach converts PDF to
markdown through HTML, then does semantic chunking, and maps datasheet *sections* to folders, files and
symbols: 154-156 sections evaluated, **73.3% file mapping**, 95.9% file existence, tokens reduced from
68.8M to 10.9M (grep/BM25 baselines score 0%), using a mid-size commercial model plus an open-weight
coding model. A companion project continues pretraining a 7B open-weight model on 818 repository-
datasheet pairs (~23.5B tokens, 117 manufacturers), released under a research-only licence. The commercial
product (seed-funded, on-premise) adds small models plus a knowledge graph of "hardware specifications,
software standards, and design patterns", claiming "90-95% accuracy" for test-case generation at one
client (metric undefined [corrected in verification]).
**So datasheet-to-code mapping has a commercial owner. It is not unclaimed research.** No evidence it
handles schematics, build-selected RMs, or PRDs.

### 1.3 A free MCU-configuration web tool (update)

A free DTS configurator (web app + IDE plugin) ingests "datasheets, technical reference manuals, and SVD
files" and emits `.dts`. The vendor says generation is deterministic and the AI step is extraction into an
internal representation, with human-in-the-loop checks for ambiguous cases. It is cloud-based, aimed at
forward generation, and reads no existing codebase or board schematic.

### 1.4 Board-review tools that check schematics against datasheets

| Tool | Ingests | Datasheets | Local? |
|---|---|---|---|
| A schematic-review SaaS (Tool A) | native Altium/Cadence, "not image-based" | upload -> attribute links -> library API -> parts-search | self-hosted (enterprise); no metrics; "does not eliminate false positives" |
| A schematic-review SaaS (Tool B) | Altium export script | fetched automatically, cites "specific datasheet pages" | not stated; subscription pricing, no metrics |
| A schematic-review SaaS (Tool C) | several EDA formats | not mentioned | **cloud only**, no metrics |
| A live-Altium coding-assistant plugin | live Altium (Windows) | the LLM reads them during review | local (MIT, small project); data goes to the user's own model account |
| **A KiCad-focused datasheet/schematic project (MIT)** | KiCad | per-part-number extraction + cross-check (pins vs nets, voltages vs rails) | local; corpus of 5,800+ KiCad repos |

All five check schematic against datasheet, and none looks at firmware — crowded and worth borrowing
from; the DT/code half is untouched.

### 1.5 Board-design generators: forward only, and none reads firmware

A requirements-to-schematic startup (requirements -> schematic/BOM, part catalogs over MCP); an
AI-assisted schematic-generation product (architecture diagram -> schematic/BOM/FMEA, cloud); a
code-as-hardware-design tool (Python, "your approved AI", local); an open-source hardware-as-code project
(MIT, ~4,000 stars, no datasheet feature); a design-copilot product ("cites its sources", cloud); a
hardware DSL project (Claude drafted "roughly 250 reference designs" from datasheets); a layout SaaS
(cloud-native, SOC 2); and three major EDA vendors' AI features (cloud layout with on-prem "rolling out in
2026"; an agent "announced for forthcoming releases"; AI bus completion and decoupling placement).

None of these ingests a PDF schematic, reads firmware, or consumes an MCU reference manual — **outside
atlas's territory**, apart from one borrowable idea: serving part data as MCP tools.

## 2. Open-source MCP servers and tools on the document side

All run locally unless noted.

| Project (licence, stars) | What it does | Notes for atlas |
|---|---|---|
| **A KiCad-focused datasheet skill (MIT, 1,300+)** | TOC->keyword->fallback page selector -> scout subagent -> typed JSON per part -> cross-check -> quality score | Value record with min/typ/max/unit/condition/evidence; three null states; PDF-hash staleness. Score measures **completeness, not correctness**. Budget 10-15 pages; no register schema; PDF-schematic path is vision guidance only. |
| A datasheet-to-YAML extraction tool (MIT, small) | datasheet -> YAML profile (rails, pinout, registers, init) | its own test found a table-extraction library **damaged 34% of tables**; skips MCU registers |
| A datasheet-chapter MCP server (Apache-2.0, small) | PDF -> markdown -> heading split -> chapter-list/read-chapter tools | closest to the spec's section tier; optional vision-model image captions |
| A datasheet-index MCP server (no licence, small) | PDF-to-markdown -> heuristic registers/pins JSON -> vector DB | ideas only; remote vector DB behind a tunnel |
| A general PDF-browsing MCP server (MIT, ~80★) | TOC, page text, page PNG, search | the raw-page tier; the coding assistant's own file-read tool covers it (inference) |
| A datasheet RAG project (Apache-2.0, small) | layout parsing + dense/rerank retrieval over 90 datasheets, local model | self-reported hit@8 0.97; its own ablation rejected a BM25-hybrid and fine-tuning |
| Several small vector-RAG-over-datasheets projects | vector RAG over datasheets / one vendor's docs / an embedded-AI workspace | the earlier survey's evidence argues against vector RAG as the backbone |
| A schematic-vision project (MIT, small) | vector wires/junctions/text + tiled vision -> auditable netlist | ~50-part industrial-drawing test; "direct file access beats RAG" |
| An open-source schematic extractor (MIT, small) | vector PDF -> components<->nets, object-detection model | F1 **0.42** geometric, ~0.56 with the detector, 0.7-0.9 on normal density; KiCad-only |
| A few KiCad/Altium live-editing MCP servers (mixed licences, one 2,500+★) | drive KiCad or a live EDA tool | editing tools; one ".dts generation" feature is unmeasured |
| A couple of small SVD/JTAG-focused MCP servers | re-checked | unchanged; **no new SVD MCP server found** |

Generic PDF converters (a document-conversion MCP project, MIT, 750+★; a large open-source PDF pipeline,
tens of thousands of stars; a lightweight markdown converter) underlie most tools above; their datasheet
accuracy is not measured here.

## 3. Silicon vendors, document side

New since the earlier survey is in **bold**.

| Vendor | What exists | Runs | Documents |
|---|---|---|---|
| One major MCU vendor | **a new "design sidekick" web tool**; still **no MCP server** | community-site login | datasheets, RMs, app notes, KB; answer links, refreshed monthly |
| A second MCU/SoC vendor | a documentation-search MCP server | **remote**, login, 40 req/h | datasheets, TRMs, HW guides; latest SDK only |
| A third MCU vendor | an SDK-integrated AI assistant MCP server | login-gated | SDK docs, API refs |
| An analog/mixed-signal vendor | a tool-docs skill + llms.txt guide; an AI debug MCP server | local | tool docs, not part datasheets |
| Another MCU vendor | an embedded-IDE AI assistant: local server + skill files (no MCP server) | local | MCU/BSP/peripheral config; **no datasheet/RM ingestion stated** |
| A DSP/analog vendor | a generative-AI feature paired with vendor data, in its IDE | not stated | not stated |
| A general-purpose MCU vendor | an AI coding assistant + MCP/docs-search server | cloud | product and doc search |
| Another general-purpose MCU vendor | an experimental agentic-AI feature (pages returned access errors) | not verified | not verified |
| A final MCU vendor | none official | — | — |

An industry census (re-read): "three of twelve stop at retrieval". Unchanged: **no vendor assistant
reads the customer's schematic, build or code together with the documents** — the assistants are remote,
single-vendor, and cite links rather than checked facts. This project's parts come from four vendors, so
several corpora would be needed, and one vendor has none (inference).

## 4. Research, 2024-2026

### 4.1 Hardware-aware codegen benchmarks

| Work | Setup | Numbers |
|---|---|---|
| A hardware-agent benchmark (2506.11003, ICSE) | 126 cases, 9 components, 3 hobbyist platforms | pass@1 **55.6% with schematic, 50.0% without**; rises further with retrieval + compiler feedback |
| A closed-loop embedded-agent study (simulated platform, snippet only) | 420 runs, simulated microcontroller | one frontier model leads; one local open-weight model is the best local option |
| A schematic-design benchmark, large datasheet KB (2603.18102) | 300 board-level tasks, **2,914-datasheet** KB | best pass rate **8.15%**; models "lack physical intuition" |

Inference from the hardware-agent benchmark: correct wiring alone is worth about **+5.6 points** of
pass@1, roughly double once retrieval and compiler feedback are added — the case for a pin/net map. **No
benchmark supplies an RM or datasheet PDF and measures register-level correctness**, on Zephyr or
otherwise.

### 4.2 Schematic and PCB understanding (new since the earlier survey)

A large schematic-vision benchmark (2604.00270, includes a major research lab): 1,854 real schematics,
245.4K pins; best VLM reaches net-detection F1 **27.44%**, net-name accuracy **25.07%**; bounding-box
symbol detection is below 0.3%; humans score ~96-99%. Together with an earlier PCB-connectivity benchmark
(84.47% end-to-end), this says **image-based schematic reading is a fallback marked `llm-inferred`, not a
primary path.**

### 4.3 Datasheet extraction and QA

- **A verification-focused extraction paper (Qiao & Dick, 2608.25217):** 7 designs, 34 datasheets. It
  retrieves properties along given verification criteria and generates *deterministic scripts* to check
  them: accuracy **97.5%**, against **14.9%** for plain "upload-and-query" and 62.7% with the criteria
  added. Five datasheets came to ~223K tokens, and one frontier model still "omitted multiple required
  engineering properties". No code released.
- **A fidelity-checking paper (2608.28439):** 37 claims over 4 components, 207 extractions passed a
  fidelity check. "One model passed our fidelity check without ever opening the datasheet." Logging at
  the tool-dispatch level recovered all 50 planted faults: "the tool layer buys portability and
  observability rather than accuracy."
- **No public benchmark for register-table extraction exists** (re-searched, no new hit).

### 4.4 Requirements-to-code traceability

| Work | Data | Result |
|---|---|---|
| A natural-language-to-code traceability paper (2509.05585) | 12 OSS projects | one model **+8.84% F1** over a prior graph-based baseline |
| An LLM-based traceability paper (2604.22432, COMPSAC) | 5 datasets, 2 languages | +7.4% F1 on average, -41.7% tokens |
| An architecture-doc-to-code paper (2511.02434) | architecture docs <-> code | F1 **0.86** without a manual architecture model (0.87 with one) |
| A generate-with-links paper (2607.18886, ISSTA) | two open datasets, 125 use cases | *generates* code with links; roughly half to two-thirds task success |
| A requirements-to-code theory paper + tool | theory only | forward-only: requirement IDs become compile-time-checked macros |
| A commercial requirements-tool's MCP feature | product | forward: "a reference to the requirement is embedded in the implementation" |

None uses firmware, C with Kconfig/devicetree, or a PRD as its source.

## 5. Measured on a real project

### 5.1 Which tool premises hold for these PDFs

| File class | Pages | Outline entries (depth) | Characters/page | Notes |
|---|---|---|---|---|
| board A / board B schematic export | 7 / 10 | **2,541 (7)** / **5,001 (7)** | 4,701 / 6,530 | Altium, thousands of line segments and JS objects each |
| MCU reference manual | ~1,400 | 1,965 (4) | 2,027 | 43 numbered chapters |
| three power-management IC datasheets | 111 / 55 / 49 | 128 / 85 / 62 | ~1.9K | one vendor's typesetting toolchain |
| one fuel-gauge datasheet | 115 | 236 | 2,814 | a desktop-publishing toolchain |
| two load-switch datasheets | 19 / 27 | 45 / 58 | ~1.6K | a word-processor export |
| MCU datasheet | 165 | 261 | 1,611 | |
| product requirements document | 24 | 36 (2) | 1,137 | a cloud-docs export; all 24 pages carry images |

No page in any file is without text, and none has hidden-layer content. **Every PDF has an outline**,
so TOC-driven chaptering is possible on all 11 files.

### 5.2 Smart PDF netlist and BOM, recovered without an LLM

The bookmark tree follows `SchDoc > Components > REFDES > REFDES-PIN` and `SchDoc > Nets > NET >
{Pins, NetLabels, Ports}`. Board A: 290 components, 719/725 pins on 120 named nets; board B: 509
components, 1,288/1,291 pins on 239 nets; no pin appears under two net names. Cross-checked against a
hand-written pin-extraction script: all 88 of its rows hit a bookmark net for the MCU, and the bookmarks
add 10 MCU nets the script misses (power, reset, debug interface, 4 nets named after their own pins); the
script's net column also picks up ~22 hidden Altium anchor tokens the bookmark JavaScript searches for.
BOM: each component's JS property pop-up holds Comment/Description/Footprint/Library-Reference/
Manufacturer/MPN; MPN non-empty for 287/290 (A) and 505/509 (B); a naive link-rectangle-to-designator tie
matched **264/290 and 460/509** pop-ups. The Altium Smart PDF wizard's "Generate nets information" (pins,
net labels, ports) and "Include Component Parameters" checkboxes are what produce this (inference; a plain
PDF print would not carry it). **Multi-board:** 218 designators and 36 net names appear in *both* PDFs, so
any key must be `(board, refdes)`; that the 36 shared names are the same wire through the board-to-board
connector is **not established** (inference).

### 5.3 The KiCad-focused datasheet skill's page budget on these datasheets

| Datasheet | Pages | Selected | Register-candidate pages | Of those, selected |
|---|---|---|---|---|
| charger IC | 111 | 10 (9%) | 43 | 0 |
| buck-boost IC | 55 | 10 (18%) | 12 | 0 |
| current-monitor IC | 49 | 10 (20%) | 13 | 2 |
| fuel-gauge IC | 115 | 10 (9%) | 22 | 3 |
| MCU datasheet | 165 | 15 (9%) | 1 | 1 |
| load-switch datasheets | 19 / 27 | 10 / 10 | 0 | — |

"Register-candidate" means a page matching a regex proxy, not a hand count; the owner's rule is layered
depth with nothing dropped. **The budget design drops 47-91% of pages, and nearly all register maps, from
the fact layer.** Adopt the schema, not the selector.

### 5.4 RM chapters selected by the build

| Build variant | Enabled SoC nodes | Chapters beyond core | Pages (+236 core) | Share of RM |
|---|---|---|---|---|
| main board (both configs) | 20 | ADC, I2C, USART, USB, USB-PD (298 pp) | 534 | 0.39 |
| dev-kit variant A | 30 | 12 chapters (661 pp) | 897 | 0.65 |
| dev-kit variant B | 32 | 14 chapters (698 pp) | 934 | 0.67 |

The core set (memory/bus, flash, power, clock control, GPIO, system-config mux, interrupt controller,
external interrupts, debug = 236 pp) and the compatible-to-chapter table are hand-written inferences; the
"unmapped" nodes are the core blocks plus 2 PWM nodes on the dev-kit variants [corrected in verification].
Chapter granularity is coarse (the I2C chapter alone is 65 pages); section-level selection is the next
cut, not measured here.

## 6. White-space verdict per capability

| Capability | Who claims it | Verdict |
|---|---|---|
| **PDF-schematic ingestion** | No product (all native-file); OSS: vision (27% F1) or geometry (0.42-0.71 F1, KiCad only) | **Open** — a deterministic route exists for Altium Smart PDFs, unused (§5.2) |
| **Datasheet distillation with page provenance** | one KiCad skill; a small YAML tool; one commercial cloud feature; one closed startup | **Partly claimed** — nobody offers full-coverage tiers with registers and cross-checked facts |
| **RM chapter selection by the build** | Nobody | **Open, cheap** (outline + EDT, §5.4) |
| **Cross-source mismatch detection** | schematic<->datasheet: several tools. code<->schematic/registers: one commercial tool, in chat | **Partly claimed** — a deterministic schematic<->DT<->driver<->datasheet report is open |
| **PRD -> firmware traceability** | non-firmware research (F1 ~0.8-0.87); forward-only products | **Open for firmware**; proposals the engineer confirms |
| **Fully local except the LLM** | a handful of small OSS pieces plus two vendor tools; every integrated product is cloud or login-gated | **Open as an integrated product** |

## 7. Adopt, adapt, or build

- **Adopt the schema (MIT):** the KiCad skill's value+evidence record (page/section/confidence/method),
  three null states, PDF-hash staleness, minimum-confidence trust gates — not its page selector or
  completeness-only score.
- **Adopt the pattern (Apache-2.0):** the chapter MCP server's list-chapters/read-chapter tools over
  TOC-split markdown as the middle tier (4 tools, fits a ~5-verb budget).
- **Adopt the rules:** required part name + explicit gap reporting (datasheet-index project); geometry by
  script, interpretation by the model, unresolved to a review queue (schematic-vision project); log which
  pages were actually opened, as provenance (fidelity-checking paper).
- **Adopt the eval method:** the schematic extractor's KiCad-generated ground truth, for any non-Altium
  PDF path.
- **Build:** the Smart PDF bookmark + JavaScript parser (~200 lines; nothing else does it); RM selection
  from EDT compatibles + SVD peripheral names + the RM outline.
- **Do not adopt:** vector-RAG backbones; unlicensed code; remote vendor MCPs.
- **Licence flag:** the PDF-parsing library used in the spike is **AGPL-3.0 or commercial** — a CLI-based
  PDF toolkit as a subprocess avoids that.

## What this means for atlas

(All inferences.)

1. **Schematic ingestion should branch on what the PDF contains.** Smart PDF with net info: bookmarks
   give the netlist, JS pop-ups give the BOM with MPN, hidden anchors give cite positions — deterministic,
   no LLM, under 1 s for both boards. Anything else: vector geometry, then tiled vision, `llm-inferred`.
   The pin/net map milestone can take the deterministic branch.
2. **The MPN is the join key** from the schematic layer to the datasheet layer, keeping the owner's rule
   that wiring lives in the schematic doc only.
3. **Provenance should record the method** (bookmark, text layer, LLM pass N, pages opened), not only the
   page.
4. **Every key needs the board.** 218 designators repeat across the two PDFs.
5. **The gap narrowed on one axis.** Datasheet-to-code mapping now has a commercial owner and a
   generalist competitor. What stays defensible: the build selects the documents, the tool runs locally,
   and it reads the PDFs clients actually send.

## Open questions for the owner

1. Will every board arrive as Altium Smart PDFs with "Generate nets information" and "Include Component
   Parameters" on? Can that be part of the atlas intake procedure?
2. Is the KiCad skill's per-value evidence schema (MIT) acceptable, with a register/timing block added?
3. The spike's PDF-parsing library is AGPL-3.0 — fine for a local internal tool, or stay on CLI PDF tools?
4. Should the RM "core" chapter set be fixed, or should the build drive it too?
5. For PRD traceability: should atlas *propose* links into a file you confirm, or only answer on demand?

## What I could not verify

One MCU vendor's agentic-AI docs returned access errors; another's developer-help page timed out (MCP
tools known only from a search snippet); two parts-search sites returned access errors; the "closed-loop
embedded agents" paper returned an access error; the competing startup's claims come from a VC blog post
only; one commercial tool's parsing location, feature accuracy and RTOS-membership date are unverified.
**Local numbers:** the file-read tool was not used on a schematic page for this note.

## Sources

Opened on 2026-09-29 unless marked. Product docs: a leading commercial firmware-assistant tool's public
documentation (schematics, hardware-catalog, add-peripheral, registers, local-models, MCU support,
quickstart, workflows, news, a partner-integration announcement) and a third-party guide to it; the
competing startup's two research papers and funding announcement; a free DTS-configurator's page and a
trade-press CEO interview; board-review tools (blog posts/docs for three schematic-review SaaS products,
one live-Altium plugin's repo, the KiCad-focused skill's repo); board-design generators (public pages for
a requirements-to-schematic startup, an architecture-to-schematic startup, a code-as-hardware-design tool,
an open-source hardware-as-code project, a design-copilot post, a hardware-DSL post, a layout SaaS post,
three major EDA vendors' AI announcements); the Altium Smart PDF wizard docs; a vendor-AI-tooling industry
census and public pages for the vendor tools in §3; OSS repo READMEs/metadata (code-forge API) for the
projects in §2. Research: https://arxiv.org/abs/2506.11003 · https://arxiv.org/abs/2603.18102 ·
https://arxiv.org/html/2604.00270v3 · https://arxiv.org/abs/2608.25217 (+ /html/) ·
https://arxiv.org/abs/2608.28439 · https://arxiv.org/abs/2604.22432 · https://arxiv.org/abs/2509.05585 ·
https://arxiv.org/abs/2511.02434 · https://arxiv.org/abs/2607.18886 · https://arxiv.org/html/2603.13999

See also: [13-mcu-reference.md](13-mcu-reference.md), [15-cross-source-join.md](15-cross-source-join.md), [17-ingestion-procedures.md](17-ingestion-procedures.md).

## Verification (2026-09-29, independent)

| Claim | Verdict | Evidence |
|---|---|---|
| Schematic-vision benchmark 27.44%; open-source extractor 0.42; leading commercial tool has no PDF path | confirmed | checked against papers/READMEs/docs directly |
| 719/725, 1,288/1,291, 88/88; MPN 287/290, 505/509 | confirmed | re-run; a few MPN ties are duplicate-designator artifacts |
| Register-candidate page coverage | corrected | 6/91, not 5/90 |
| Licences, star counts | confirmed | repo metadata |
| One vendor's rate limit; another's no-MCP status | confirmed | vendor blog and industry census |
| RM page count for the main build | corrected | +2 PWM nodes beyond the stated core |
| The competing mapping approach's headline numbers | corrected | one figure came from the funding-round press piece, not the papers |
| An architecture-to-code paper's scope | corrected | links architecture docs to code, not requirements to code |
| Hardware-agent and verification-extraction paper numbers | confirmed | checked against the papers directly |
| Spike script timing | corrected | ~35 s per script, not the originally estimated 7 s |
