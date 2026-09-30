# embarch-atlas

## Docs

[DESIGN.md](DESIGN.md) is the contract: on-disk layout, identifiers, record schema, CLI verbs, what an LLM step may and may not do. Read it before changing a module. It moves to `../embarch-doc/embarch-atlas/` once that repo is free to edit; until then it is the source of truth.

## This repo is public

**No client material, ever.** No client names (the pre-push hook refuses them), and also no client *internals*: net names, refdes, BOM lines, pin maps, file paths, part lists or build output from a client project. Tests use synthetic fixtures or public data only. A real project is a runtime input: its outputs are written to that project's `<fw>/embarch/atlas/`, which is git-excluded there, and never copied here. Run `python3 ../embarch-fleet/scripts/check-client-names.py --repo .` before every commit, not just before a push: the hook checks the tree, and a name in an earlier commit is still in pushed history.

## Git

**Work directly on `main`** (suite rule, `../embarch-doc/embarch-dev-workflow.md` §6). Commit once `.venv/bin/pytest -q` is green.

## Python

Python 3.12. `./install.py` creates `.venv`, installs the package editable (`records.py` reads `schemas/` from the repo root, so a wheel install is not supported yet), writes the `~/.local/bin/embarch-atlas` shim and copies the skills to `~/.claude/skills/`; `./install.py --check` reports drift. Each module owning CLI verbs exposes `register(sub, groups)`; `cli.py` only lists module names. Poppler CLI tools (`pdftotext`, `pdftoppm`, `pdfinfo`) are called as subprocesses. PyMuPDF (AGPL) is a runtime dependency, never vendored. The STM32CubeMX database is read from the engineer's local install, never copied.
