"""The live module: MCU registers read on the running board through embarch-core, decoded here.

The atlas never touches hardware (DESIGN.md, "Live"). It turns a register name into addresses,
asks embarch-core for those words through the `embarch-api` CLI (Core resolves the probe for the
role from its topology, checks the board's identity, halts a running core for the read and lets it
run again), and decodes what comes back against the SVD's register and field records.

Read-only is not side-effect free. A register whose read clears or pops something is refused by
name unless the caller passes `allow_clear`, and a whole-peripheral read leaves it out. What counts:
a field the SVD marks `readAction` (kept as access=rclr), and a reference-manual field that says it
is cleared by reading another register ("cleared by reading the USART_RDR register").

Off unless Core answers: `available()` asks `embarch-api status`, and the MCP server lists
`reg_read` only when it does.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .common import read_jsonl

# Core's caps (embarch-core decision 81): words per request over all ranges, ranges per request.
MAX_WORDS = 256
MAX_RANGES = 32
# Reserved words a block read may span between two registers rather than start another range.
GAP_WORDS = 8
STATUS_TIMEOUT_S = 8
READ_TIMEOUT_S = 90

# "<REG.FIELD> is cleared by reading <REG>", in the wordings a reference manual uses.
_TOK = r"(?:the\s+)?([A-Z][A-Za-z0-9]*_[A-Za-z0-9_]*[A-Za-z0-9])"
CLEAR_ON_READ = [re.compile(p) for p in (
    r"\b(?:by|upon|after|on)\s+(?:a\s+|any\s+|each\s+)?(?:read|reading)\s+(?:access\s+|operation\s+)?"
    r"(?:to|of|from|in)?\s*" + _TOK,
    r"\bwhen\s+" + _TOK + r"(?:\s+register)?\s+is\s+read\b",
    r"\b(?:[Ee]very|[Aa]ny|[Ee]ach)\s+read(?:\s+operation)?\s+(?:of|from)\s+" + _TOK,
    r"\b[Rr]eading\s+" + _TOK + r"(?:\s+register)?\s+(?:clears|resets|frees|empties|pops)\b",
)]


class LiveError(RuntimeError):
    """Core refused or failed the read; the message is Core's own."""


# --------------------------------------------------------------------------- transport

def settings(atlas_dir: Path) -> dict[str, Any]:
    """`[live]` in atlas.toml: `api` (the embarch-api binary), `api_config` (its config, default
    `<fw>/embarch/embarch.toml`), `role` (default "dut")."""
    from .project import fw_dir, load_config
    try:
        cfg = load_config(atlas_dir)
    except FileNotFoundError:
        cfg = {}
    live = cfg.get("live") or {}
    api_config = live.get("api_config") or str(fw_dir(atlas_dir, cfg) / "embarch" / "embarch.toml")
    return {"api": live.get("api") or "embarch-api", "api_config": api_config, "role": live.get("role") or "dut"}


def call_api(s: dict[str, Any], args: list[str], timeout: float) -> dict[str, Any]:
    """Run `embarch-api --json <args>` and return its JSON; LiveError when it says it failed."""
    cmd = [s["api"], "--config", s["api_config"], "--json", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise LiveError(f"{s['api']} not found; install embarch-api or set [live] api in atlas.toml")
    except subprocess.TimeoutExpired:
        raise LiveError(f"embarch-api {args[0]} gave no answer in {timeout:.0f} s")
    try:
        out = json.loads(p.stdout)
    except ValueError:
        tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
        raise LiveError(f"embarch-api {args[0]} exited {p.returncode}: " + " | ".join(tail))
    if not out.get("success"):
        raise LiveError(str(out.get("error") or f"embarch-api {args[0]} failed"))
    return out


def available(atlas_dir: Path) -> str | None:
    """None when embarch-core answers; else why the live tools stay off."""
    try:
        call_api(settings(atlas_dir), ["status"], STATUS_TIMEOUT_S)
    except LiveError as e:
        return str(e)
    return None


def core_read(s: dict[str, Any], ranges: list[tuple[int, int]], role: str) -> dict[str, Any]:
    args = ["mem-read", "--role", role]
    for addr, words in ranges:
        args += ["--range", f"0x{addr:08X}:{words}"]
    return call_api(s, args, READ_TIMEOUT_S)["read"]


# --------------------------------------------------------------------------- records

def _docs_of_kind(atlas_dir: Path, kind: str) -> list[Path]:
    out = []
    for d in sorted((Path(atlas_dir) / "docs").glob("*")):
        try:
            if json.loads((d / "doc.json").read_text(encoding="utf-8")).get("kind") == kind:
                out.append(d)
        except (OSError, ValueError):
            continue
    return out


def _jsonl(path: Path) -> list[dict]:
    try:
        return list(read_jsonl(path))
    except (OSError, ValueError):
        return []


def periph_of(r: dict) -> str:
    rid = r.get("id") or ""
    return rid.split(".reg.", 1)[1].split(".", 1)[0] if ".reg." in rid else (r.get("periph") or "")


def family(periph: str) -> str:
    """A peripheral instance's family: USART2 -> USART, I2C1 -> I2C, TIM14 -> TIM."""
    return periph.rstrip("0123456789") or periph


def short_name(name: str, periph: str) -> str:
    """A register's name without its family prefix: UCPD_RXDR -> RXDR (an SVD often keeps it)."""
    fam = family(periph)
    return name[len(fam) + 1:] if name.upper().startswith(fam.upper() + "_") else name


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _addr(r: dict) -> int | None:
    try:
        return int(str(r.get("addr")), 0)
    except (TypeError, ValueError):
        return None


class Svd:
    """The MCU's register and field records, from the atlas's SVD document."""

    def __init__(self, doc: Path):
        self.doc = doc
        self.regs = [r for r in _jsonl(doc / "records" / "register.jsonl")
                     if r.get("kind") == "register" and _addr(r) is not None]
        self.fields: dict[str, list[dict]] = {}
        for f in _jsonl(doc / "records" / "field.jsonl"):
            if f.get("kind") == "field" and f.get("reg"):
                self.fields.setdefault(f["reg"], []).append(f)
        for fs in self.fields.values():
            fs.sort(key=lambda f: -((f.get("bits") or [0])[0]))

    def periphs(self) -> dict[str, str]:
        """Normalised name -> peripheral instance as the SVD spells it."""
        return {_norm(periph_of(r)): periph_of(r) for r in self.regs}

    def resolve(self, target: str) -> tuple[str, list[dict]]:
        """(peripheral, registers) for `PERIPH` (all of it) or `PERIPH.REG`. A name the SVD splits
        into alternate views at one address (CR1_FIFO_ENABLED, CR1_FIFO_DISABLED) gives them all."""
        head, _, reg = target.partition(".")
        periph = self.periphs().get(_norm(head))
        if not periph:
            raise LookupError(f"no peripheral {head!r} in {self.doc.name}")
        mine = [r for r in self.regs if periph_of(r) == periph]
        if not reg:
            return periph, sorted(mine, key=lambda r: (_addr(r), r.get("name")))
        rn = _norm(reg)
        exact = [r for r in mine if rn in (_norm(r.get("name")), _norm(short_name(r.get("name", ""), periph)))]
        if exact:
            return periph, exact
        alt = [r for r in mine if short_name(r.get("name", ""), periph).upper().startswith(reg.upper() + "_")]
        if alt:
            return periph, alt
        names = ", ".join(sorted({short_name(r.get("name", ""), periph) for r in mine})[:40])
        raise LookupError(f"no register {reg!r} in {periph}; it has: {names}")


def rm_clears(rm_dirs: Iterable[Path]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(family, short register name) -> [(the REG.FIELD a read of it clears, cite)], from the
    reference manual's per-register field text (`rm_regs.json`)."""
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for d in rm_dirs:
        try:
            regs = json.loads((d / "rm_regs.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in regs:
            for f in r.get("fields") or []:
                text = f.get("text") or ""
                for pat in CLEAR_ON_READ:
                    for m in pat.finditer(text):
                        prefix, _, short = m.group(1).partition("_")
                        if not short:
                            continue
                        key = (prefix.rstrip("x").rstrip("0123456789").upper(), short.upper())
                        what = (f"{r.get('name')}.{f.get('name')}", f"{d.name} p{f.get('page') or r.get('page')}")
                        if what not in out.setdefault(key, []):
                            out[key].append(what)
    return out


def side_effects(reg: dict, fields: list[dict], clears: dict) -> list[str]:
    """Why reading `reg` changes the target, one reason per effect; empty when it does not."""
    why = [f"{f.get('name')} is cleared by the read ({f.get('doc')}: readAction)"
           for f in fields if "clr" in str(f.get("access") or "") or "clear" in str(f.get("access") or "")]
    periph = periph_of(reg)
    key = (family(periph).upper(), short_name(reg.get("name", ""), periph).upper())
    why += [f"reading it clears {what} ({cite})" for what, cite in clears.get(key, [])]
    return why


# --------------------------------------------------------------------------- plan and decode

def plan(regs: list[dict], skip: set[int]) -> list[tuple[int, int]]:
    """The word ranges that cover `regs`, never touching a word in `skip`, merging neighbours
    across at most GAP_WORDS reserved words."""
    words = sorted({_addr(r) & ~3 for r in regs} - skip)
    ranges: list[list[int]] = []
    for w in words:
        if ranges:
            start, n = ranges[-1]
            end = start + 4 * n
            gap = range(end, w, 4)
            if (w - end) // 4 <= GAP_WORDS and not any(g in skip for g in gap) and (w - start) // 4 + 1 <= MAX_WORDS:
                ranges[-1][1] = (w - start) // 4 + 1
                continue
        ranges.append([w, 1])
    return [(a, n) for a, n in ranges]


def word_map(read: dict) -> dict[int, int]:
    return {rng["address"] + 4 * i: w for rng in read.get("ranges") or [] for i, w in enumerate(rng.get("words") or [])}


def reg_value(words: dict[int, int], reg: dict) -> int | None:
    a = _addr(reg)
    w = words.get(a & ~3)
    if w is None:
        return None
    width = int(reg.get("width") or 32)
    return (w >> (8 * (a & 3))) & ((1 << width) - 1)


def _bits(f: dict) -> tuple[int, int]:
    b = f.get("bits") or [0, 0]
    return (b[0], b[1]) if len(b) == 2 else (b[0], b[0])


def field_value(v: int, f: dict) -> int:
    hi, lo = _bits(f)
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def _int(x) -> int | None:
    try:
        return int(str(x), 0)
    except (TypeError, ValueError):
        return None


def _enum(f: dict, v: int) -> str:
    e = f.get("enum") or {}
    text = e.get(str(v))
    return re.sub(r"^B_0x[0-9A-Fa-f]+:\s*", "", text) if text else ""


def _rng(f: dict) -> str:
    hi, lo = _bits(f)
    return f"[{hi}:{lo}]" if hi != lo else f"[{hi}]"


def decode(reg: dict, v: int, fields: list[dict], all_fields: bool) -> list[str]:
    """The register's value and its fields; a `*` marks a field that is not at its reset value."""
    periph, name = periph_of(reg), reg.get("name")
    width = int(reg.get("width") or 32)
    reset = _int(reg.get("reset"))
    head = f"{periph}.{name} @ {reg.get('addr')} = 0x{v:0{width // 4}X}"
    if reset is not None:
        head += f"  reset 0x{reset:0{width // 4}X}"
    if reg.get("access") == "wo":
        return [head + "  (write-only: reads back as 0 or junk, not decoded)"]
    out, same = [head], 0
    covered = 0
    for f in fields:
        hi, lo = _bits(f)
        covered |= ((1 << (hi - lo + 1)) - 1) << lo
        fv = field_value(v, f)
        fr = field_value(reset, f) if reset is not None else None
        differs = fr is not None and fv != fr
        if not differs:
            same += 1
            if not all_fields:
                continue
        line = f"  {'*' if differs else ' '} {f.get('name')}{_rng(f)} = {fv:#x}"
        if fr is not None and differs:
            line += f" (reset {fr:#x})"
        e = _enum(f, fv)
        if e:
            line += f"  {e}"
        out.append(line)
    stray = v & ~covered & ((1 << width) - 1)
    if fields and stray:
        out.append(f"  ! bits outside every field are set: 0x{stray:X}")
    if not all_fields and same:
        out.append(f"    ({same} field{'s' if same != 1 else ''} at reset)")
    return out


# --------------------------------------------------------------------------- the tool

def reg_read(atlas_dir: Path, target: str, allow_clear: bool = False, role: str | None = None) -> str:
    """`PERIPH.REG` or a whole `PERIPH` read on the running board, decoded field by field."""
    svds = _docs_of_kind(atlas_dir, "svd")
    if not svds:
        return "src: none\n\nNo SVD in this atlas; reg_read decodes MCU registers only.\n"
    svd = Svd(svds[0])
    head = target.partition(".")[0]
    try:
        periph, regs = svd.resolve(target)
    except LookupError as e:
        hint = (" External parts' registers are read through the live shell, which is not built yet."
                if head and _norm(head) not in svd.periphs() else "")
        return f"src: none\n\n{e}.{hint}\n"
    clears = rm_clears(_docs_of_kind(atlas_dir, "reference-manual"))
    effects = {r["id"]: side_effects(r, svd.fields.get(r["id"], []), clears) for r in regs}
    guarded = [r for r in regs if effects[r["id"]]]
    single = "." in target
    if guarded and not allow_clear and single:
        lines = [f"- {r.get('name')}: " + "; ".join(effects[r["id"]]) for r in guarded]
        return ("src: none\n\nRefused: reading this register changes the target.\n" + "\n".join(lines)
                + "\nCall again with allow_clear=true if clearing it is the point.\n")
    skip = set() if allow_clear else {_addr(r) & ~3 for r in guarded}
    to_read = [r for r in regs if (_addr(r) & ~3) not in skip]
    ranges = plan(to_read, skip)
    words_total = sum(n for _, n in ranges)
    if len(ranges) > MAX_RANGES or words_total > MAX_WORDS:
        return (f"src: none\n\n{periph} spans {words_total} words in {len(ranges)} ranges; one read takes at most "
                f"{MAX_WORDS} words in {MAX_RANGES} ranges. Ask for its registers by name.\n")
    s = settings(atlas_dir)
    who = role or s["role"]
    try:
        read = core_read(s, ranges, who)
    except LiveError as e:
        return f"src: none\n\nThe read failed: {e}\n"
    words = word_map(read)
    halt = (f"core halted {read.get('halted_us')} us and let run again" if read.get("halted")
            else "core was already halted, left halted")
    out = [f"src: live {read.get('role')} {read.get('chip')} via embarch-core (probe {read.get('probe_serial')}), "
           f"{halt}; decoded with {svd.doc.name}", ""]
    seen_addr: dict[int, str] = {}
    for r in to_read:
        a = _addr(r)
        if not single and a in seen_addr:
            out.append(f"  (also viewed as {r.get('name')} at {r.get('addr')})")
            continue
        seen_addr[a] = r.get("name")
        v = reg_value(words, r)
        if v is None:
            out.append(f"{periph}.{r.get('name')} @ {r.get('addr')}: not in the answer")
            continue
        out += decode(r, v, svd.fields.get(r["id"], []), all_fields=single)
        if effects[r["id"]]:
            out.append("  ! this read cleared: " + "; ".join(effects[r["id"]]))
    if guarded and not allow_clear:
        out += ["", "Not read (reading changes the target; allow_clear=true reads them):"]
        out += [f"- {r.get('name')}: " + "; ".join(effects[r["id"]]) for r in guarded]
    if not single:
        out += ["", "`*` marks a field not at its reset value; ask for PERIPH.REG for every field."]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- cli

def _cmd_reg_read(args) -> int:
    from . import common
    print(reg_read(common.atlas_dir(args.atlas_dir), args.target, args.allow_clear, args.role), end="")
    return 0


def register(sub, groups) -> None:
    from . import common
    g = common.verb_group(sub, groups, "live", "read the running board through embarch-core (live module)")
    p = g.add_parser("reg-read", help="an MCU register (PERIPH.REG) or a whole peripheral, read on the board "
                                      "and decoded field by field")
    p.add_argument("target")
    p.add_argument("--allow-clear", action="store_true",
                   help="read a register even though reading it clears something")
    p.add_argument("--role", help="the enrolled role to read from (default [live] role, else dut)")
    p.set_defaults(func=_cmd_reg_read)
