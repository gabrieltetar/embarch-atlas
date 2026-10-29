"""`live core-state`: where is the core, as symbols.

Core's `POST /live/core-state` (embarch-core decision 97) returns raw words: `DHCSR`, `DWT_PCSR`
samples, and with `halt` the registers and `SCB_ICSR`. This module asks for them through
`embarch-api core-state`, names the bits of `DHCSR` and `ICSR`, and resolves each PC sample, `PC`
and `LR` to a function and line in the ELF the board runs, the one `live_stacks` checks against
flash. When the flash is not that ELF, the addresses stay raw and the answer says so.

The ELF is read with the toolchain the build names (`nm` through `footprint.image`, `addr2line`
beside it): no new dependency. Without `addr2line` a function is still named, from the symbol
table, with the line its definition starts on.
"""
from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from . import live
from .live import LiveError

SAMPLES_DEFAULT = 8

# DHCSR, ARMv6-M/v7-M/v8-M: the status half (read) and the control half.
DHCSR_BITS = [(25, "S_RESET_ST"), (24, "S_RETIRE_ST"), (19, "S_LOCKUP"), (18, "S_SLEEP"), (17, "S_HALT"),
              (16, "S_REGRDY"), (5, "C_SNAPSTALL"), (3, "C_MASKINTS"), (2, "C_STEP"), (1, "C_HALT"),
              (0, "C_DEBUGEN")]
# SCB_ICSR flags; VECTACTIVE [8:0] and VECTPENDING [20:12] are fields.
ICSR_BITS = [(31, "NMIPENDSET"), (28, "PENDSVSET"), (27, "PENDSVCLR"), (26, "PENDSTSET"), (25, "PENDSTCLR"),
             (23, "ISRPREEMPT"), (22, "ISRPENDING"), (11, "RETTOBASE")]
EXCEPTIONS = {0: "thread mode", 1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
              6: "UsageFault", 7: "SecureFault", 11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}
EXC_RETURN = {0xFFFFFFF1: "to handler mode, MSP", 0xFFFFFFF9: "to thread mode, MSP",
              0xFFFFFFFD: "to thread mode, PSP"}


def exception_name(n: int) -> str:
    return EXCEPTIONS.get(n) or (f"IRQ {n - 16} (exception {n})" if n >= 16 else f"reserved exception {n}")


def decode_dhcsr(raw: int) -> str:
    """The `DHCSR` bits set, by name, and the state they mean (lockup beats halted beats sleeping)."""
    set_ = [n for b, n in DHCSR_BITS if raw >> b & 1]
    state = ("lockup" if raw >> 19 & 1 else "halted" if raw >> 17 & 1 else "sleeping" if raw >> 18 & 1
             else "running")
    named = sum(1 << b for b, _ in DHCSR_BITS)
    other = raw & ~named
    tail = f"; unnamed bits 0x{other:08X}" if other else ""
    return f"DHCSR 0x{raw:08X}: {state}; set: {', '.join(set_) or 'none'}{tail}"


def decode_icsr(raw: int) -> str:
    """`SCB_ICSR` by field name, with the exception number named."""
    act, pend = raw & 0x1FF, raw >> 12 & 0x1FF
    flags = [n for b, n in ICSR_BITS if raw >> b & 1]
    named = sum(1 << b for b, _ in ICSR_BITS) | 0x1FF | 0x1FF << 12
    other = raw & ~named
    tail = f"; unnamed bits 0x{other:08X}" if other else ""
    return (f"SCB_ICSR 0x{raw:08X}: VECTACTIVE {act} ({exception_name(act)}); VECTPENDING {pend}"
            + (f" ({exception_name(pend)})" if pend else "") + f"; set: {', '.join(flags) or 'none'}{tail}")


# --------------------------------------------------------------------------- resolving

def _addr2line_of(img_tools: dict[str, str]) -> str | None:
    """The `addr2line` beside the build's `nm`, else the Zephyr SDK's on PATH."""
    nm = img_tools.get("CMAKE_NM")
    if nm:
        cand = Path(nm).with_name(Path(nm).name.replace("nm", "addr2line", 1))
        if cand.is_file():
            return str(cand)
    return shutil.which("arm-zephyr-eabi-addr2line")


def parse_addr2line(text: str, n: int) -> list[tuple[str, str] | None]:
    """`addr2line -a -f` output: per address a line `0x...`, the function, `file:line`. (function, where)
    per address, None where it knew nothing (`??`)."""
    lines = [l for l in text.splitlines() if l.strip()]
    out: list[tuple[str, str] | None] = []
    i = 0
    while i + 2 < len(lines) and len(out) < n:
        if not lines[i].startswith("0x"):
            i += 1
            continue
        fn, where = lines[i + 1], lines[i + 2].split(" (discriminator")[0]
        out.append(None if fn == "??" else (fn, "" if where.startswith("??") else where))
        i += 3
    out += [None] * (n - len(out))
    return out


def resolve(addrs: list[int], img: dict, elf: Path, tools: dict[str, str], rel=lambda p: p,
            run=subprocess.run) -> dict[int, dict]:
    """address -> {fn, where, exact}. The function from addr2line when the toolchain has it, else the
    symbol containing the address; `where` is `file:line` of the address (addr2line) or of the
    symbol's definition (`exact` False)."""
    syms = sorted((s for s in img["symbols"] if s.get("t", "").upper() in ("T", "W") and s.get("s")),
                  key=lambda s: s["a"])

    def holder(a: int) -> dict | None:
        return next((s for s in syms if (s["a"] & ~1) <= a < (s["a"] & ~1) + s["s"]), None)

    out: dict[int, dict] = {}
    a2l = _addr2line_of(tools)
    got: dict[int, tuple[str, str] | None] = {}
    if a2l and addrs:
        try:
            p = run([a2l, "-a", "-f", "-C", "-e", str(elf), *[f"0x{a:08x}" for a in addrs]],
                    capture_output=True, text=True, timeout=30)
            got = dict(zip(addrs, parse_addr2line(p.stdout, len(addrs))))
        except (OSError, subprocess.SubprocessError):
            got = {}
    for a in addrs:
        h = holder(a)
        g = got.get(a)
        if g:
            where = rel(g[1].rsplit(":", 1)[0]) + ":" + g[1].rsplit(":", 1)[1] if ":" in g[1] else g[1]
            out[a] = {"fn": g[0], "where": where, "exact": bool(where), "off": a - (h["a"] & ~1) if h else None}
        elif h:
            where = f"{h['f']}:{h['l']}" if h.get("f") else ""
            out[a] = {"fn": h["n"], "where": where, "exact": False, "off": a - (h["a"] & ~1)}
        else:
            out[a] = {"fn": None, "where": "", "exact": False, "off": None}
    return out


def _sym(r: dict) -> str:
    if not r["fn"]:
        return "(no symbol: not in the ELF's code)"
    off = f"+0x{r['off']:x}" if r.get("off") else ""
    where = f" {r['where']}" if r["where"] else ""
    return f"{r['fn']}{off}{where}" + ("" if r["exact"] else (" (definition line)" if r["where"] else ""))


# --------------------------------------------------------------------------- the answer

def render(state: dict, r: dict[int, dict] | None, refusal: str) -> str:
    """The answer text; `r` is None when the addresses stay raw."""
    out = [f"role {state.get('role')}, chip {state.get('chip')}, probe {state.get('probe_serial')}: "
           f"found {state.get('found')}, left {state.get('left')}, {state.get('halt_count')} halt(s)"
           f"{' (' + str(state.get('halted_us')) + ' us this request)' if state.get('halted_us') else ''}"]
    dh = (state.get("dhcsr") or {}).get("raw")
    if dh is not None:
        out.append(decode_dhcsr(dh))
    da = (state.get("dhcsr_after") or {}).get("raw")
    if da is not None:
        out.append("after the samples: " + decode_dhcsr(da)
                   + ("; instructions retired in between" if da >> 24 & 1 else "; none retired: the core did not "
                                                                                 "execute between the reads"))
    ic = (state.get("scb_icsr") or {}).get("raw")
    if ic is not None:
        out.append(decode_icsr(ic))
    out.append("")
    if refusal:
        out += [refusal, "Addresses stay raw below.", ""]
    regs = state.get("registers")
    if regs:
        for name in ("pc", "lr"):
            v = regs[name]
            if name == "lr" and v >= 0xFFFFFFF0:
                out.append(f"LR  0x{v:08X}  EXC_RETURN {EXC_RETURN.get(v, '(other)')}: the core is in or just left an "
                           "exception, the caller is on the stack")
            elif r is not None:
                a = v & ~1 if name == "pc" else (v & ~1) - 2
                out.append(f"{name.upper():<3} 0x{v:08X}  {_sym(r[a])}" + ("" if name == "pc" else "  (the call)"))
            else:
                out.append(f"{name.upper():<3} 0x{v:08X}")
        out.append(f"SP  0x{regs['sp']:08X}  xPSR 0x{regs['xpsr']:08X}  CONTROL 0x{regs['control']:X}  "
                   f"PRIMASK {regs['primask']}")
        out.append("")
    samples = state.get("pc_samples") or []
    if samples:
        got = [s for s in samples if s is not None]
        out.append(f"PC samples ({len(got)} of {len(samples)} gave an address):")
        if r is not None:
            hist = Counter(r[s & ~1]["fn"] or f"0x{s:08X} (no symbol)" for s in got)
            for fn, n in hist.most_common():
                out.append(f"  {n:>3} x {n * 100 // max(len(got), 1):>3}%  {fn}")
            out.append("")
            for s in sorted(set(got), key=lambda x: (-got.count(x), x)):
                out.append(f"  0x{s:08X} x{got.count(s)}  {_sym(r[s & ~1])}")
        else:
            for s in sorted(set(got), key=lambda x: (-got.count(x), x)):
                out.append(f"  0x{s:08X} x{got.count(s)}")
        if len(got) < len(samples):
            out.append(f"  {len(samples) - len(got)} sample(s) empty: the core was asleep")
    elif state.get("pc_note"):
        out.append(str(state["pc_note"]))
    return "\n".join(out).rstrip() + "\n"


def core_state(atlas_dir: Path, build: str | None = None, role: str | None = None, halt: bool = False,
               samples: int = SAMPLES_DEFAULT) -> str:
    """Where the core is, from Core's `core-state`, as symbols. `halt` stops the core once for the
    registers and `SCB_ICSR`; the default never halts for the read itself. Resolving needs the ELF
    the board runs, and the check that it does is one halted `mem-read` (decision 81), after the
    state was taken."""
    from . import buildq
    from .query import Atlas
    s = live.settings(atlas_dir)
    role = role or s["role"]
    args = ["core-state", "--role", role, "--samples", str(samples)] + (["--halt"] if halt else [])
    try:
        state = live.call_api(s, args, live.READ_TIMEOUT_S)["state"]
    except LiveError as e:
        return f"src: none\n\nThe read did not run: {e}\n"
    except KeyError:
        return "src: none\n\nCore's answer had no `state`: embarch-api is older than core-state.\n"
    bad = live.chip_refusal(atlas_dir, state, role)
    if bad:
        return f"src: none\n\n{bad}\n"
    addrs = {v & ~1 for v in state.get("pc_samples") or [] if v is not None}
    regs = state.get("registers")
    if regs:
        addrs.add(regs["pc"] & ~1)
        if regs["lr"] < 0xFFFFFFF0:
            addrs.add((regs["lr"] & ~1) - 2)
    refusal, r, label = "", None, ""
    if addrs:
        try:
            src = buildq.source(Atlas(Path(atlas_dir)), build)
            img = buildq._image(src)
            same, why = live.image_check(s, role, src.elf, atlas_dir)
            if same:
                r = resolve(sorted(addrs), img, src.elf, src.tools, src.rel)
                label = f"{src.label} zephyr.elf ({why})"
            else:
                refusal = (f"Refused to name symbols: {why}, so the board runs a build other than {src.label}. "
                           "Pass build=<dir> of the image it runs.")
        except live.ChipMismatch as e:
            refusal = (f"Refused to name symbols: the probe of role {role} reads another chip than the atlas's ({e}).")
        except (LookupError, FileNotFoundError, OSError, ValueError, RuntimeError, LiveError,
                subprocess.CalledProcessError) as e:
            refusal = f"Refused to name symbols: no ELF to check the flash against ({e})."
    head = f"src: embarch-core core-state{' + ' + label if label else ''}\n\n"
    return head + render(state, r, refusal)
