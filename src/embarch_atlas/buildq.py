"""`config` and `dt_node`: the build's Kconfig symbols and devicetree nodes, with why.

Both answer from the atlas's pinned build by default (`kconfig.json`, `dt.json`), or from a build
dir the caller names (`build=`), read and never written. A named build's extraction runs on the
first query and is cached under `cache/`, keyed by the content it came from, and only while the
workspace's Zephyr is the one the build used (`kconfig.workspace_mismatch`); otherwise `config`
falls back to the value and the fragment assignments, and `dt_node` says why it cannot answer.
The `src:` line names the build: the atlas, or the dir with the app and Zephyr versions it
recorded and the fw repo's HEAD and dirty state now.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from . import kconfig as KC
from .common import read_json, write_json

MAX_LIST = 60


# ---------------------------------------------------------------- where the answer comes from

def _git(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _modules(zdir: Path) -> list[tuple[str, str]]:
    """(path, name) per Zephyr module, from zephyr_modules.txt ("name":"path":...)."""
    out = []
    p = zdir / "zephyr_modules.txt"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = re.findall(r'"([^"]*)"', line)
            if len(parts) >= 2:
                out.append((parts[1], Path(parts[1]).name))     # tagged by dir name, as dt.rel_bases does
    return out


class Source:
    """One build: its .config, fragments and path bases, and kconfig/dt loaded on first use
    (`load_kconfig`, `load_dt` set them, or the matching `*_why` when they cannot)."""

    def __init__(self, label: str, dotconfig: Path, frags: list[str], bases: list[tuple[str, str]]):
        self.label, self.dotconfig, self.frags, self.bases = label, dotconfig, frags, bases
        self.kconfig: dict | None = None
        self.kconfig_why: str | None = None
        self.dt: dict | None = None
        self.dt_why: str | None = None
        self.load_kconfig = lambda: None
        self.load_dt = lambda: None
        self.elf: Path | None = None
        self.tools: dict[str, str] = {}
        self.cache: Path | None = None

    def rel(self, path: str) -> str:
        return KC._rel(path, "/", self.bases)


def pinned(a) -> Source:
    from .dt import rel_bases
    if not a.atlas:
        raise LookupError("no atlas: run /atlas-build, or pass `build` to name a build dir")
    stamp = read_json(a.atlas / "atlas.json")
    bd = a.atlas / "build"
    src = Source(f"atlas {a.atlas.name}", bd / ".config", KC.fragment_files(bd), rel_bases(stamp, a.dir))
    kj, dj = a.atlas / "kconfig.json", a.atlas / "dt.json"

    def load_kconfig():
        if kj.is_file():
            src.kconfig = read_json(kj)
        else:
            why = (stamp.get("build") or {}).get("kconfig")
            what = (f"its build did not write one ({why})" if isinstance(why, str) and why.startswith("not extracted")
                    else "it was built before kconfig.json existed")
            src.kconfig_why = (f"{a.atlas.name} has no kconfig.json: {what}. Recover without a rebuild: "
                               f"`embarch-atlas kconfig-extract {a.atlas.name}` (needs the build dir and the "
                               "workspace's Zephyr unchanged since the build; it says if not, and then only "
                               "`build <target> --force` does it). Value and fragment assignments only")

    def load_dt():
        if dj.is_file():
            src.dt = read_json(dj)
        else:
            src.dt_why = f"{a.atlas.name} has no dt.json"
    src.load_kconfig, src.load_dt = load_kconfig, load_dt
    src.elf, src.tools, src.cache = bd / "zephyr.elf", (stamp.get("build") or {}).get("tools") or {}, a.dir / "cache"
    return src


def _key(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
        h.update(b"\0")
    return h.hexdigest()[:16]


def named(a, build: str) -> Source:
    fw = a.dir.resolve().parents[1]
    p = Path(build)
    p = p if p.is_absolute() else (fw / p)
    zdir = KC.zephyr_dir(p)
    env, python, _, _ = KC.ninja_menuconfig(zdir)
    zb = env.get("ZEPHYR_BASE", "")
    app_dir = env.get("APPLICATION_SOURCE_DIR") or str(fw)
    top = _git(Path(app_dir), "rev-parse", "--show-toplevel") or str(fw)
    bases = [(top, "fw"), *[(m, n) for m, n in _modules(zdir) if m != top], (zb, "zephyr"), (str(zdir), "build")]
    head = _git(Path(top), "rev-parse", "--short=12", "HEAD")
    dirty = " dirty" if _git(Path(top), "status", "--porcelain", "--untracked-files=no") else ""
    label = (f"build {p} (app {KC.version_h(zdir, 'APP_BUILD_VERSION') or '?'}, zephyr "
             f"{KC.version_h(zdir, 'BUILD_VERSION') or '?'}; fw now {head or '?'}{dirty})")
    frags = KC.fragment_files(zdir)
    src = Source(label, zdir / "zephyr" / ".config", frags, bases)
    cache = a.dir / "cache"

    def load_kconfig():
        blobs = [src.dotconfig.read_bytes(), (KC.version_h(zdir, "BUILD_VERSION") or "").encode()]
        blobs += [Path(f).read_bytes() for f in frags if Path(f).is_file()]
        kc = cache / "kconfig" / f"{_key(*blobs)}.json"
        if kc.is_file():
            src.kconfig = read_json(kc)
            return
        why = KC.workspace_mismatch(zdir)
        if why:
            src.kconfig_why = f"{why}. Value and fragment assignments only; rebuild to get the rest"
            return
        src.kconfig = KC.extract(zdir, bases)
        write_json(kc, src.kconfig)

    def load_dt():
        edt = zdir / "zephyr" / "edt.pickle"
        if not edt.is_file():
            src.dt_why = f"no {edt}"
            return
        dc = cache / "dt" / f"{_key(edt.read_bytes())}.json"
        if dc.is_file():
            src.dt = read_json(dc)
            return
        why = KC.workspace_mismatch(zdir)
        if why:
            src.dt_why = f"{why}; the edt.pickle needs that Zephyr's python-devicetree. Rebuild, then ask again"
            return
        src.dt = _dt_of_build(zdir, Path(zb), bases, python)
        write_json(dc, src.dt)
    src.load_kconfig, src.load_dt = load_kconfig, load_dt
    from .footprint import build_tools
    src.elf, src.tools, src.cache = zdir / "zephyr" / "zephyr.elf", build_tools(zdir / "CMakeCache.txt"), cache
    return src


def _dt_of_build(zdir: Path, zb: Path, bases: list[tuple[str, str]], python: str) -> dict:
    """dt.json's node, use and driver keys for a build dir (no SVD, no MMIO scan)."""
    from . import dt as D
    raw = D.raw_from_pickle(zdir / "zephyr" / "edt.pickle", zb, bases, python)
    rel = D.Rel(bases)
    labels = {l for n in raw["nodes"].values() for l in n["labels"]}
    cc = read_json(zdir / "compile_commands.json") if (zdir / "compile_commands.json").is_file() else []
    tus = sorted({e["file"] for e in cc if e.get("file", "").endswith(".c")})
    srcs = D.scan_sources(tus, labels)
    elf = zdir / "zephyr" / "zephyr.elf"
    inst = D.Elf(elf).device_ordinals() if elf.is_file() else set()
    return {**raw, "drivers": {k: [rel(f) for f in v] for k, v in srcs["drivers"].items()},
            "drivers_not_built": {}, "consumers": {k: [rel(f) for f in v] for k, v in srcs["consumers"].items()},
            "armed": srcs["armed"], "instantiated_ordinals": sorted(inst)}


def source(a, build: str | None) -> Source:
    return named(a, build) if build else pinned(a)


# ---------------------------------------------------------------- config

def _sym_name(q: str) -> str:
    q = q.strip()
    return q[7:] if q.upper().startswith("CONFIG_") else q


def config_answer(src: Source, query: str) -> str:
    src.load_kconfig()
    values = KC.read_dotconfig(src.dotconfig)
    syms = (src.kconfig or {}).get("symbols") or {}
    names = set(syms) | set(values)
    q = _sym_name(query)
    if any(c in q for c in "*?["):
        pat = q.upper()
        hits = sorted(n for n in values if fnmatch.fnmatchcase(n, pat))
        hidden = sum(1 for n in syms if n not in values and fnmatch.fnmatchcase(n, pat))
        lines = [f"CONFIG_{n}={values[n]}" for n in hits[:MAX_LIST]]
        if len(hits) > MAX_LIST:
            lines.append(f"({len(hits) - MAX_LIST} more)")
        if hidden:
            lines.append(f"({hidden} more defined but not in .config: their dependencies are off)")
        return "\n".join(lines) if lines else f"no symbol matches {query!r}"
    name = q if q in names else next((n for n in names if n.lower() == q.lower()), None)
    if not name:
        close = difflib.get_close_matches(q.upper(), sorted(names), n=6, cutoff=0.7)
        return (f"CONFIG_{q} is not a symbol of this build"
                + (f". Nearest: {', '.join('CONFIG_' + c for c in close)}" if close else "")
                + ("" if syms else f" ({src.kconfig_why})"))
    e = syms.get(name) or {}
    value = e.get("v", values.get(name, "n"))
    head = f"CONFIG_{name}={value}" + (f" ({e['t']})" if e.get("t") else "") + (f' "{e["p"]}"' if e.get("p") else "")
    lines = [head]
    asg = e.get("asg")
    if asg is None:
        asg = [[src.rel(f), ln, v] for f, ln, v in KC.assignments(src.frags).get(name, [])]
    if asg:
        for i, (f, ln, v) in enumerate(asg):
            last = i == len(asg) - 1
            note = (" <- not applied: the value above won" if v != value else "") if last else " (overridden below)"
            lines.append(f"assigned {v} in {f}:{ln}{note}")
    else:
        frags = ", ".join(Path(f).name for f in src.frags) or "no fragments recorded"
        lines.append(f"assigned in no fragment ({frags})")
    if not syms:
        lines.append(f"({src.kconfig_why})")
        return "\n".join(lines)
    if e.get("dep"):
        met = {"y": "met", "m": "met as m", "n": "NOT met"}[e.get("dv", "n")]
        lines.append(f"depends on {e['dep']}  [{met}]")
    if e.get("choice"):
        lines.append(f"member of choice {e['choice']}")
    for kind, key in (("selected by", "sel"), ("implied by", "imp")):
        for s, cond in e.get(key) or []:
            lines.append(f"{kind} CONFIG_{s}" + (f" if {cond}" if cond != "y" else ""))
    for i, (v, cond) in enumerate(e.get("def") or []):
        lines.append(f"default {v}" + (f" if {cond}" if cond != "y" else "") + ("  <- applies" if i == 0 else ""))
    for at in e.get("at") or []:
        lines.append(f"defined at {at}")
    if e.get("at_inactive"):
        lines.append(f"(+{e['at_inactive']} definitions under other boards or SoCs, inactive here)")
    if value == "n" and e.get("dv") == "n" and asg and asg[-1][2] != "n":
        lines.append("why n: the fragment asks for it, but its dependencies are not met")
    elif value == "n" and e.get("dv", "y") != "n" and not asg and not e.get("def") and not e.get("sel"):
        lines.append("why n: no fragment sets it, nothing selects it, and no default applies")
    return "\n".join(lines)


# ---------------------------------------------------------------- dt_node

def _find_nodes(dt: dict, q: str) -> list[str]:
    nodes = dt.get("nodes") or {}
    q = q.strip()
    if q in nodes:
        return [q]
    if q.startswith("&"):
        q = q[1:]
    by_label = [p for p, n in nodes.items() if q in (n.get("labels") or [])]
    if by_label:
        return by_label
    alias = (dt.get("aliases") or {}).get(q) or (dt.get("chosen") or {}).get(q)
    if alias:
        return [alias] if alias in nodes else []
    by_compat = [p for p, n in nodes.items() if q in (n.get("compats") or [])]
    if by_compat:
        return by_compat
    by_name = [p for p in nodes if p.rsplit("/", 1)[-1] == q or p.rsplit("/", 1)[-1].split("@")[0] == q]
    if by_name:
        return by_name
    ql = q.lower()
    return [p for p, n in nodes.items() if any(ql in c.lower() for c in n.get("compats") or [])]


def _fmt(v: Any, prop: str) -> str:
    if prop == "reg" and isinstance(v, list) and all(isinstance(x, int) for x in v):
        return "[" + ", ".join(hex(x) for x in v) + "]"
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 160 else s[:157] + "..."


def dt_answer(src: Source, query: str) -> str:
    src.load_dt()
    if not src.dt:
        return f"({src.dt_why})"
    dt = src.dt
    hits = _find_nodes(dt, query)
    nodes = dt.get("nodes") or {}
    if not hits:
        names = sorted({l for n in nodes.values() for l in n.get("labels") or []} | set(nodes))
        close = difflib.get_close_matches(query, names, n=6, cutoff=0.6)
        return f"no node, label, alias or compatible {query!r}" + (f". Nearest: {', '.join(close)}" if close else "")
    if len(hits) > 1:
        rows = [f"{p}  labels {','.join(nodes[p].get('labels') or []) or '-'}  "
                f"{'okay' if nodes[p].get('okay') else 'disabled'}" for p in hits[:MAX_LIST]]
        return f"{len(hits)} nodes match {query!r}; ask for one by path or label:\n" + "\n".join(rows)
    path = hits[0]
    n = nodes[path]
    inst = n.get("ordinal") in set(dt.get("instantiated_ordinals") or [])
    out = [path,
           f"labels {', '.join(n.get('labels') or []) or '-'} | compatible {', '.join(n.get('compats') or []) or '-'} | "
           f"{'okay' if n.get('okay') else 'disabled'} | binding {'yes' if n.get('binding') else 'none'}"
           + (f" | ordinal {n['ordinal']}, {'a device in the ELF' if inst else 'no device in the ELF'}"
              if n.get("ordinal") is not None else "")]
    parent = n.get("parent")
    if parent:
        plabels = ",".join((nodes.get(parent) or {}).get("labels") or [])
        out.append(f"parent {parent}" + (f" ({plabels})" if plabels else "")
                   + (f", on bus {','.join(n['on_bus'])}" if n.get("on_bus") else ""))
    toks = [re.sub(r"[^a-z0-9]", "_", c.lower()) for c in n.get("compats") or []]
    drv = [f for t in toks for f in (dt.get("drivers") or {}).get(t, [])]
    nb = [f for t in toks for f in (dt.get("drivers_not_built") or {}).get(t, [])]
    cons = sorted({f for l in n.get("labels") or [] for f in (dt.get("consumers") or {}).get(l, [])})
    if drv:
        out.append(f"driver {', '.join(drv)} (compiled)")
    elif nb:
        out.append(f"driver {', '.join(nb)} (in the tree, not compiled in this build)")
    elif toks:
        out.append("driver: no DT_DRV_COMPAT for it among the compiled sources")
    if cons:
        out.append(f"referenced by label in {', '.join(cons)}")
    props = n.get("props") or {}
    if props:
        out.append("properties:")
        for k in sorted(props):
            pv = props[k]
            loc = pv.get("loc") if isinstance(pv, dict) else None
            val = pv.get("val") if isinstance(pv, dict) else pv
            out.append(f"  {k} = {_fmt(val, k)}" + (f"  ({loc})" if loc else "  (binding default)"))
    uses = [u for u in dt.get("uses") or [] if u.get("node") == path]
    if uses:
        out.append("pins:")
        for u in uses:
            out.append(f"  {u.get('pin')} {u.get('prop')}[{u.get('index', 0)}] {u.get('flags') or u.get('func') or ''}".rstrip()
                       + (f"  ({u['loc']})" if u.get("loc") else ""))
    kids = sorted(p for p, c in nodes.items() if c.get("parent") == path)
    if kids:
        out.append(f"children ({len(kids)}): " + ", ".join(k.rsplit("/", 1)[-1] for k in kids[:MAX_LIST]))
    return "\n".join(out)


# ---------------------------------------------------------------- footprint, stacks

def _image(src: Source) -> dict:
    from . import footprint as F
    if not src.elf or not src.elf.is_file():
        raise FileNotFoundError(f"no zephyr.elf in this build ({src.elf})")
    return F.image(src.elf, src.tools, src.cache, src.rel)


def footprint_answer(src: Source, by: str = "dir", vs: Source | None = None) -> str:
    from . import footprint as F
    src.load_dt()
    return F.footprint_answer(_image(src), src.dt, by, _image(vs) if vs else None, vs.label if vs else "")


def stacks_answer(src: Source) -> str:
    from . import footprint as F
    src.load_kconfig()
    return F.stacks_answer(_image(src), F.dotconfig_of(src.dotconfig), (src.kconfig or {}).get("symbols"))
