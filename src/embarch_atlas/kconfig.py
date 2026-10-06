"""kconfig.json: every Kconfig symbol of one build, and why it has its value.

Per symbol: its value in the build's `.config`, the configuration fragments that assign it (in
merge order, the last one wins), where it is defined, its `depends on` with that expression's
value, who `select`s or `imply`s it, and its defaults. A fragment assignment the build did not
honour (an unmet `depends on`) is marked.

Kconfig is parsed with the kconfiglib of the Zephyr tree the build used, in the build's own
Python, with the environment Zephyr gives `menuconfig` (read from the build's `build.ninja`).
That tree is the workspace's Zephyr, which moves: so the extraction runs at atlas-build time,
and for any other build dir only while the workspace's `git describe` still equals the
`BUILD_VERSION` the build recorded (`workspace_mismatch`). Without it, a query still has the
value (`.config`) and the fragment assignments, which need no Kconfig tree.

Run as a script (`--extract <build dir> <rel bases json>`), it prints the extraction as JSON:
that is how it runs inside the build's Python.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ASSIGN_RE = re.compile(r"^CONFIG_(\w+)=(.*)$")
UNSET_RE = re.compile(r"^# CONFIG_(\w+) is not set$")
MAX_DEFAULTS = 6


# ---------------------------------------------------------------- files every build has

def zephyr_dir(bdir: Path) -> Path:
    """The dir holding .config and edt.pickle: <bdir>/zephyr, or <bdir>/<app>/zephyr under sysbuild."""
    bdir = Path(bdir)
    if (bdir / "zephyr" / ".config").is_file():
        return bdir
    subs = [p.parent.parent for p in bdir.glob("*/zephyr/.config")]
    if len(subs) == 1:
        return subs[0]
    raise FileNotFoundError(f"no zephyr/.config under {bdir}")


def read_dotconfig(path: Path) -> dict[str, str]:
    """CONFIG_X=v lines of a .config (strings unquoted); `# CONFIG_X is not set` is `n`."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = ASSIGN_RE.match(line)
        if m:
            v = m[2]
            out[m[1]] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v
            continue
        m = UNSET_RE.match(line)
        if m:
            out[m[1]] = "n"
    return out


def fragment_files(bdir: Path) -> list[str]:
    """The configuration fragments the build merged, in merge order (build_info.yml cmake.kconfig.files)."""
    p = Path(bdir) / "build_info.yml"
    if not p.is_file():
        return []
    files, inside, in_files = [], False, False
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^  kconfig:\s*$", line):
            inside, in_files = True, False
            continue
        if inside and re.match(r"^  \S", line):
            break
        if inside and re.match(r"^    files:\s*$", line):
            in_files = True
            continue
        if inside and re.match(r"^    \S", line):
            in_files = False
            continue
        m = re.match(r"^\s+- '?(.*?)'?\s*$", line)
        if inside and in_files and m:
            files.append(m[1])
    extra = Path(bdir) / "zephyr" / "misc" / "generated" / "extra_kconfig_options.conf"
    if extra.is_file():
        files.append(str(extra))
    return files


def assignments(files: list[str]) -> dict[str, list[tuple[str, int, str]]]:
    """Symbol -> [(file, line, value)] over the fragments, in merge order."""
    out: dict[str, list[tuple[str, int, str]]] = {}
    for f in files:
        try:
            lines = Path(f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            s = line.strip()
            m = ASSIGN_RE.match(s)
            if m:
                v = m[2].split("#", 1)[0].strip() if not m[2].startswith('"') else m[2]
                out.setdefault(m[1], []).append((f, i, v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v))
                continue
            m = UNSET_RE.match(s)
            if m:
                out.setdefault(m[1], []).append((f, i, "n"))
    return out


def version_h(bdir: Path, name: str) -> str | None:
    """BUILD_VERSION (Zephyr's git describe) or APP_BUILD_VERSION as the build recorded them."""
    for rel in ("zephyr/include/generated/zephyr/version.h", "zephyr/include/generated/zephyr/app_version.h",
                "zephyr/include/generated/app_version.h"):
        p = Path(bdir) / rel
        if p.is_file():
            m = re.search(rf"^#define {name} (\S+)", p.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                return m[1].strip('"')
    return None


# ---------------------------------------------------------------- the menuconfig environment

def _ninja_unescape(s: str) -> str:
    return re.sub(r"\$(.)", r"\1", s)


def ninja_menuconfig(bdir: Path) -> tuple[dict[str, str], str, str, str]:
    """(env, python, Kconfig root, cwd) of the build's `menuconfig` target."""
    ninja = Path(bdir) / "build.ninja"
    text = ninja.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if "menuconfig.py" not in line or "COMMAND" not in line:
            continue
        cmd = _ninja_unescape(line.split("=", 1)[1].strip())
        cwd = str(bdir)
        for part in cmd.split("&&"):
            toks = shlex.split(part.strip())
            if toks[:1] == ["cd"] and len(toks) > 1:
                cwd = toks[1]
                continue
            if "env" not in toks or not any(t.endswith("menuconfig.py") for t in toks):
                continue
            i = toks.index("env") + 1
            env = {}
            while i < len(toks) and "=" in toks[i] and not toks[i].startswith("/"):
                k, v = toks[i].split("=", 1)
                env[k] = v
                i += 1
            rest = toks[i:]
            script = next(j for j, t in enumerate(rest) if t.endswith("menuconfig.py"))
            python = rest[script - 1] if script else env.get("PYTHON_EXECUTABLE", sys.executable)
            return env, python, rest[script + 1], cwd
    raise FileNotFoundError(f"{ninja}: no menuconfig target")


def workspace_mismatch(bdir: Path) -> str | None:
    """Why this build's Kconfig tree is not the workspace's now, or None when it is."""
    try:
        env, _, _, _ = ninja_menuconfig(bdir)
    except (OSError, FileNotFoundError) as e:
        return str(e)
    zb = env.get("ZEPHYR_BASE")
    built = version_h(bdir, "BUILD_VERSION")
    if not zb or not built:
        return "the build recorded no BUILD_VERSION, so its Zephyr tree cannot be matched"
    r = subprocess.run(["git", "-C", zb, "describe", "--abbrev=12", "--always"], capture_output=True, text=True)
    now = r.stdout.strip()
    if r.returncode or now != built:
        return f"the build used Zephyr {built}; {zb} is at {now or '?'} now"
    return None


# ---------------------------------------------------------------- extraction (in the build's Python)

def _rel(path: str, srctree: str, bases: list[tuple[str, str]]) -> str:
    p = path if os.path.isabs(path) else os.path.join(srctree, path)
    p = os.path.normpath(p)
    for base, tag in sorted(bases, key=lambda b: -len(b[0])):
        if p == base or p.startswith(base.rstrip("/") + "/"):
            return f"{tag}:{os.path.relpath(p, base)}"
    return p


def _extract(bdir: Path, bases: list[tuple[str, str]]) -> dict:
    zb = os.environ["ZEPHYR_BASE"]
    sys.path.insert(0, os.path.join(zb, "scripts", "kconfig"))
    import kconfiglib as K  # noqa: E402  (the build's own)
    root = os.environ["EMBARCH_KCONFIG_ROOT"]
    kconf = K.Kconfig(root, warn=False, suppress_traceback=True)
    zdir = zephyr_dir(bdir)
    kconf.load_config(str(zdir / "zephyr" / ".config"), replace=True)
    srctree = os.environ.get("srctree", zb)
    tri = "nmy"
    rev: dict[str, list] = {}
    weak: dict[str, list] = {}
    for s in kconf.unique_defined_syms:
        if not s.tri_value:                    # only a symbol that is on selects or implies anything
            continue
        for sel in s.selects:                  # (target, cond[, location]) by kconfiglib version
            if K.expr_value(sel[1]):
                rev.setdefault(sel[0].name, []).append([s.name, K.expr_str(sel[1])])
        for imp in s.implies:
            if K.expr_value(imp[1]):
                weak.setdefault(imp[0].name, []).append([s.name, K.expr_str(imp[1])])
    asg = assignments(fragment_files(zdir))
    syms = {}
    for s in kconf.unique_defined_syms:
        e: dict = {"t": K.TYPE_TO_STR[s.orig_type], "v": s.str_value}
        live = [n for n in s.nodes if n.prompt or K.expr_value(n.dep)]    # drop other SoCs' defconfig nodes
        e["at"] = [f"{_rel(n.filename, srctree, bases)}:{n.linenr}" for n in live or s.nodes]
        if len(live) < len(s.nodes):
            e["at_inactive"] = len(s.nodes) - len(live)
        prompt = next((n.prompt[0] for n in s.nodes if n.prompt), None)
        if prompt:
            e["p"] = prompt
        if s.direct_dep is not kconf.y:
            e["dep"] = K.expr_str(s.direct_dep)
            e["dv"] = tri[K.expr_value(s.direct_dep)]
        if s.name in rev:
            e["sel"] = rev[s.name]
        if s.name in weak:
            e["imp"] = weak[s.name]
        active = [d for d in s.defaults if K.expr_value(d[1])]
        if active:                             # the first one is the one that applies
            e["def"] = [[K.expr_str(d[0]), K.expr_str(d[1])] for d in active[:MAX_DEFAULTS]]
        if s.choice:
            e["choice"] = s.choice.name or "<choice>"
        if s.name in asg:
            e["asg"] = [[_rel(f, srctree, bases), ln, v] for f, ln, v in asg[s.name]]
            if asg[s.name][-1][2] != s.str_value and s.orig_type in (K.BOOL, K.TRISTATE, K.INT, K.HEX, K.STRING):
                e["ign"] = True
        syms[s.name] = e
    return {"zephyr": version_h(zdir, "BUILD_VERSION"), "app": version_h(zdir, "APP_BUILD_VERSION"),
            "fragments": [_rel(f, srctree, bases) for f in fragment_files(zdir)], "symbols": syms}


def extract(bdir: Path, bases: list[tuple[str, str]], timeout: int = 300) -> dict:
    """The extraction for a build dir, run in the build's own Python with its menuconfig environment."""
    zdir = zephyr_dir(bdir)
    env, python, root, cwd = ninja_menuconfig(zdir)
    run_env = {**os.environ, **env, "EMBARCH_KCONFIG_ROOT": root}
    r = subprocess.run([python, __file__, "--extract", str(zdir), json.dumps(bases)], capture_output=True,
                       text=True, env=run_env, cwd=cwd if os.path.isdir(cwd) else str(zdir), timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"kconfig extraction failed in {python}:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout)


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--extract":
    json.dump(_extract(Path(sys.argv[2]), [tuple(x) for x in json.loads(sys.argv[3])]), sys.stdout)
