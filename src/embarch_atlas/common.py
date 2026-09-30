"""Shared plumbing: paths, hashing, JSON Lines, stable writes."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable, Iterator


def atlas_dir(explicit: str | None = None, start: Path | None = None) -> Path:
    """`<git toplevel>/embarch/atlas`, unless given explicitly."""
    if explicit:
        return Path(explicit).resolve()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=start or Path.cwd(),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(top) / "embarch" / "atlas"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, data: Any) -> None:
    """Byte-stable JSON: sorted keys, 2-space indent, trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path}:{n}: {e}") from e


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def count_tokens(text: str) -> int | None:
    """o200k_base count as a proxy (real Claude counts run ~1.4x). None if tiktoken is absent."""
    try:
        import tiktoken
    except ImportError:
        return None
    return len(tiktoken.get_encoding("o200k_base").encode(text))


def verb_group(sub, groups: dict, name: str, help: str):
    """Nested verbs shared by several modules (`doc prep`, `doc check`, `mcu svd`...).

    The first module to ask creates the group; later ones reuse it.
    """
    if name not in groups:
        p = sub.add_parser(name, help=help)
        groups[name] = p.add_subparsers(dest=f"{name}_verb", required=True)
    return groups[name]
