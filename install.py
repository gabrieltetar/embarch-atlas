#!/usr/bin/env python3
"""Install embarch-atlas for this user: the CLI shim and the three skills.

- The package goes into this repo's `.venv` (editable), created with `uv` if missing.
- `~/.local/bin/embarch-atlas` is a two-line shim that execs the venv's entry point,
  so skills can call `embarch-atlas` from any project directory.
- Each `skills/<name>/` is **copied** to `~/.claude/skills/<name>/`, with a
  `.embarch-atlas-install` stamp holding the source tree's hash. A copy, not a
  symlink: an installed skill should change only when someone re-installs, never
  because a working tree moved under it.

The skills carry `disable-model-invocation: true`, so an agent session (the
fleet's included) can list them but never invoke one by itself.

Usage:
  ./install.py            install or refresh
  ./install.py --check    report drift; exit 1 if anything is stale or missing
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILLS_SRC = REPO / "skills"
SKILLS_DST = Path.home() / ".claude" / "skills"
SHIM = Path.home() / ".local" / "bin" / "embarch-atlas"
VENV_BIN = REPO / ".venv" / "bin"
STAMP = ".embarch-atlas-install"

SHIM_TEXT = f"""#!/usr/bin/env bash
# Installed by {REPO}/install.py -- re-run it rather than editing this file.
exec "{VENV_BIN / 'embarch-atlas'}" "$@"
"""


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != STAMP and "__pycache__" not in p.parts:
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def skills() -> list[Path]:
    return sorted(p for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").is_file())


def check() -> list[str]:
    problems = []
    if not (VENV_BIN / "embarch-atlas").exists():
        problems.append(f"venv entry point missing: {VENV_BIN / 'embarch-atlas'}")
    if not SHIM.exists() or SHIM.read_text() != SHIM_TEXT:
        problems.append(f"shim missing or edited: {SHIM}")
    for src in skills():
        dst = SKILLS_DST / src.name
        stamp = dst / STAMP
        if not stamp.exists():
            problems.append(f"skill not installed: {dst}")
        elif stamp.read_text().strip() != tree_hash(src):
            problems.append(f"skill stale (source changed since install): {dst}")
        elif tree_hash(dst) != tree_hash(src):
            problems.append(f"skill hand-edited after install: {dst}")
    return problems


def install() -> None:
    if not (VENV_BIN / "python").exists():
        subprocess.run(["uv", "venv", str(REPO / ".venv")], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "-p", str(VENV_BIN / "python"), "-e", f"{REPO}[tokens]"],
                   check=True)
    SHIM.parent.mkdir(parents=True, exist_ok=True)
    SHIM.write_text(SHIM_TEXT)
    SHIM.chmod(0o755)
    SKILLS_DST.mkdir(parents=True, exist_ok=True)
    for src in skills():
        dst = SKILLS_DST / src.name
        if dst.exists() and not (dst / STAMP).exists():
            sys.exit(f"refusing to overwrite {dst}: it was not installed by this script")
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        (dst / STAMP).write_text(tree_hash(src) + "\n")
        print(f"installed {dst}")
    print(f"installed {SHIM}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        problems = check()
        for p in problems:
            print(p)
        print("current" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    install()
    return 0


if __name__ == "__main__":
    sys.exit(main())
