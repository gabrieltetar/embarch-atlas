# Is Markdown the best form for an LLM to browse? Formats, navigation and serving for atlas documents

**Status:** draft, 2026-09-29.

This note answers the owner's question ("is markdown the best form for an LLM to browse?") for the
**document** layer: datasheets, reference-manual (RM) chapters and schematic nets, and covers how the
tiers should be served over MCP and the CLI. It builds on an earlier encoding note (content beats syntax,
related facts sit together, native names are IDs, a byte-stable spine, JSON is storage). "Real model
tokens" are actual counts from headless coding-assistant usage; "o200k" counts use tiktoken as a proxy.

## Headline findings

1. **Markdown is the right container, and one of the cheapest readable forms.** Measured on three
   real-datasheet samples, a Markdown pipe table (one condition per row, no merged cells) cost 2,943 /
   931 / 802 real tokens. The same content cost 1.39-1.61x that as pretty JSON, 1.34-1.56x as HTML and
   1.16-1.24x as YAML; only line records, CSV (0.78x) and merged-cell Markdown (0.90x) cost less
   [corrected in verification]. A small YAML front-matter added a flat ~41 tokens.
2. **The format decides what `grep` returns.** One `grep -w` recovered 34/34 electrical-spec rows
   complete from the Markdown table or from one-record-per-line text. It recovered 0/34 from pretty JSON,
   YAML or Markdown key-value blocks, and only 18-19/34 when the PDF's merged cells were reproduced (HTML
   rowspan, or "ditto" rows). Raw PDF-text extraction gave 5/34.
3. **Once the right page is in hand, the format hardly matters.** The model answered 15/15 from the page
   images, the Markdown table and the line records, and 14/15 from raw text (a lost unit symbol; the model
   said so rather than guessing). The hard problem is *reaching* the page — navigation, not format.
4. **The coding assistant's file-read tool on a PDF page returns a JPEG and no text, at 1,463-1,549 real
   tokens a page.** That is cheaper than reading the distilled Markdown of the same two pages (2,726
   against 3,273). So tier 3 is cheap enough to be a routine check. Distillation pays for findability,
   exactness and citation, not for token savings at page scale.
5. **Real-model counts run 1.29-1.63x o200k (median 1.40, n = 31 files),** vs a prior planning factor of
   1.3. The file-read tool also adds ~2 tokens per line and ~145 per call, so a 366-line pretty-JSON file
   costs 880 more tokens to read than to paste.
6. **No published method beats "table of contents + lexical search + read the section" for a
   tool-using agent.** The methods that do win are refinements of it:
   - A locate-then-read tool design scores +10.3 points over flat agentic search.
   - One paper gets lexical-only retrieval to parity with hybrid retrieval (0.717 vs 0.716) at 41x lower
     build cost.
   - Graph-based retrieval ties or loses on simple fact retrieval.
   - A hierarchical-tree navigation product's 98.7% figure is self-reported.
7. **Every project PDF already carries a navigation tree.** All 11 documents checked have a bookmark
   outline, of 36-5,001 entries. The vendor outlines give section numbers and physical pages, and the
   printed page equals the physical page in the three documents checked. The RM's 43 chapters cost 593
   o200k tokens as an outline. Two Altium schematic outlines list components, nets and ports for every
   sheet: thousands of entries per board.

## 1. What the format literature says (tables and technical text, 2024-2026)

| Study | Setup | Result | Weight for atlas |
|---|---|---|---|
| Multi-table QA (2411.19504) | 7-8B open models, 8K context | Markdown 46.6-49.9% vs CSV 40.6-47.6, JSON 34.3-41.0, HTML 44.0-46.6; JSON weakest | small models only |
| Telecom-standard tables (2601.04202) | 2,220 tables, merged cells, multi-level headers | HTML/JSON/Markdown all 90-92% on a mid-size model; Markdown uses 42% fewer tokens than HTML | closest domain; 1.6-pt spread |
| Cross-format tables (2606.09578) | HTML / Markdown / LaTeX / images | one frontier model ~65% in every format; HTML "safest" for open LLMs; gaps shrink as images | frontier models look format-flat |
| HTML-cleaning RAG (2411.02959) | RAG over web pages | cleaned HTML beats plain text on six QA sets | web pages, not tables |
| Long-doc multimodal (2407.01523) | 130 PDFs as page images | one model F1 42.7%; most others **worse** than their own OCR-text variant | whole-document page images are weak |
| Schematic/netlist QA (2606.23704) | schematic Q&A | restructured JSON beat native netlists on two models; native PDF far lower | only schematic study found; Markdown untested |

Read together (inference): frontier models are close to format-flat on small tables; the largest spread
above is 1.6 points against 30-60% token differences. HTML's edge shows on open models and complex
headers. The schematic-QA gain most likely comes from *restructuring*, not JSON syntax, since Markdown
was never tried — matching the encoding note that the answer path and locality matter, syntax mostly does
not. Anthropic's own guidance: long data at the top and the query at the end (up to 30% better); quotes
before the answer. PDF support: 1,500-3,000 text tokens per page plus image tokens at a fixed formula with
a cap; use logical page numbers; citations address PDFs by 1-indexed page location. **None of these
studies tests a tool-using agent browsing files** — the gap the measurements below partly fill.

## 2. Measured on the project: encodings x three samples

All three samples are public vendor pages, hand-transcribed from renders into a gold-reference script:
- **A.** A load-switch IC's electrical characteristics and protection specifications: 34 rows and 7
  footnotes.
- **B.** A buck-boost IC's mode-register table and register summary.
- **C.** A reference-manual timing-register section (register field list with formulas).

Every encoding carries identical facts. The real-model counts come from a headless usage-delta script.

| Encoding | A: real tokens (xmd) | B | C | Note |
|---|---:|---:|---:|---|
| Markdown pipe table (one row per condition) | 2,943 (1.00) | 931 (1.00) | 802 (1.00) | reference |
| Markdown + YAML front-matter | 2,984 (1.01) | 973 (1.05) | 843 (1.05) | +41-42 tokens flat |
| Markdown prose (RM field list) | — | — | 795 (0.99) | prose is as cheap for RM text |
| One record per line (`key=value; ...`) | 2,722 (0.92) | 872 (0.94) | 843 (1.05) | self-describing |
| Markdown table with merged cells ("ditto") | 2,654 (0.90) | — | — | breaks grep, see below |
| Markdown key-value blocks | 3,220 (1.09) | 1,087 (1.17) | — | best at 1,000 rows (encoding note) |
| YAML | 3,545 (1.20) | 1,151 (1.24) | 929 (1.16) | |
| JSON compact | 3,429 (1.17) | 1,137 (1.22) | 932 (1.16) | one line, so grep returns everything |
| JSON pretty | 4,429 (1.50) | 1,500 (1.61) | 1,112 (1.39) | |
| HTML table | 4,511 (1.53) | 1,455 (1.56) | 1,076 (1.34) | rowspan variant 4,083 (1.39) |
| CSV | 2,299 (0.78) | — | — | known to fail at scale (encoding note) |
| raw PDF-text extraction of the page(s) | 2,789 (0.95) | 1,388 (1.49) | 1,100 (1.37) | loses unit symbols, overbars, row binding |
| SVD XML of the same register | — | — | 501 (0.62) | **drops every formula and rule** |

Real-token/o200k ratio over all 31 files: 1.29-1.63, median 1.40. A legacy tokenizer tracks o200k within
0.93-1.07x and does not predict the current model's real count either.

**Grep yield.** This asks whether one matched line carries the record's name, its condition and its
values: exactly what an agent's `Grep` returns without context flags.

| Encoding | A rows (34) | B fields+regs (16) | C fields (5) | Lines carry labels? |
|---|---:|---:|---:|---|
| one record per line | 34 | 16 | 5 | yes |
| Markdown table (+front-matter) | 34 | 16 | 5 | no, header is another line |
| HTML table | 32 | 16 | 5 | no; `>` escaping lost 2 rows |
| merged cells (Markdown or HTML rowspan) | 19 / 18 | — | — | no |
| raw PDF-text extraction | 5 | 8 | 0 | no |
| Markdown KV, YAML, JSON pretty | 0, 0, 0 | 1, 0, 0 | —, 0, 0 | partly |
| JSON compact | 34 | 16 | 5 | one line, 36,686 tokens for 17 greps |

**Reading cost through the coding assistant.** This is the real-token delta per file-read call, including
the ~145-token call overhead measured on a one-line file.

| What was read | Real tokens | Returned blocks |
|---|---:|---|
| PDF, 1 page (each of the three samples) | 1,463 / 1,471 / 1,549 | text line + 1 JPEG |
| PDF, sample A's two pages | 2,726 | text line + 2 JPEG |
| same content: Markdown table file (83 lines) | 3,273 (content 2,943) | text |
| same content: line records file (42 lines) | 2,953 (content 2,722) | text |
| same content: pretty JSON file (366 lines) | 5,309 (content 4,429) | text |
| same content: raw-text-extraction file (107 lines) | 3,164 (content 2,789) | text |

The file-read tool sends a PDF page with **no text layer**: it arrives as an 850x1100 px JPEG, read from
the image header in the stream [corrected in verification]: about 1,240 visual tokens plus overhead. The
line-number prefix costs ~2.0-2.2 tokens per line in every file.

**Accuracy.** Results, single run each:
- **Sample A, two pages:** 15/15 from the page images, 15/15 from the Markdown table, 15/15 from the line
  records, 14/15 from raw text. On the miss the model wrote "no unit; the expected symbol is most likely,
  but the symbol is missing" rather than hallucinating.
- **A dense, rotated-text register-map page**, read as a page image: 15/15 bit positions against the SVD.
  Caveat: this class of register is common in training data, so memorization cannot be ruled out.

These tests hit a ceiling: format does not separate the forms *once the right page is in context*, and
n = 15 is too small to rank formats.

**What the samples showed about content** (the "make an agent curious" quirks the scope asks for; they
live in prose and footnotes, which is why the RM must be distilled even with the SVD in hand): a hex
reset value with a misleading binary-looking label; a TOC name that disagrees with its own register-summary
table; a footnote holding fault-class behaviour two other tables omit; a timing formula missing a "+1"
term present on sibling fields, with a "configure only when disabled" rule in the RM but not the SVD; and
an SVD register description copy-pasted from an unrelated peripheral, using an older pair of role terms
the current RM revision has replaced.

## 3. Long-document navigation: does anything beat TOC + grep + read the section?

| Method | Evidence | Verdict for atlas |
|---|---|---|
| Hierarchical-tree navigation (MIT, cloud-only product) | layout tree, no LLM; then LLM summaries; one benchmark 98.7% **self-reported**; no local server | adopt the *shape*; skip the product |
| Recursive-summary tree paper (2401.18059) | clustered chunks, recursive summaries; +20% on a QA benchmark | fiction/QA; summaries are the prose tier the prior note capped |
| Interactive-navigation paper (2310.05029) | LLM navigates a summary tree interactively; beats long-context/retrieval baselines | same idea; datasheets already have the tree |
| Section-sized retrieval-unit paper (2406.15319) | 4K-token units, top-8; strong scores on two QA benchmarks | supports section-sized units over small chunks |
| Locate-then-read paper (2602.05014) | PDF -> structured Markdown with coordinates; retrieve + read-section tools; +10.3 pts over flat agentic search; read-section alone: 18.9% | **closest published analogue of tier-2 + find** |
| Lexical-query paper (2605.27123) | LLM writes lexical queries over an inverted index; parity with hybrid at 41x cheaper build | lexical + a capable agent is enough |
| Contextual retrieval (Anthropic blog) | small context prepended per chunk; top-20 failure cut roughly in half with BM25 | header + "aka" line is the no-LLM version |
| Page-image retrieval (2407.01449 + a scaling follow-up) | beats OCR+BM25 on a visual-doc benchmark; later versions saturate >90 | needs a large VLM; schematic pages only |
| Graph-retrieval benchmark (2506.05690) | basic RAG matches/beats graph RAG on simple fact retrieval, far cheaper | most datasheet lookups are simple fact retrieval |
| Graph/multimodal study (2607.16604) | a graph injected into the prompt hurts faithfulness; helps only in retrieval, under evidence deficits | keep the graph behind tools, not in the prompt |

**Sizes that decide the shape:**
- **The 11 PDFs:** 1.47M text-layer tokens together.
- **The RM:** 1.02M on its own, above a 1M context. Its largest in-use chapter is about 40K, and the
  register subsection within it is 13K.
- **The datasheets:** 11.6K to 112.7K tokens, with a median page of 509-878.
- **Outlines:** the RM has 1,965 entries at 36K tokens in full. Its 43 chapters cost 593, and one
  chapter's subtree (89 entries) costs 1,659. The datasheet outlines run 483-4,109 in full and 155-1,216
  at two levels.

Several datasheets would fit in context whole. "Read it whole" still loses to "read the section", for
three reasons:
- Context rot and lost-in-the-middle effects (encoding note).
- The needed fact usually sits in one node of an outline of 45-261 entries.
- A tier-3 page costs about 1.5K tokens.

The evidence therefore points to **TOC first, then lexical find, then read the section, then view the
page to verify**. Every published winner above is that loop with better coordinates.

## 4. Serving: files, MCP, CLI, skills

Facts about the runner, from the pages linked in Sources:
- **Tool schemas are deferred.** Tool search is on by default: only names and server instructions load at
  session start; `alwaysLoad: true` exempts a small server. Each first use costs one tool-search round
  trip (observed here). This relaxes an older tax where every schema loaded into every message.
- **MCP output is capped**, warned above 10,000 tokens and capped at 25,000; past the cap the client saves
  it to a file and returns a reference. A tool can raise the limit via a metadata field.
- **Resources vs tools.** Resources are application-driven (auto list/read); tools are model-controlled.
  Protocol pagination covers only list operations, so **a tool result must paginate itself**.
- **Anthropic's tool guidance:** a 25K default cap; a concise/detailed switch; "eschew low-level technical
  identifiers"; prefix vs suffix namespacing measurably moves evals. Tool search lifted one model's
  benchmark score from 49% to 74%; presenting tools as files cut one workflow from 150K to 2K tokens.
- **Skills:** description text truncates at 1,536 characters; after compaction a skill re-attaches its
  first 5,000 tokens, within a 25,000 total.
- **A files.txt-style convention** is common but low-adoption (~10%): H1, blockquote summary, H2 file
  lists, an "optional" section for what an agent can skip.

## 5. Stable IDs and citations

Cost of address styles (o200k): a part-and-page cite (`<part> p8`) costs 8 tokens, a document-and-section
cite (`<doc> p976 §32.9.5`) costs 11, a part-register-field cite costs 8, a page-plus-condition cite costs
16, a YAML cite object costs 33, a UUID costs 36.

Rules (inference; the encoding note's ID rules carry over unchanged):
1. **The page coordinate is the physical 1-based PDF index** — what the file-read tool's page parameter
   and the citations API's page location both use. In all 11 PDFs either no page labels exist or 0 differ
   from the physical page. Store the printed label only where it differs.
2. **Doc ID = part slug + revision** (e.g. `<part>@rev`), plus a SHA-256 prefix of the PDF so a replaced
   PDF is detected.
3. **Section ID = the document's own numbering** from the outline, falling back to a title slug. Table ID
   = the caption's own number.
4. **Fact ID = native names, never ordinals:** `<part>.ec.<symbol>.<condition>`, `<part>.reg.<REG>.<FIELD>`.
   Facts keyed by (part, symbol, condition) survive a revision that moves pages; the cite is re-derived.
5. **A cite is short and re-openable:** e.g. `<part> p8 fn26` (≤ 16 tokens), resolving through a
   doc-section call or a direct page read, with no lookup table.

## 6. Recommendation

### Format per tier

| Tier | File | Format | Budget (real tokens) |
|---|---|---|---|
| L0 map | `MAP.md` | Markdown, files.txt-shaped: parts, docs, enabled RM chapters, boards, links | ≤ 5K, byte-stable |
| L1 card | `parts/<id>/CARD.md` | small YAML front-matter (id, doc, sha, rev, provenance, cite form), an "aka" line, 5-15 bullet facts with page cites, register summary, **quirks**, section index with tier-2 status | ≤ 1.5K |
| L2 section | `parts/<id>/s/<secid>.md` | Markdown; tables with **one condition per row, no merged cells, symbol repeated**, footnotes pulled onto rows as a note column plus a notes list; prose as field lists | ≤ 4K per file, split larger |
| L2 facts | `facts/facts.txt` | one fact per line: `id key=value; ...; cite=...; prov=...` | ~80 tokens per fact; never loaded whole |
| L3 page | the PDF itself, plus `pages/p<NNN>.txt` from text extraction | image through a page-read call; the text layer only as a grep target | ~1.5K per page |
| store | `store/atlas.sqlite` (or JSON) | machine store behind MCP and the mismatch checks; never read by the LLM | — |

HTML is never a reading format: 1.34-1.61x the tokens, and entity escaping breaks literal grep. JSON/YAML
appears only as front-matter or as MCP structured content.

### File layout (repo-relative, under the firmware's own atlas directory)

```
MAP.md                        L0 index+spine (project-instructions import)
docs.txt                      files.txt-style doc list: id, file, rev, sha, pages, outline
parts/<part>/{CARD.md,toc.md,s/<secid>.md,pages/p<NNN>.txt}   L1 card, outline, L2 sections, L3 text
mcu/<mcu>/{CARD.md,toc.md,s/,reg/<PERIPH>.md}   RM (enabled peripherals + errata), SVD-joined registers
board/<board>/{CARD.md,nets.md,pins.md,parts.md}   schematic analysis (wiring lives here only)
facts/facts.txt               every fact, one per line, all parts + boards
checks/mismatches.md          last mismatch report (also served by MCP)
store/                        machine store, hashes, extraction logs
```

### Draft MCP tool surface (seven tools; the CLI mirrors each as a verb)

Every response starts with one cite header line, e.g. `src: <part>@rev p8-9 §EC-protection prov=D`. It is
Markdown, not JSON, with a concise/full detail switch, and a `next=` cursor when truncated.

| Tool | Input | Output | Budget |
|---|---|---|---|
| `part_card` | `part` (fuzzy) | CARD.md bytes + board bindings if named | ≤ 1.5K |
| `doc_toc` | `doc`, `under?`, `depth=2` | outline rows: id, title, pages, tier-2 file, tokens | ≤ 2.5K |
| `doc_section` | `doc`, `section`/`page`, `tier=distilled\|text` | L2 bytes or page text layer; `next=` offset; a resource link to the PDF | ≤ 6K/page |
| `find_fact` | `query`, `part?`, `kind?`, `limit=20` | ranked `facts.txt` lines (BM25), each with cite | ≤ 2K |
| `register` | `target`, `field?` | address, reset, access, fields, formulas, rules, cites, DT refs | ≤ 1.5K |
| `pin_trace` | `pin`/`net`/`part.pin`, `board?` | MCU pin -> net -> far-end pin -> DT node -> driver, per hop | ≤ 1K/pin |
| `mismatch_report` | `scope?`, `severity?` | claim A @cite vs claim B @cite, rule, status | ≤ 4K/page |

Schema cost (inference): 7 tools at ~150-250 tokens each is ~1-1.75K, small enough for `alwaysLoad: true`.
The CLI verbs are the same functions, so scripts, a future UI and a human share one backend. No generic
"search" or "read file" tool: the agent's own grep and file-read cover files already.

## What this means for atlas (inferences)

- **Keep Markdown as the answer to the owner's question, and put the rules *inside* it.** One condition
  per row, no merged cells, footnotes attached to rows, native names verbatim, an "aka" line, and a page
  cite on every fact. The container choice is worth ~10-60% in tokens. The row rules decide whether grep
  works at all (0-34 of 34).
- **Generate two views of one store:** section Markdown for reading, and `facts.txt` for grep and
  `find_fact`. Both come from the same extraction, so they cannot drift. The ~8% token difference between
  them is disk, not context.
- **Make tier 3 a routine verification step, not a last resort.** At ~1.5K per page it is a cheap "second
  pass" for the trust bar: an LLM-extracted row can be re-checked against the page image.
- **Build the navigation layer from the PDF outline deterministically, before any LLM step.** It is free,
  it exists for all 11 documents, and it gives section IDs and pages.
- **Budget in real model tokens (x1.4 over o200k),** and fail the generator when a tier exceeds its cap.
- **One set of bytes, three doors.** Tier files are plain Markdown on disk; the agent's own file-read and
  grep reach them for free; an MCP "doc section" tool and the CLI return the *same bytes* with a cite
  header. No separate "LLM rendering" can drift away from what the engineer reads. MCP carries only what
  files cannot: joins, register lookups, fact search with filters, and the mismatch report.
- **A skill carries the protocol** (map -> card -> section -> page, cite everything, never answer from
  memory) in under 5K tokens so it survives compaction. The top-level map loads as a project-instructions
  import, never through a tool, so it stays byte-stable and cached.

## Open questions for the owner

1. Should the top-level map load every session (a project-instructions import) or only through the atlas
   skill? The first costs ~5K cached tokens per session.
2. May the tier-3 cross-check run automatically? It would be the coding assistant viewing page images of
   the project's own PDFs, ~1.5K tokens per page, on the subscription.
3. Should the facts index span all parts and boards in one file, or split per part? One file makes grep
   global; per part keeps diffs small.
4. For schematics, is the Altium bookmark netlist (components/nets/ports per sheet) acceptable as a
   *source*, with the page image as the check? Or must nets come from EDA exports when they exist?
5. Is `alwaysLoad` acceptable for the atlas MCP server (~1-1.75K tokens every session), or should it stay
   deferred?

## What I could not verify

- **Real-model counts depend on this setup**: usage deltas from a specific coding-assistant version, not
  a dedicated token-counting endpoint; other models may tokenize differently.
- **The JPEG size** (850x1100 px) is measured directly from the stream's image bytes [corrected in
  verification]; a future client version may render PDF pages differently.
- **The QA runs cannot rank formats**: single, ceiling-limited runs (n = 15 twice); the register-map-page
  result may include memorization; no navigation test (finding a fact in a whole datasheet) was run — the
  A/B test that matters next.
- **The schematic outline counts** come from a parse of bookmark levels, not checked against a netlist;
  the tree may not list every pin.
- **Opened only as abstracts:** the interactive-navigation, recursive-summary and section-sized-retrieval
  papers; the hierarchical-tree product's benchmark is the vendor's own.

## Sources

External, all opened 2026-09-29: https://arxiv.org/html/2411.19504v2 · https://arxiv.org/html/2601.04202
· https://arxiv.org/html/2606.09578 · https://arxiv.org/abs/2411.02959 · https://arxiv.org/abs/2407.01523
· https://arxiv.org/html/2606.23704 · https://github.com/VectifyAI/PageIndex ·
https://arxiv.org/abs/2401.18059 · https://arxiv.org/abs/2310.05029 · https://arxiv.org/abs/2406.15319 ·
https://arxiv.org/html/2602.05014v3 · https://arxiv.org/abs/2605.27123 ·
https://www.anthropic.com/engineering/contextual-retrieval · https://arxiv.org/abs/2407.01449 (+ the
ICLR 2025 proceedings PDF) · https://arxiv.org/abs/2505.17166 · https://arxiv.org/html/2506.05690v3 ·
https://arxiv.org/abs/2607.16604 · https://platform.claude.com/docs/en/build-with-claude/{pdf-support,
vision,citations} · https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
· https://www.anthropic.com/engineering/{writing-tools-for-agents,code-execution-with-mcp,advanced-tool-use}
· https://modelcontextprotocol.io/specification/2025-06-18/server/{tools,resources,utilities/pagination}
· https://code.claude.com/docs/en/{mcp,skills} · https://llmstxt.org/ ·
https://huggingface.co/Xenova/claude-tokenizer

See also: [01-agent-code-intelligence.md](01-agent-code-intelligence.md), [13-mcu-reference.md](13-mcu-reference.md), [17-ingestion-procedures.md](17-ingestion-procedures.md).

## Verification (2026-09-29, independent)

| Claim | Verdict | Evidence |
|---|---|---|
| Token ratios across encodings | corrected | within ±6 tokens; line-kv sample C 1.05x, dedup sample 0.90x |
| Real/o200k ratio 1.29-1.63, median 1.40 | confirmed | recomputed |
| Grep yield 34/0/18-19/5 | confirmed | real `grep -w` re-run |
| Read-cost deltas (145/1,471/3,273) | confirmed | exact re-run |
| JPEG size "inferred" | corrected | measured directly from image header: 850x1100 |
| QA 15/15, 14/15, register-page result | confirmed | re-graded |
| Outline counts | confirmed | re-parsed with a PDF library |
| Locate-then-read paper: +10.3, 18.9 | confirmed | checked against its results table |
| Lexical-query paper: 0.717, 41x | confirmed | checked against its body text |
| Hierarchical-tree product: licence, cloud-only | confirmed | README |
| §1 study numbers | confirmed | cited pages |
| §4 client-limit numbers | confirmed | cited pages |
