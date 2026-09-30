"""`build <target> [<commit>]`: a Zephyr build of one target at one commit, in its own worktree.

The engineer's working tree, index, branch and build dirs are never touched. The
build runs from a detached git worktree under `<atlas_dir>/.work/`, into a build
dir under `<atlas_dir>/.work/`, and the artifacts the atlas needs are copied to
`atlases/<target>@<commit12>/build/` with the stamp in `atlas.json`.

The firmware repo is usually a Zephyr module itself (`zephyr/module.yml` with
`board_root`/`dts_root`), and west's module list points at the main checkout.
Left alone, the worktree build would pick up the main checkout's boards, DTS and
drivers. So the build passes `-DZEPHYR_MODULES=<every west module except the fw
repo>;<worktree>` and afterwards proves, from `build_info.yml`,
`zephyr_modules.txt` and `compile_commands.json`, that nothing came from the main
checkout. It fails loudly otherwise.

This module also holds the small helpers the other atlas modules share: reading
`atlas.toml`, and resolving an `<atlas>` argument to its directory.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from embarch_atlas import __version__
from embarch_atlas.common import atlas_dir as _default_atlas_dir
from embarch_atlas.common import read_json, sha256_file, write_json

ARTIFACTS = {
    # published name -> path relative to the build's zephyr/ dir (or the build dir itself)
    "edt.pickle": "zephyr/edt.pickle",
    ".config": "zephyr/.config",
    "compile_commands.json": "compile_commands.json",
    "zephyr.elf": "zephyr/zephyr.elf",
    "zephyr.dts": "zephyr/zephyr.dts",
    "build_info.yml": "build_info.yml",
    # not in the minimal set, but cheap and needed by the joins
    "zephyr.dts.d": "zephyr/zephyr.dts.d",
    "zephyr_modules.txt": "zephyr_modules.txt",
}
REQUIRED = ("edt.pickle", ".config", "compile_commands.json", "zephyr.elf", "zephyr.dts", "build_info.yml")


class BuildError(RuntimeError):
    pass


# ---------------------------------------------------------------- config

def resolve_atlas_dir(args) -> Path:
    """`--atlas-dir`, else `<fw>/embarch/atlas` when `--fw` is given, else the git toplevel's."""
    if getattr(args, "atlas_dir", None):
        return Path(args.atlas_dir).resolve()
    if getattr(args, "fw", None):
        return Path(args.fw).resolve() / "embarch" / "atlas"
    return _default_atlas_dir()


def load_toml(ad: Path) -> dict:
    """atlas.toml through `project.load_config` (the one reader); {} when there is none."""
    try:
        from embarch_atlas.project import load_config
    except ImportError:          # project module not installed: read it directly
        p = ad / "atlas.toml"
        if not p.is_file():
            return {}
        with open(p, "rb") as f:
            return tomllib.load(f)
    try:
        return load_config(ad)
    except FileNotFoundError:
        return {}


def target_config(ad: Path, target: str, args=None) -> dict:
    """Merge `atlas.toml` (project, mcu, the named [[targets]] entry) with CLI flags.

    Flags win. Returns plain values; missing required ones raise BuildError naming the flag.
    """
    toml = load_toml(ad)
    proj = toml.get("project", {})
    mcu = toml.get("mcu", {})
    tgt = next((t for t in toml.get("targets", []) if t.get("name") == target), {})
    a = args
    fw = getattr(a, "fw", None) or proj.get("fw")
    if not fw:
        fw = str(ad.parent.parent) if ad.name == "atlas" and ad.parent.name == "embarch" else None
    cfg = {
        "target": target,
        "fw": fw,
        "west_topdir": getattr(a, "west_topdir", None) or proj.get("west_topdir"),
        "west": getattr(a, "west", None) or proj.get("west"),
        "sources": proj.get("sources", "embarch/hardware"),
        "board": getattr(a, "board", None) or tgt.get("board"),
        "app": getattr(a, "app", None) or tgt.get("app"),
        "schematic_boards": (getattr(a, "schematic_boards", None) or "").split(",") if getattr(a, "schematic_boards", None)
        else list(tgt.get("schematic_boards", [])),
        "extra_args": list(getattr(a, "extra_arg", None) or tgt.get("extra_args", [])),
        "mcu": {"part": getattr(a, "mcu", None) or mcu.get("part"), "svd": getattr(a, "svd", None) or mcu.get("svd"),
                "ref": getattr(a, "mcu_ref", None) or mcu.get("ref")},
        "boards": {**{b.get("label"): b.get("pdf") for b in toml.get("boards", []) if b.get("label")},
                   **dict(x.split("=", 1) for x in (getattr(a, "board_pdf", None) or []))},
        "from_toml": bool(toml),
    }
    return cfg


def resolve_atlas(ad: Path, ref: str) -> Path:
    """An `<atlas>` argument: a path, an id (`<target>@<commit12>`), a target name (its current atlas)."""
    p = Path(ref)
    if (p / "atlas.json").is_file():
        return p.resolve()
    cand = ad / "atlases" / ref
    if (cand / "atlas.json").is_file():
        return cand
    cur = ad / "current.json"
    if cur.is_file():                      # a target name: the atlas matching HEAD (project.status's shape)
        for m in read_json(cur).get("matches", []):
            if str(m.get("atlas", "")).startswith(f"{ref}@") and (ad / "atlases" / m["atlas"] / "atlas.json").is_file():
                return ad / "atlases" / m["atlas"]
    matches = sorted((ad / "atlases").glob(f"{ref}*")) if (ad / "atlases").is_dir() else []
    matches = [m for m in matches if (m / "atlas.json").is_file()]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(f"no atlas matches {ref!r} under {ad / 'atlases'}"
                     + (f" (ambiguous: {[m.name for m in matches]})" if matches else ""))


# ---------------------------------------------------------------- git

def _git(repo: Path, *argv: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True)
    if check and r.returncode:
        raise BuildError(f"git -C {repo} {' '.join(argv)}: {r.stderr.strip()}")
    return r.stdout.strip()


def resolve_commit(fw: Path, commit: str) -> str:
    return _git(fw, "rev-parse", "--verify", f"{commit}^{{commit}}")


def ensure_worktree(fw: Path, ad: Path, commit: str) -> Path:
    """Detached worktree at `<ad>/.work/<commit12>/<fw basename>`; reused when present and clean.

    The basename matches the fw repo's so the Zephyr module keeps its name
    (zephyr_module.py names a module without `name:` after its directory).
    """
    wt = ad / ".work" / commit[:12] / fw.name
    if wt.exists():
        if not (wt / ".git").exists():
            raise BuildError(f"{wt} exists but is not a git worktree; remove it and re-run")
        head = _git(wt, "rev-parse", "HEAD")
        if head != commit:
            raise BuildError(f"worktree {wt} is at {head[:12]}, not {commit[:12]}; "
                             f"remove it with: git -C {fw} worktree remove {wt}")
        dirty = _git(wt, "status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise BuildError(f"worktree {wt} is not clean:\n{dirty}\nremove it with: git -C {fw} worktree remove --force {wt}")
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(fw, "worktree", "add", "--detach", str(wt), commit)
    return wt


# ---------------------------------------------------------------- west / modules

def find_west(cfg: dict) -> str:
    if cfg.get("west"):
        return cfg["west"]
    top = Path(cfg["west_topdir"])
    for cand in (top / "venv" / "bin" / "west", top / ".venv" / "bin" / "west"):
        if cand.is_file():
            return str(cand)
    w = shutil.which("west")
    if not w:
        raise BuildError("west not found: pass --west or set [project] west in atlas.toml")
    return w


def west_projects(west: str, topdir: Path) -> list[dict]:
    """Every active project: name, path, revision, manifest sha, abspath, and the checked-out HEAD."""
    r = subprocess.run([west, "list", "-f", "{name}\t{path}\t{revision}\t{sha}\t{abspath}"],
                       cwd=topdir, capture_output=True, text=True)
    if r.returncode:
        raise BuildError(f"west list failed: {r.stderr.strip()}")
    out = []
    for line in r.stdout.splitlines():
        f = line.split("\t")
        if len(f) != 5:
            continue
        name, path, rev, sha, absp = (x.strip() for x in f)
        sha = None if sha in ("N/A", "") else sha
        head = _git(Path(absp), "rev-parse", "HEAD", check=False) if (Path(absp) / ".git").exists() else ""
        out.append({"name": name, "path": path, "revision": rev, "sha": sha, "abspath": absp, "head": head or None})
    return out


def is_zephyr_module(path: Path) -> bool:
    """zephyr_module.py's own test: zephyr/module.yml|yaml, or zephyr/CMakeLists.txt + zephyr/Kconfig."""
    z = path / "zephyr"
    return (z / "module.yml").is_file() or (z / "module.yaml").is_file() or (
        (z / "CMakeLists.txt").is_file() and (z / "Kconfig").is_file())


def module_list(projects: list[dict], fw: Path, wt: Path, zephyr_base: Path | None) -> list[str]:
    mods = []
    for p in projects:
        ap = Path(p["abspath"]).resolve()
        if ap == fw.resolve() or (zephyr_base and ap == zephyr_base.resolve()):
            continue
        if is_zephyr_module(ap):
            mods.append(str(ap))
    if is_zephyr_module(wt):
        mods.append(str(wt))
    return mods


def _module_setting(mod: Path, key: str) -> str | None:
    """A `build: settings: <key>` value from module.yml, read without a YAML parser."""
    for name in ("module.yml", "module.yaml"):
        f = mod / "zephyr" / name
        if f.is_file():
            m = re.search(rf"^\s*{key}\s*:\s*(\S+)", f.read_text(encoding="utf-8"), re.M)
            return m[1].strip("'\"") if m else None
    return None


def board_definitions(board: str, roots: list[Path]) -> list[str]:
    """Directories under `<root>/boards/` whose board.yml names `board`."""
    hits = []
    pat = re.compile(rf"^\s*-?\s*name\s*:\s*['\"]?{re.escape(board)}['\"]?\s*$", re.M)
    for root in roots:
        bdir = root / "boards"
        if not bdir.is_dir():
            continue
        for by in bdir.rglob("board.yml"):
            try:
                if pat.search(by.read_text(encoding="utf-8", errors="replace")):
                    hits.append(str(by.parent))
            except OSError:
                continue
    return sorted(set(hits))


def board_roots(mods: list[str], zephyr_base: Path | None, app_dir: Path) -> list[Path]:
    roots = [zephyr_base] if zephyr_base else []
    for m in mods:
        br = _module_setting(Path(m), "board_root")
        if br:
            roots.append((Path(m) / br).resolve())
    roots.append(app_dir)
    return roots


# ---------------------------------------------------------------- verification

def _under(p: str, base: Path) -> bool:
    try:
        Path(os.path.normpath(p)).relative_to(base)
        return True
    except ValueError:
        return False


def _yml_paths(text: str) -> list[str]:
    return re.findall(r"'(/[^']+)'", text) + re.findall(r'"(/[^"]+)"', text)


def verify_from_worktree(bdir: Path, fw: Path, wt: Path, ad: Path) -> dict:
    """Prove every fw-repo input of the build came from the worktree. Raises BuildError otherwise.

    A path is a leak when it is under the main checkout but under neither the
    worktree nor the atlas dir (the build dir itself lives in the atlas dir).
    """
    fw, wt, ad = fw.resolve(), wt.resolve(), ad.resolve()

    def leak(p: str) -> bool:
        return _under(p, fw) and not _under(p, wt) and not _under(p, ad)

    ev: dict[str, Any] = {"worktree": str(wt)}
    leaks: list[str] = []
    bi = bdir / "build_info.yml"
    if not bi.is_file():
        raise BuildError(f"{bi} missing: cannot prove where the build came from")
    bit = bi.read_text(encoding="utf-8")
    paths = _yml_paths(bit)
    leaks += [f"build_info.yml: {p}" for p in paths if leak(p)]
    board_dir = re.search(r"board:\s*\n(?:.*\n)*?\s*path:\s*\n\s*-\s*'([^']+)'", bit)
    app_dir = re.search(r"source-dir:\s*'([^']+)'", bit)
    ev["board_dir"] = board_dir[1] if board_dir else None
    ev["app_source_dir"] = app_dir[1] if app_dir else None
    ev["build_info_paths_in_worktree"] = sum(1 for p in paths if _under(p, wt))
    if ev["app_source_dir"] and not _under(ev["app_source_dir"], wt):
        leaks.append(f"application source-dir {ev['app_source_dir']} is not in the worktree")
    zm = bdir / "zephyr_modules.txt"
    if zm.is_file():
        mods = [re.findall(r'"([^"]*)"', line) for line in zm.read_text(encoding="utf-8").splitlines() if line.strip()]
        ev["zephyr_modules"] = {m[0]: m[1] for m in mods if len(m) >= 2}
        leaks += [f"zephyr_modules.txt: {m[0]} -> {m[1]}" for m in mods if len(m) >= 2 and leak(m[1])]
        ev["fw_module_is_worktree"] = any(len(m) >= 2 and Path(m[1]).resolve() == wt for m in mods)
    cc = bdir / "compile_commands.json"
    if not cc.is_file():
        raise BuildError(f"{cc} missing")
    tus = json.loads(cc.read_text(encoding="utf-8"))
    n_wt = n_leak_tu = n_leak_inc = 0
    for e in tus:
        f = e.get("file", "")
        if _under(f, wt):
            n_wt += 1
        if leak(f):
            n_leak_tu += 1
            if n_leak_tu <= 5:
                leaks.append(f"compile_commands.json TU {f}")
        for inc in re.findall(r"(?:-I|-isystem\s+|-imacros\s+)(/\S+)", e.get("command", "")):
            if leak(inc):
                n_leak_inc += 1
                if n_leak_inc <= 5:
                    leaks.append(f"compile_commands.json include {inc}")
    ev.update({"tus": len(tus), "tus_in_worktree": n_wt, "tus_from_main_checkout": n_leak_tu,
               "includes_from_main_checkout": n_leak_inc})
    dts_d = bdir / "zephyr" / "zephyr.dts.d"
    if dts_d.is_file():
        deps = [t for t in dts_d.read_text(encoding="utf-8").split() if t.startswith("/")]
        ev["dts_inputs_in_worktree"] = sorted(os.path.relpath(p, wt) for p in deps if _under(p, wt))
        leaks += [f"zephyr.dts.d: {p}" for p in deps if leak(p)]
    av = bdir / "zephyr" / "include" / "generated" / "zephyr" / "app_version.h"
    if av.is_file():
        m = re.search(r'#define\s+APP_BUILD_VERSION\s+(\S+)', av.read_text(encoding="utf-8"))
        ev["app_build_version"] = m[1] if m else None
    if leaks:
        raise BuildError("the build did not come (only) from the worktree:\n  " + "\n  ".join(leaks))
    if ev.get("board_dir") and _under(ev["board_dir"], fw) and not _under(ev["board_dir"], wt):
        raise BuildError(f"board dir {ev['board_dir']} is in the main checkout")
    return ev


# ---------------------------------------------------------------- stamp

def _tool_git_sha() -> str | None:
    here = Path(__file__).resolve().parent
    r = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def skill_versions() -> dict:
    """sha256[:12] of each atlas skill's SKILL.md: the installed copy, else the repo's."""
    out = {}
    repo_skills = Path(__file__).resolve().parents[2] / "skills"
    for base in (Path.home() / ".claude" / "skills", repo_skills):
        for s in sorted(base.glob("atlas-*/SKILL.md")) if base.is_dir() else []:
            out.setdefault(s.parent.name, sha256_file(s)[:12])
    return out


def stamp(cfg: dict, ad: Path, commit: str, projects: list[dict], fw: Path, extra: dict) -> dict:
    manifest = ad / "inventory" / "manifest.json"
    schematics = {}
    src_dir = (fw / cfg["sources"]) if cfg.get("sources") else fw
    for label, pdf in sorted(cfg.get("boards", {}).items()):
        if not pdf or (cfg.get("schematic_boards") and label not in cfg["schematic_boards"]):
            continue
        p = Path(pdf) if Path(pdf).is_absolute() else src_dir / pdf
        schematics[label] = sha256_file(p) if p.is_file() else None
    west = []
    for p in projects:
        row = {k: p[k] for k in ("name", "path", "revision", "sha", "head")}
        if Path(p["abspath"]).resolve() == fw.resolve():
            row["head"] = commit          # the atlas builds the fw repo at its pinned commit, not the checkout
            row["atlas_commit"] = True
        west.append(row)
    return {
        "id": f"{cfg['target']}@{commit[:12]}",
        "target": cfg["target"],
        "board": cfg["board"],
        "app": cfg["app"],
        "schematic_boards": cfg.get("schematic_boards") or [],
        "commit": commit,
        "dirty": False,
        "west": west,
        "sources": sha256_file(manifest) if manifest.is_file() else None,
        "schematics": schematics,
        "mcu": cfg.get("mcu", {}),
        "tool": {"name": "embarch-atlas", "version": __version__, "git": _tool_git_sha()},
        "skill_versions": skill_versions(),
        "created": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **extra,
    }


# ---------------------------------------------------------------- the verb

def split_extra(extra: list[str]) -> tuple[list[str], list[str]]:
    if "--" in extra:
        i = extra.index("--")
        return extra[:i], extra[i + 1:]
    return list(extra), []


def west_command(west: str, bdir: Path, board: str, app_dir: Path, extra: list[str], modules: list[str]) -> list[str]:
    west_args, cmake_args = split_extra(extra)
    cmake_args = [a for a in cmake_args if not a.startswith("-DZEPHYR_MODULES=")]
    return [west, "build", "-d", str(bdir), "-b", board, "-s", str(app_dir), "-p", "auto", *west_args,
            "--", *cmake_args, "-DZEPHYR_MODULES=" + ";".join(modules)]


def _zephyr_base(topdir: Path, west: str) -> Path | None:
    r = subprocess.run([west, "config", "zephyr.base"], cwd=topdir, capture_output=True, text=True)
    zb = r.stdout.strip()
    if zb:
        return (topdir / zb).resolve() if not Path(zb).is_absolute() else Path(zb)
    return (topdir / "zephyr") if (topdir / "zephyr").is_dir() else None


def run_build(ad: Path, cfg: dict, commit_ref: str = "HEAD", force: bool = False, log=print) -> Path:
    for k, flag in (("fw", "--fw"), ("west_topdir", "--west-topdir"), ("board", "--board"), ("app", "--app")):
        if not cfg.get(k):
            raise BuildError(f"{k} unknown: set it in atlas.toml or pass {flag}")
    fw = Path(cfg["fw"]).resolve()
    topdir = Path(cfg["west_topdir"]).resolve()
    commit = resolve_commit(fw, commit_ref)
    c12 = commit[:12]
    atlas_id = f"{cfg['target']}@{c12}"
    out = ad / "atlases" / atlas_id
    if (out / "atlas.json").is_file() and not force:
        log(f"{atlas_id}: already built ({out}); atlases are never edited (pass --force to rebuild)")
        return out
    west = find_west(cfg)
    projects = west_projects(west, topdir)
    zb = _zephyr_base(topdir, west)
    wt = ensure_worktree(fw, ad, commit)
    log(f"worktree {wt}")
    mods = module_list(projects, fw, wt, zb)
    app_dir = wt / cfg["app"]
    if not app_dir.is_dir():
        raise BuildError(f"app {cfg['app']} not found at {commit[:12]} ({app_dir})")
    defs = board_definitions(cfg["board"], board_roots(mods, zb, app_dir))
    if len(defs) > 1:
        raise BuildError(f"board {cfg['board']} is defined {len(defs)} times: {defs}")
    main_defs = board_definitions(cfg["board"], board_roots([str(fw)] if is_zephyr_module(fw) else [], None, fw))
    bdir = ad / ".work" / f"build-{cfg['target']}-{c12}"
    eng_build = fw / "embarch" / "build"
    if _under(str(bdir), eng_build.resolve()):
        raise BuildError(f"refusing to build into the engineer's {eng_build}")
    cmd = west_command(west, bdir, cfg["board"], app_dir, cfg.get("extra_args", []), mods)
    env = {k: v for k, v in os.environ.items() if k not in ("ZEPHYR_EXTRA_MODULES", "EXTRA_ZEPHYR_MODULES", "ZEPHYR_MODULES")}
    # west from a venv: that venv's tools (cmake, ninja, the Python with Zephyr's requirements) come first,
    # as they do for the engineer's own builds.
    wbin = str(Path(west).resolve().parent) if os.sep in west else None
    if wbin:
        env["PATH"] = wbin + os.pathsep + env.get("PATH", "")
        env.setdefault("VIRTUAL_ENV", str(Path(wbin).parent))
    logf = ad / ".work" / f"build-{cfg['target']}-{c12}.log"
    log(f"west build -> {bdir} (log {logf})")
    with open(logf, "w", encoding="utf-8") as lf:
        lf.write(" ".join(cmd) + "\n")
        lf.flush()
        r = subprocess.run(cmd, cwd=topdir, stdout=lf, stderr=subprocess.STDOUT, env=env)
    if r.returncode:
        tail = logf.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        raise BuildError(f"west build failed ({r.returncode}); last lines of {logf}:\n" + "\n".join(tail))
    zdir = bdir
    if not (bdir / "zephyr" / "edt.pickle").is_file():   # sysbuild layout: <bdir>/<app>/zephyr/
        subs = [p.parent.parent for p in bdir.glob("*/zephyr/edt.pickle")]
        if len(subs) != 1:
            raise BuildError(f"no zephyr/edt.pickle under {bdir}")
        zdir = subs[0]
    ev = verify_from_worktree(zdir, fw, wt, ad)
    ev["board_definitions"] = defs
    ev["main_checkout_board_definitions_excluded"] = main_defs
    dst = out / "build"
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for name, rel in ARTIFACTS.items():
        src = zdir / rel
        if src.is_file():
            shutil.copy2(src, dst / name)
            copied.append(name)
        elif name in REQUIRED:
            raise BuildError(f"build artifact missing: {src}")
    tools = _cmake_tools(zdir)
    extra = {"build": {"worktree": os.path.relpath(wt, ad), "dir": os.path.relpath(bdir, ad),
                       "command": [os.path.relpath(c, ad) if c.startswith(str(ad)) else c for c in cmd],
                       "zephyr_modules": mods, "artifacts": copied, "tools": tools,
                       "zephyr_base": str(zb) if zb else None, "west_topdir": str(topdir), "fw": str(fw),
                       "from_worktree": ev}}
    write_json(out / "atlas.json", stamp(cfg, ad, commit, projects, fw, extra))
    log(f"{atlas_id}: built from {wt}; {ev['tus_in_worktree']}/{ev['tus']} TUs from the worktree, "
        f"0 from the main checkout; board dir {ev.get('board_dir')}")
    return out


def _cmake_tools(bdir: Path) -> dict:
    cc = bdir / "CMakeCache.txt"
    out = {}
    if cc.is_file():
        for line in cc.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(CMAKE_NM|CMAKE_OBJDUMP|_?Python3_EXECUTABLE|ZEPHYR_SDK_INSTALL_DIR):\w+=(.*)", line)
            if m:
                out[m[1].lstrip("_")] = m[2]
    return out


def cmd_build(args) -> int:
    ad = resolve_atlas_dir(args)
    cfg = target_config(ad, args.target, args)
    if not cfg["from_toml"]:
        print(f"note: no {ad / 'atlas.toml'}; using flags only", file=sys.stderr)
    try:
        out = run_build(ad, cfg, args.commit, force=args.force)
    except BuildError as e:
        print(f"build: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def add_config_flags(p) -> None:
    """Flags that stand in for atlas.toml values (shared with `join`)."""
    p.add_argument("--fw", help="firmware git repo (default: atlas.toml [project] fw)")
    p.add_argument("--west-topdir", help="west workspace (default: atlas.toml [project] west_topdir)")
    p.add_argument("--west", help="west executable (default: <topdir>/venv/bin/west, else PATH)")
    p.add_argument("--board", help="Zephyr board (default: the target's `board`)")
    p.add_argument("--app", help="app dir relative to the fw repo (default: the target's `app`)")
    p.add_argument("--schematic-boards", help="comma-separated schematic board labels")
    p.add_argument("--extra-arg", action="append", help="extra `west build` argument (repeatable; `--` then cmake args)")
    p.add_argument("--mcu", help="MCU part family: the MPN prefix of the MCU on the schematic")
    p.add_argument("--svd", help="local SVD file")
    p.add_argument("--mcu-ref", help="MCU refdes, board-qualified (e.g. B:U3); default: found by MPN")
    p.add_argument("--board-pdf", action="append", help="LABEL=<pdf in sources> (repeatable; default: atlas.toml [[boards]])")


def register(sub, groups) -> None:
    p = sub.add_parser("build", help="worktree, west build, artifacts, stamp")
    p.add_argument("target")
    p.add_argument("commit", nargs="?", default="HEAD")
    p.add_argument("--force", action="store_true", help="rebuild an existing atlas (development only)")
    add_config_flags(p)
    p.set_defaults(func=cmd_build)
