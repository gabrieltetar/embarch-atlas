# embarch-atlas

Lets a coding agent understand a firmware project's hardware documents (schematic PDFs, component datasheets, the MCU reference manual, the PRD), joined to its Zephyr build, without reading any of them whole.

Part of the [EmbArch](https://github.com/gabrieltetar/embarch-doc) suite. Design: [DESIGN.md](DESIGN.md).

Three Claude Code skills drive it, and the `embarch-atlas` CLI does every deterministic step:

- `/atlas-translate-docs`: turn the supplied PDFs into tiered, page-cited, cross-checked distillations and a schematic netlist.
- `/atlas-build`: build an atlas for one board and one firmware commit, in its own git worktree.
- `/atlas-update`: build the next atlas, reusing every translation whose source did not change.

Fully local. The tool never fetches a document.

MIT licensed.
