"""The live module: MCU registers read on the running board through embarch-core, decoded here.

The atlas never touches hardware (DESIGN.md, "Live"). It turns a register name into addresses,
asks embarch-core for those words through the `embarch-api` CLI (Core resolves the probe for the
role from its topology, checks the board's identity, halts a running core for the read and lets it
run again), and decodes what comes back against the SVD's register and field records.

Read-only is not side-effect free. A register whose read clears or pops something is refused by
name unless the caller passes `allow_clear`, and a whole-peripheral read leaves it out. What counts:
a field the SVD marks `readAction` (kept as access=rclr), and a reference-manual field that says it
is cleared by reading another register ("cleared by reading the USART_RDR register").

External parts' registers go through the board's shell instead: `shell()` runs one command on
the console signal the role declares (Core's exchange route), and `reg_read <part>.<REG>` turns a
register record into `i2c read` commands for the bus and address the devicetree gives, reads the
part's `bus` record for its byte order and address windows, and decodes the bytes the same way.

Off unless Core answers: `available()` asks `embarch-api status`, and the MCP server lists
`reg_read` and `shell` only when it does.
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

# Exchanges per external read: one per register (a 16-bit pair is two).
MAX_EXCHANGES = 24
EXCHANGE_TIMEOUT_MS = 3000
DEFAULT_PROMPT = "uart:~$ "
# Core waits this long for a console port that is gone (a USB console re-enumerating after a
# reset) before an exchange fails; it costs nothing when the port is there.
PORT_WAIT_MS = 10_000
_CURSOR_RIGHT = re.compile(r"\x1b\[(\d+)C")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# A part's own register or field text saying a read clears it ("cleared by reading ALERT_DIAG",
# "does not reset until it is read", "reading 07h clears them").
READ_CLEARS_TEXT = re.compile(
    r"\b(?:clear\w*|reset\w*)\s+(?:\w+\s+){0,3}?(?:by|upon|on|after|when)\s+(?:a\s+|any\s+)?read"
    r"|\buntil\s+(?:it\s+is\s+)?read\b|\breading\s+(?:\S+\s+){0,3}?clears\b", re.I)

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


def _enum_key(k) -> int | None:
    """An enum key as a number: "1", "0x1", "1h" (hex), "01b" (binary)."""
    s = str(k).strip()
    try:
        if re.fullmatch(r"[0-9A-Fa-f]+h", s):
            return int(s[:-1], 16)
        if re.fullmatch(r"[01]+b", s):
            return int(s[:-1], 2)
        return int(s, 0)
    except ValueError:
        return None


def _enum(f: dict, v: int) -> str:
    """The enum name for `v`, whichever way the record keys it."""
    e = f.get("enum") or {}
    if not isinstance(e, dict):
        return ""
    text = next((val for k, val in e.items() if _enum_key(k) == v), None)
    return re.sub(r"^B_0x[0-9A-Fa-f]+:\s*", "", str(text)) if text else ""


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
    head = target.partition(".")[0]
    svd = Svd(svds[0]) if svds else None
    if svd is None or _norm(head) not in svd.periphs():
        return ext_reg_read(atlas_dir, target, allow_clear, role)
    try:
        periph, regs = svd.resolve(target)
    except LookupError as e:
        return f"src: none\n\n{e}.\n"
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


# --------------------------------------------------------------------------- the live shell

def consoles(s: dict[str, Any], role: str) -> list[str]:
    """The role's console signals: `[live] console` when set, else every direct signal of the role
    that Core may write (bidirectional or host-to-dut), by name."""
    out = call_api(s, ["list-signals"], STATUS_TIMEOUT_S).get("signals") or []
    return sorted(x["name"] for x in out if x.get("origin_role") == role
                  and x.get("direction") in ("bidirectional", "host-to-dut")
                  and (x.get("route") or {}).get("kind") == "direct")


def prompt_of(atlas_dir: Path) -> str:
    """The shell prompt the pinned build was configured with, else Zephyr's default."""
    from .query import current_atlas
    try:
        atlas = current_atlas(Path(atlas_dir), None)
        sym = json.loads((atlas / "kconfig.json").read_text(encoding="utf-8"))["symbols"]["SHELL_PROMPT_UART"]
        return sym.get("v") or DEFAULT_PROMPT
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_PROMPT


def clean(text: str, command: str, prompt: str) -> str:
    """The command's output alone: escape codes out (a cursor move kept as spaces), the echo and
    everything before it dropped, the closing prompt dropped."""
    t = _ANSI.sub("", _CURSOR_RIGHT.sub(lambda m: " " * int(m[1]), text)).replace("\r", "")
    echo = t.find(command + "\n")
    if echo >= 0:
        t = t[echo + len(command) + 1:]
    p = prompt.rstrip()
    t = t.rstrip()
    if p and t.endswith(p):
        t = t[: -len(p)]
    return t.rstrip("\n") + "\n"


class Console:
    """One role's shell, through Core: each `run` is one exchange (the port opened and closed)."""

    def __init__(self, atlas_dir: Path, role: str | None = None):
        from .project import load_config
        self.s = settings(atlas_dir)
        self.role = role or self.s["role"]
        try:
            pinned = ((load_config(atlas_dir).get("live") or {}).get("console"))
        except FileNotFoundError:
            pinned = None
        self.names = [pinned] if pinned else consoles(self.s, self.role)
        if not self.names:
            raise LiveError(f"no console signal is declared for the role '{self.role}' (declare_signal, "
                            "direction bidirectional, direct route), or set [live] console")
        self.prompt = prompt_of(atlas_dir)
        self.used: dict[str, Any] = {}

    def run(self, command: str, timeout_ms: int = EXCHANGE_TIMEOUT_MS) -> str:
        last = None
        for name in self.names:
            try:
                ex = call_api(self.s, ["signal-exchange", "--name", name, "--write", command, "--until",
                                       self.prompt, "--timeout-ms", str(timeout_ms),
                                       "--port-wait-ms", str(PORT_WAIT_MS)], READ_TIMEOUT_S)["exchange"]
            except LiveError as e:   # declared but not enumerable now: try the next console
                last = e
                continue
            self.used = {"signal": ex.get("signal"), "port": ex.get("port"), "prompt_seen": ex.get("until_seen")}
            return clean(ex.get("text") or "", command, self.prompt)
        raise last or LiveError("no console answered")

    def src(self) -> str:
        u = self.used
        return f"live {self.role} shell via embarch-core (signal {u.get('signal')}, {u.get('port')})"


def shell(atlas_dir: Path, command: str, role: str | None = None, timeout_ms: int | None = None) -> str:
    """One shell command on the role's console, its output with escape codes stripped."""
    try:
        con = Console(atlas_dir, role)
        out = con.run(command, min(int(timeout_ms or EXCHANGE_TIMEOUT_MS), 10_000))
    except LiveError as e:
        return f"src: none\n\nThe command did not run: {e}\n"
    tail = "" if con.used.get("prompt_seen") else (f"\n(no prompt within the timeout: the output may be cut; "
                                                   "pass a longer timeout_ms)\n")
    return f"src: {con.src()}\n\n{out}{tail}"


# --------------------------------------------------------------------------- external registers

def i2c_instances(atlas_dir: Path) -> list[dict]:
    """Every I2C device the atlas's join tied to a part: node, labels, bus device, address, docs."""
    from .query import current_atlas
    atlas = current_atlas(Path(atlas_dir), None)
    if atlas is None:
        return []
    try:
        pins = json.loads((atlas / "pins.json").read_text(encoding="utf-8"))
        nodes = json.loads((atlas / "dt.json").read_text(encoding="utf-8")).get("nodes") or {}
    except (OSError, ValueError):
        return []
    part_docs = pins.get("part_docs") or {}
    out = []
    for bus in (pins.get("i2c") or {}).get("buses") or []:
        for fam in bus.get("families") or []:
            for dt in fam.get("dt") or []:
                parts = [p for p in fam.get("parts") or [] if dt.get("reg") in (p.get("addrs") or [])]
                docs = sorted({d for p in parts for d in part_docs.get(p.get("part"), [])})
                node = dt.get("node") or ""
                labels = (nodes.get(node) or {}).get("labels") or [] if isinstance(nodes, dict) else []
                out.append({"node": node, "name": node.rsplit("/", 1)[-1], "labels": labels,
                            "bus": bus.get("bus"), "bus_dev": str(bus.get("bus") or "").rsplit("/", 1)[-1],
                            "addr": dt.get("reg"), "compat": fam.get("compat"),
                            "refs": [p.get("part") for p in parts], "docs": docs})
    return out


def _instance(atlas_dir: Path, head: str) -> tuple[dict | None, str]:
    """The I2C device `head` names (a DT label, a node name, or a part when it has one instance)."""
    from .query import Atlas
    insts = i2c_instances(atlas_dir)
    h = head.lower()
    exact = [i for i in insts if h in [x.lower() for x in i["labels"]] or h in (i["name"].lower(), i["node"].lower(),
                                                                             i["name"].split("@")[0].lower())]
    if len(exact) == 1:
        return exact[0], ""
    doc = Atlas(Path(atlas_dir)).find_doc(head)
    mine = [i for i in insts if doc is not None and doc.name in i["docs"]]
    if len(mine) == 1:
        return mine[0], ""
    pick = exact or mine
    if pick:
        rows = ", ".join(f"{(i['labels'] or [i['name']])[0]} ({i['bus_dev']} 0x{i['addr']:02X})" for i in pick)
        return None, f"{head!r} is {len(pick)} devices on the board; name one by its DT label: {rows}"
    if doc is not None:
        return None, f"no I2C device of {doc.name} in this build's devicetree"
    return None, f"no MCU peripheral, DT label or part named {head!r}"


def _ext_regs(doc: Path, reg: str | None) -> list[list[dict]]:
    """Registers of the part to read, each as its records: one, or an `_LSB` + `_MSB` pair."""
    regs = [r for r in _jsonl(doc / "records" / "register.jsonl") if r.get("kind") == "register"]
    if reg is None:
        pairs: dict[str, dict] = {}
        out = []
        for r in sorted(regs, key=lambda r: _addr(r) or 0):
            m = re.match(r"(.+)_(LSB|MSB)$", r.get("name") or "", re.I)
            if m:
                pairs.setdefault(m[1], {})[m[2].upper()] = r
                if len(pairs[m[1]]) == 2:
                    out.append([pairs[m[1]]["LSB"], pairs[m[1]]["MSB"]])
            else:
                out.append([r])
        return out
    rn = _norm(reg)
    one = [r for r in regs if _norm(r.get("name")) == rn]
    if one:
        return [one[:1]]
    lsb = [r for r in regs if _norm(r.get("name")) == rn + "lsb"]
    msb = [r for r in regs if _norm(r.get("name")) == rn + "msb"]
    if lsb and msb:
        return [[lsb[0], msb[0]]]
    names = ", ".join(sorted({re.sub(r"_(LSB|MSB)$", "", r.get("name") or "") for r in regs})[:60])
    raise LookupError(f"no register {reg!r} in {doc.name}; it has: {names}")


def _bus(doc: Path) -> dict:
    for r in _jsonl(doc / "records" / "bus.jsonl"):
        if r.get("kind") == "bus" and str(r.get("iface") or "").lower() in ("i2c", "smbus"):
            return r
    return {}


def _route(bus: dict, inst: dict, reg_addr: int) -> tuple[int, int]:
    """(bus address, register byte) for a register address, through the bus record's windows."""
    for w in bus.get("windows") or []:
        lo, hi = _int(w.get("from")), _int(w.get("to"))
        if lo is not None and hi is not None and lo <= reg_addr <= hi:
            return _int(w.get("addr")), reg_addr - (_int(w.get("base")) if w.get("base") else lo)
    return inst["addr"], reg_addr


def _ext_effects(doc: Path, recs: list[dict]) -> list[str]:
    ids = {r.get("id") for r in recs}
    why = []
    by_cite: dict[str, list[str]] = {}
    for f in _jsonl(doc / "records" / "field.jsonl"):
        if f.get("reg") not in ids:
            continue
        text = " ".join(str(f.get(k) or "") for k in ("desc", "q"))
        if "clr" in str(f.get("access") or "") or READ_CLEARS_TEXT.search(text):
            by_cite.setdefault(f"{f.get('doc')} p{f.get('p')}", []).append(str(f.get("name")))
    why += [f"{', '.join(names)} {'clears' if len(names) == 1 else 'clear'} on the read ({cite})"
            for cite, names in by_cite.items()]
    for r in recs:
        if READ_CLEARS_TEXT.search(" ".join(str(r.get(k) or "") for k in ("desc", "q", "summary"))):
            why.append(f"the register's text says a read clears it ({r.get('doc')} p{r.get('p')})")
    for r in _jsonl(doc / "records" / "rule.jsonl") + _jsonl(doc / "records" / "quirk.jsonl"):
        named = ids & set(r.get("refs") or []) or r.get("reg") in ids
        if named and READ_CLEARS_TEXT.search(" ".join(str(r.get(k) or "") for k in ("text", "q"))):
            why.append(f"{r.get('id')} ({r.get('doc')} p{r.get('p')})")
    return list(dict.fromkeys(why))


_HEX_LINE = re.compile(r"^[0-9A-Fa-f]{8}:\s+((?:[0-9a-f]{2}\s+)+)", re.M)


def _parse_read(out: str, n: int) -> list[int]:
    if n == 1:
        m = re.search(r"Output:\s*0x([0-9a-fA-F]+)", out)
        if m:
            return [int(m[1], 16)]
    data = [int(b, 16) for line in _HEX_LINE.findall(out) for b in line.split()]
    if len(data) >= n:
        return data[:n]
    raise LiveError(out.strip().splitlines()[-1] if out.strip() else "no answer from the shell")


def _device_state(con: Console, inst: dict) -> str:
    try:
        out = con.run("device list")
    except LiveError:
        return ""
    m = re.search(r"^- " + re.escape(inst["name"]) + r" \(([^)]*)\)", out, re.M)
    return m[1] if m else ""


def _ext_decode(recs: list[dict], v: int, fields_of: dict[str, list[dict]], label: str,
                all_fields: bool) -> list[str]:
    """One register or an LSB/MSB pair as one value, decoded like an MCU register."""
    if len(recs) == 1:
        r = dict(recs[0])
        fields = fields_of.get(r.get("id"), [])
    else:
        lsb, msb = recs
        name = re.sub(r"_(LSB|MSB)$", "", lsb.get("name") or "", flags=re.I)
        rl, rm_ = _int(lsb.get("reset")), _int(msb.get("reset"))
        r = {"name": name, "addr": f"{lsb.get('addr')}+{msb.get('addr')}", "width": 16,
             "reset": f"0x{(rm_ << 8) | rl:04X}" if rl is not None and rm_ is not None else None}
        fields = fields_of.get(lsb.get("id"), []) + [dict(f, bits=[b + 8 for b in f.get("bits") or []])
                                                       for f in fields_of.get(msb.get("id"), [])]
        fields.sort(key=lambda f: -((f.get("bits") or [0])[0]))
    r["id"] = f"x.reg.{label}.{r.get('name')}"
    return decode(r, v, fields, all_fields)


def ext_reg_read(atlas_dir: Path, target: str, allow_clear: bool = False, role: str | None = None) -> str:
    """`<label or part>.<REG>`, or a whole small part, read with `i2c read` on the board's shell."""
    from .query import Atlas
    head, _, reg = target.partition(".")
    inst, why = _instance(atlas_dir, head)
    if inst is None:
        return f"src: none\n\n{why}.\n"
    doc = next((Path(atlas_dir) / "docs" / d for d in inst["docs"] if (Path(atlas_dir) / "docs" / d / "records").is_dir()), None)
    if doc is None:
        return f"src: none\n\nNo document with register records for {inst['node']} ({', '.join(inst['refs'])}).\n"
    try:
        groups = _ext_regs(doc, reg or None)
    except LookupError as e:
        return f"src: none\n\n{e}.\n"
    if len(groups) > MAX_EXCHANGES:
        return (f"src: none\n\n{doc.name} has {len(groups)} registers; a whole-part read takes at most "
                f"{MAX_EXCHANGES} (one shell command each). Ask for registers by name.\n")
    bus = _bus(doc)
    if any(int(r.get("width") or 8) > 8 for g in groups for r in g) and bus.get("byte_order") not in ("lsb-first", "msb-first"):
        return (f"src: none\n\n{doc.name} has multi-byte registers but no `bus` record that states their byte order "
                "on the bus. Add one (datasheet procedure, step 5) before reading them.\n")
    effects = {id(g): _ext_effects(doc, g) for g in groups}
    single = bool(reg)
    if single and effects[id(groups[0])] and not allow_clear:
        return ("src: none\n\nRefused: reading this register changes the part.\n- "
                + "; ".join(effects[id(groups[0])]) + "\nCall again with allow_clear=true if clearing it is the point.\n")
    label = (inst["labels"] or [inst["name"]])[0]
    fields_of: dict[str, list[dict]] = {}
    for f in _jsonl(doc / "records" / "field.jsonl"):
        fields_of.setdefault(f.get("reg"), []).append(f)
    try:
        con = Console(atlas_dir, role)
    except LiveError as e:
        return f"src: none\n\nThe read failed: {e}\n"
    rab = int(bus.get("reg_addr_bytes") or 1)
    out, skipped = [], []
    for g in groups:
        if effects[id(g)] and not allow_clear:
            skipped.append((g, effects[id(g)]))
            continue
        vals = []
        try:
            for r in g:
                a = _int(r.get("addr"))
                dev, byte = _route(bus, inst, a)
                n = max(1, int(r.get("width") or 8) // 8)
                cmd = (f"i2c read_byte {inst['bus_dev']} 0x{dev:02X} 0x{byte:0{2 * rab}X}" if n == 1 else
                       f"i2c read {inst['bus_dev']} 0x{dev:02X} 0x{byte:0{2 * rab}X} {n:x}")
                data = _parse_read(con.run(cmd), n)
                order = data if bus.get("byte_order") == "lsb-first" else list(reversed(data))
                vals.append(sum(b << (8 * i) for i, b in enumerate(order)))
        except LiveError as e:
            state = _device_state(con, inst)
            hint = f"; the board lists {inst['name']} as ({state})" if state else ""
            return (f"src: {con.src()}\n\nThe read of {label}.{g[0].get('name')} failed: {e}{hint}. "
                    f"Nothing answered at 0x{dev:02X} on {inst['bus_dev']}.\n")
        v = vals[0] if len(vals) == 1 else (vals[1] << 8) | vals[0]
        out += _ext_decode(g, v, fields_of, label, all_fields=single)
        if effects[id(g)]:
            out.append("  ! this read cleared: " + "; ".join(effects[id(g)]))
    head_line = (f"src: {con.src()}; {label} = {inst['node']} at 0x{inst['addr']:02X} on {inst['bus_dev']}"
                 f" ({', '.join(inst['refs'])}); decoded with {doc.name}")
    lines = [head_line, ""] + out
    if skipped:
        lines += ["", "Not read (reading changes the part; allow_clear=true reads them):"]
        lines += [f"- {re.sub(r'_(LSB|MSB)$', '', g[0].get('name') or '')}: " + "; ".join(w) for g, w in skipped]
    if not single:
        lines += ["", "`*` marks a field not at its reset value; ask for <label>.<REG> for every field."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- live stacks

THREAD_ROW = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s+(.*?)\s*\(real size\s+(\d+)\):\s*unused\s+(\d+)\s+usage\s+(\d+)"
                        r"\s*/\s*(\d+)\s*\(\s*(\d+)\s*%\)", re.M)
# Words that name the same thing in a thread's name and its stack symbol.
_ALIAS = {"irq": "interrupt", "isr": "interrupt", "sysworkq": "work", "workq": "work"}
PROBE_WORDS = 16
HOT_PCT = 90


def parse_thread_stacks(out: str) -> list[dict]:
    """`kernel thread stacks` rows: the thread's address, name, size, unused, used, percent."""
    return [{"addr": int(m[1], 16), "name": m[2].strip(), "size": int(m[3]), "unused": int(m[4]),
             "used": int(m[5]), "pct": int(m[7])} for m in THREAD_ROW.finditer(out)]


def elf_segments(elf: Path) -> list[tuple[int, int, int]]:
    """(load address, file offset, file size) of every PT_LOAD segment with bytes: what flashing
    writes. ELF32 little-endian, the Cortex-M case."""
    import struct
    data = elf.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError(f"{elf} is not a 32-bit little-endian ELF")
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    out = []
    for i in range(phnum):
        ptype, off, _vaddr, paddr, filesz = struct.unpack_from("<IIIII", data, phoff + i * phentsize)
        if ptype == 1 and filesz:
            out.append((paddr, off, filesz))
    return sorted(out)


def probe_ranges(segs: list[tuple[int, int, int]], words: int = PROBE_WORDS) -> list[tuple[int, int]]:
    """Three places of the image to compare with the board's flash: the start (the vector table),
    the middle and the end of the largest segment."""
    paddr, _off, size = max(segs, key=lambda s: s[2])
    n = min(words, size // 4)
    starts = {paddr, (paddr + size // 2) & ~3, (paddr + size - 4 * n) & ~3}
    return [(a, n) for a in sorted(starts)]


def image_check(s: dict[str, Any], role: str, elf: Path) -> tuple[bool, str]:
    """Whether the board's flash holds this ELF's image, at the probe ranges (one halted read)."""
    data = elf.read_bytes()
    segs = elf_segments(elf)
    ranges = probe_ranges(segs)
    read = core_read(s, ranges, role)
    board = word_map(read)
    differ = 0
    for addr, n in ranges:
        for i in range(n):
            a = addr + 4 * i
            seg = next((x for x in segs if x[0] <= a < x[0] + x[2]), None)
            if seg is None:
                continue
            want = int.from_bytes(data[seg[1] + a - seg[0]: seg[1] + a - seg[0] + 4], "little")
            if board.get(a) != want:
                differ += 1
                break
    total = len(ranges)
    return differ == 0, (f"flash matches the ELF at {total} places" if differ == 0
                         else f"flash differs from the ELF at {differ} of {total} places")


def _thread_words(name: str) -> set[str]:
    from .footprint import _words
    w = _words(name)
    return w | {_ALIAS[x] for x in w if x in _ALIAS}


def join_stacks(live: list[dict], img: dict, rows: list[dict]) -> list[dict]:
    """Each live thread with the stack symbol it most likely runs on: the thread object at its
    address names it (K_THREAD_DEFINE pairs `_k_thread_obj_X` with `_k_thread_stack_X`), else a
    stack of the same size sharing a name word with the thread or its object. No pairing by size
    alone: a free stack of the right size is as often the main thread's, which has returned."""
    syms = sorted((s for s in img["symbols"] if s.get("s")), key=lambda s: s["a"])

    def holder(a: int) -> dict | None:
        return next((s for s in syms if s["a"] <= a < s["a"] + s["s"]), None)

    taken: set[str] = set()
    out = []
    for th in sorted(live, key=lambda x: -x["pct"]):
        obj = holder(th["addr"])
        obj_n = obj["n"] if obj else ""
        fits = [r for r in rows if r["n"] not in taken and th["size"] <= r["s"] <= th["size"] + 64]
        pick, how = None, ""
        m = re.match(r"_k_thread_obj_(.+)", obj_n)
        if m:
            pick = next((r for r in fits if r["n"] == f"_k_thread_stack_{m[1]}"), None)
            how = "K_THREAD_DEFINE" if pick else ""
        if pick is None:
            words = _thread_words(th["name"]) | _thread_words(obj_n)
            scored = sorted(((len(words & _thread_words(r["n"])), r["n"], r) for r in fits), key=lambda x: (-x[0], x[1]))
            if scored and scored[0][0] and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                pick, how = scored[0][2], "same size, name word"
        out.append({**th, "obj": obj_n, "stack": pick, "how": how})
        if pick:
            taken.add(pick["n"])
    return out


def live_stacks(atlas_dir: Path, build: str | None = None, role: str | None = None) -> str:
    """Every thread's stack peak on the board (`kernel thread stacks`), joined to the stack symbol,
    its defining line and the *_STACK_SIZE option, once the board is shown to run that build."""
    from . import buildq, footprint as F
    from .query import Atlas
    try:
        con = Console(atlas_dir, role)
        out = con.run("kernel thread stacks", 5000)
    except LiveError as e:
        return f"src: none\n\nThe command did not run: {e}\n"
    live = parse_thread_stacks(out)
    if not live:
        return (f"src: {con.src()}\n\nNo thread rows in the answer (CONFIG_THREAD_STACK_INFO and the kernel shell "
                f"are needed):\n{out}")
    join_note, rows, img = "", [], None
    try:
        src = buildq.source(Atlas(Path(atlas_dir)), build)
        img = buildq._image(src)
        same, why = image_check(con.s, con.role, src.elf)
        if same:
            src.load_kconfig()
            rows = F.stack_rows(img, F.dotconfig_of(src.dotconfig), (src.kconfig or {}).get("symbols"))
            join_note = f" + {src.label} zephyr.elf ({why})"
        else:
            join_note = (f"; not joined to {src.label}: {why}, so the board runs another build. Pass build=<dir> "
                         "of the image it runs")
    except (LookupError, FileNotFoundError, OSError, ValueError, RuntimeError, LiveError,
            subprocess.CalledProcessError) as e:
        join_note = f"; not joined to an ELF: {e}"
    joined = join_stacks(live, img, rows) if rows else [dict(th, obj="", stack=None, how="") for th in
                                                         sorted(live, key=lambda x: -x["pct"])]
    lines = [f"src: {con.src()}{join_note}", "",
             f"{'thread':<28} {'size':>5} {'peak':>5} {'%':>4}  stack symbol, defined at, option (inference)"]
    for th in joined:
        name = th["name"] or (th["obj"] or f"0x{th['addr']:08x}")
        st = th["stack"]
        tail = ""
        if st:
            where = f"{st['f']}:{st['l']}" if st.get("f") else "(no debug file)"
            opt = f"  ~ CONFIG_{', CONFIG_'.join(st['opts'])}" if st.get("opts") else ""
            tail = f"{st['n']} {where}{opt}" + (f" [{th['how']}]" if th["how"] else "")
        elif th.get("obj"):
            tail = f"(no stack symbol paired; thread object in {th['obj']})"
        mark = "!" if th["pct"] >= HOT_PCT else " "
        lines.append(f"{mark}{name:<27} {th['size']:>5} {th['used']:>5} {th['pct']:>3}%  {tail}".rstrip())
    hot = [th for th in joined if th["pct"] >= HOT_PCT]
    if hot:
        lines += ["", f"`!` at or above {HOT_PCT} %: " + ", ".join(
            f"{th['name'] or th['obj']} ({th['unused'] if 'unused' in th else th['size'] - th['used']} B left)" for th in hot)
            + ". A peak is the most used since boot; a path not yet run can go higher."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- cli

def _cmd_reg_read(args) -> int:
    from . import common
    print(reg_read(common.atlas_dir(args.atlas_dir), args.target, args.allow_clear, args.role), end="")
    return 0


def _cmd_shell(args) -> int:
    from . import common
    print(shell(common.atlas_dir(args.atlas_dir), " ".join(args.command), args.role, args.timeout_ms), end="")
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
    p = g.add_parser("stacks", help="every thread's stack peak on the board, joined to its stack symbol and option")
    p.add_argument("--build", help="the build dir the board runs (default: the atlas's pinned build)")
    p.add_argument("--role", help="the enrolled role (default [live] role, else dut)")
    p.set_defaults(func=lambda a: print(live_stacks(common.atlas_dir(a.atlas_dir), a.build, a.role), end="") or 0)
    p = g.add_parser("shell", help="one command on the board's shell, through embarch-core")
    p.add_argument("command", nargs="+")
    p.add_argument("--role", help="the enrolled role (default [live] role, else dut)")
    p.add_argument("--timeout-ms", type=int)
    p.set_defaults(func=_cmd_shell)
