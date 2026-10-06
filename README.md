# embarch-atlas

Lets a coding agent understand a firmware project's hardware documents (schematic PDFs,
component datasheets, the MCU reference manual, the PRD), joined to its Zephyr build,
without reading any of them whole.

Part of the [EmbArch](https://github.com/gabrieltetar/embarch-doc) suite. Design:
[DESIGN.md](DESIGN.md). The research behind it: [research/](research/README.md).

## What you get

For one board and one firmware commit, an atlas under `<fw>/embarch/atlas/` (git-excluded):

- **Distilled documents**, board-independent and tiered: a card per part (≤ 1.5K tokens),
  page-cited sections, one fact per line in `facts.txt`, and the raw page on demand. Every
  number is anchored to the page it cites, or tagged `(derived)`, `(figure)`, `(inference)`.
- **The schematic as data**: netlist and BOM read from Altium Smart-PDF bookmarks and
  component popups, cross-checked by tracing the drawn wires; board-to-board interfaces
  proposed with a score, confirmed by the engineer.
- **The pin/net map**: every MCU pin → net → far-end part pin → devicetree node → driver.
- **A mismatch report** that leads with the checks that passed: I2C identity by address
  strap, pinctrl vs net names, DT values vs BOM, driver register constants vs the
  datasheet's register map, SVD vs reference manual, a blind second reading of the facts
  the join depends on.
- **Queries** over all of it: `embarch-atlas q ...` and the same twelve lookups as an MCP
  server.
- **A map for humans** (`graph.json`, drawn by embarch-ui's Atlas tab): app, middleware,
  drivers, SoC pins and every part on every board as a generic schematic symbol, each pin
  labelled with its net and, where the MCU drives it, the firmware's name for it.

Fully local. **The tool never fetches a document**: a missing one becomes a question for
the engineer. The only LLM is the Claude Code session running the skills.

## Install

Python 3.12+, poppler (`pdftotext`, `pdfinfo`, `pdftoppm`), `uv`, and a Zephyr west
workspace for the builds.

```sh
./install.py            # .venv, the `embarch-atlas` command in ~/.local/bin, the skills in ~/.claude/skills
./install.py --check    # drift report
```

## Use

In the firmware repo:

```sh
embarch-atlas init .    # atlas.toml; wires CLAUDE.local.md (the map loads every session) and .mcp.json, both git-excluded
```

Fill `embarch/atlas/atlas.toml` (boards, targets, the MCU's SVD path), drop the documents
into the sources dir (`embarch/hardware/` by default), then in Claude Code:

| Skill | Does |
|---|---|
| `/atlas-translate-docs` | inventory, schematic netlist, the missing-documents questions, and one distillation per document, each checked and frozen |
| `/atlas-build <target> [commit]` | a worktree build at the commit, the joins, the mismatch report, `MAP.md`, the second look |
| `/atlas-update <target> [commit]` | the next atlas, reusing every unchanged translation and verdict, and a diff |
| `atlas-use` | loaded by the agent itself: how to answer a hardware question from the atlas |

`embarch-atlas --help` lists every deterministic step the skills call.

## Status

First cut, exercised on one real two-board product: seven datasheets, an MCU reference
manual of over a thousand pages, and a PRD. What is not built yet is listed at the end of
[DESIGN.md](DESIGN.md).

MIT licensed.
