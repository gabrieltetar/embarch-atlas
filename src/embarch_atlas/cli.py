"""`embarch-atlas <verb>`: the deterministic half of every atlas skill.

Each module that owns verbs exposes `register(subparsers)`; this file only lists
them, so modules can be written in parallel without touching each other.
"""
from __future__ import annotations

import argparse
import importlib
import sys

# Module name -> registers its own verbs. Order is the order in `--help`.
MODULES = [
    "project",
    "inventory",
    "smartpdf",
    "interfaces",
    "missing",
    "pdftext",
    "check",
    "render",
    "regparse",
    "freeze",
    "columns",
    "svd",
    "vote",
    "rm",
    "build",
    "join",
    "graph",
    "codecheck",
    "secondlook",
    "diff",
    "update",
    "query",
    "mcp_server",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="embarch-atlas", description=__doc__.splitlines()[0])
    parser.add_argument("--atlas-dir", help="the project's atlas dir (default: <git toplevel>/embarch/atlas)")
    sub = parser.add_subparsers(dest="verb", required=True)
    groups: dict[str, argparse._SubParsersAction] = {}
    for name in MODULES:
        try:
            mod = importlib.import_module(f"embarch_atlas.{name}")
        except ModuleNotFoundError as e:
            if e.name == f"embarch_atlas.{name}":
                continue  # not written yet
            raise
        mod.register(sub, groups)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
