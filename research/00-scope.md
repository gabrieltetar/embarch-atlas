# embarch-atlas, document edition: research scope

**Status:** fixed 2026-09-29, before the research started.

This is the generalized, public-safe copy of the scope note for the document
extension of atlas research. It says what changed and what was decided before
the research ran. The full-fidelity version, with the target project's real
part numbers, file paths and spike artifacts, lives in that project's
machine-local atlas folder.

## What changed from the prior scope

A prior survey scoped atlas to code structure, build-config reality and
hardware binding, measured on an existing firmware project. This pass
**extends** that atlas; it does not restart it. The prior findings are the
base and are not re-researched, only built on.

What is new: atlas must also let an LLM understand a product's **hardware
schematics, component datasheets and MCU reference manual**, joined to the
firmware, without reading any of them whole. The proof-of-concept target is a
small, custom Zephyr board around an STM32-family MCU (referred to below as
`<MCU>`).

## Answers held (owner)

| Question | Answer |
|---|---|
| Relation to prior atlas | Extend it. Same sub-project, scope widened to documents |
| Use cases | All four: write drivers; debug across hardware/software; consistency review (schematic vs devicetree vs code vs datasheet); whole-picture map |
| **First step** | **Convert the PDFs (schematics, datasheets, reference manual) into smaller files through written, repeatable preprocessing procedures.** The conversion is part of atlas, not a manual chore |
| Document sourcing | The engineer supplies every document locally. **The tool never fetches anything** |
| Remote services | None for parsing. The only remote is Claude, via Claude Code |
| Schematic source | **PDF is the norm.** Native EDA project files are a bonus when they exist |
| Datasheet vs wiring | A datasheet distillation is **board-independent**. Wiring (net names, which pins are used) belongs to the **schematic analysis**, not the datasheet doc |
| Depth | **Tiered**: short card, then full distilled markdown section by section (page-cited), then raw PDF page on demand. Nothing is dropped; it is layered |
| Reference manual depth | The register-description file (SVD) for registers; reference-manual prose distilled **only for peripherals the build actually enables**, plus the errata sheet |
| Output home | Everything per project under `<fw>/embarch/atlas/`, local, git-excluded |
| Structured facts | Register maps; limits and thresholds; timing and sequencing; pins and behaviour rules; **plus any part-specific quirk that should make an agent curious** |
| LLM runner | **Claude Code sessions** (skills/agents on the subscription). Scripts do the deterministic part. No API pipeline |
| Trust bar | Every fact **cited** (doc + page/section). LLM-extracted facts get a **deterministic or second-pass cross-check**; failures are marked unverified |
| Interface | **MCP server, CLI, a UI view** (the UI is out of scope for now: a solid backend first). Open question: **is markdown the best form for an LLM to browse?** |
| Other inputs | Product requirements documents. Not in scope: internal review sheets, engineer notes, bench data |
| Targets | One product for the first proof, but the design must handle **multi-board products** |
| First proof | Distilled docs; a pin/net map (every MCU pin -> net -> far-end part pin -> devicetree node -> driver); a mismatch report |
| Stack | Open. The research decides |
| Research spikes | Allowed: install anything into a scratch venv, model downloads included. Client PDFs never leave the machine |

## Added after the research launched

The research agents did not see this section. It governs the skill drafting
that follows the research.

| Question | Answer |
|---|---|
| Skills | Three: **translate docs** (PDF to tiered distillations, with extensive checks); **build atlas** (translated docs + firmware -> atlas); **update atlas** |
| Versioning | **Many atlases may exist; each is built for one board + one firmware commit.** An atlas is fixed to that pair |
| Build artifacts | The build skill checks out the pinned commit in **its own git worktree** and builds there. The engineer's working tree and build dirs are never touched, and the artifacts provably match the commit |
| Skill home | Source of truth in a dedicated skills repo, installed the way the suite's other shared tooling is installed, so every firmware project gets them |
| Timing | The skills are drafted **after** the research lands |

Inference, not decided: translated datasheets are board-independent, so one
translation could serve every atlas in a project, each atlas referencing the
translations by content hash.

## Answers to the synthesis note's skill-blocking questions (owner)

| # | Question | Owner | Applied as |
|---|---|---|---|
| 1 | Licences: a copyleft PDF library, a vendor's proprietary configurator database as an input | "Yes, EmbArch is MIT-licensed open source, we can use those" | Both allowed. Two conditions keep the repo MIT: the copyleft library is a **runtime dependency**, never vendored, so its licence binds whoever runs the tool, not the MIT code; the vendor database is **read from the engineer's local install**, never copied or committed |
| 2 | When does a fact get its second, independent look? | "On building the atlas" | translate-docs does the first pass plus the deterministic checks. **build-atlas runs the second look** (fresh subagent, page image) on the facts that atlas actually uses and that are flagged or high-stakes: figure-only, unit prefix, conditions, limits, quirks, register semantics. The result is cached by content hash, so the next atlas reuses it |
| 3 | Record format | "The best for LLM reading it" | The LLM reads markdown sections (row rules, see note 14) and **one-fact-per-line text**. The machine source of truth is **JSON Lines with a JSON Schema**; it never needs reading, and it avoids YAML 1.1's `yes` -> `True` trap (note 11). Facts are per part; a recursive grep over the facts directory is the global view |
| 4 | Map loading and always-loaded MCP tools | "Pick the best option for LLM usage" | **The MAP (<= 5K tokens, byte-stable) loads every session** through an untracked, git-excluded local file in the firmware repo, so it is cached and never goes through a tool. **The MCP server is always-loaded** (7 tools, ~1-1.75K tokens), which avoids a tool-search round trip on first use. With several atlases, the one matching the checkout's board + commit loads, and a stale one says so |
| 5 | Reference-manual chapter selection | "If a peripheral is in use" | Per-peripheral sections (note 13's rule, not whole chapters), plus the cross-cutting core (clock control, power, external interrupts, GPIO, DMA/DMA-mux, interrupt controller, system config, flash). **In use = instantiated in the build's ELF, or touched by SoC code.** Devicetree-okay with no driver is a mismatch-report item, not "in use". Because this depends on the build, **build-atlas asks for reference-manual chapters and translate-docs distils them lazily**, cached by content hash. The per-page reference-manual text layer is kept whole as the raw tier (disk only) |

## Carried over from the prior scope

- Consumer: a coding agent, mid-task, in a firmware repo.
- Interface shape: a small always-loaded map, backed by a drill-down query
  surface.
- Fully local; must survive a real client firmware project, not a toy.
- Runtime fusion (static map + EmbArch's own traces) stays a later optional
  layer; the design must not foreclose it.

## Layout of this folder

- `00-scope.md`: this file.
- `research/NN-<slug>.md`: one note per territory, findings with sources,
  inferences marked as such, a `## Verification` section appended by an
  independent checker.
- `README.md`: index and synthesis.

These files are the generalized, public-safe copies. Full-fidelity versions —
with the target project's real part numbers, board names, file paths and
spike scripts/outputs — live in that project's machine-local atlas folder,
not in this repo.
