"""Bump the atlas when HEAD moves: `hook install|uninstall|run`, the bump lock and the bump state.

`update <target>` bumps an atlas in one command; what was left was remembering to run it. `hook install`
writes `post-commit`, `post-checkout`, `post-merge` and `post-rewrite` hooks into the firmware repo's hooks
dir. Each one records the move (`bump/moved`: its mtime is the time of the move, its text the checkout it
happened in) and, unless a worker is already waiting, starts `hook run` detached. The worker waits until HEAD
has been quiet for the debounce interval (a rebase moves HEAD many times) and no rebase, merge, cherry-pick,
revert or bisect is in progress there, then runs `update` for each target under the bump lock, logging to
`bump/log`. A move during a bump starts another round once the bump ends; a burst of moves is one bump.

The bump lock (`bump/update.lock`, an exclusive `flock`) is taken by every `update`, from the hook or by hand,
so a second concurrent bump is refused rather than racing the first over `.work/` and `atlases/`. A second
worker is refused by `bump/worker.lock`.

Every `update` records its outcome in `bump/state.json`, per target: running (with its pid), ok, stopped or
failed, with the reason. While an atlas of that target is stale, `project.atlas_state` turns a running, stopped
or failed bump to HEAD into a stale reason, so the MAP's `> **Stale:**` line and every lookup's `src:` line say
why the atlas did not follow HEAD. A bump that is running hides the atlas it is building from `current.json`
and from the lookups until that atlas has joined (`joining`): the previous atlas stays the one answering.

The hook never runs a second look: `update` only queues its batches.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from . import common

BUMP_DIR = "bump"
STATE = "state.json"
LOG = "log"
MOVED = "moved"
HANDLED = "handled"          # the `moved` mtime (ns) the last bump covered
WORKER_PID = "worker.pid"
WORKER_LOCK = "worker.lock"
UPDATE_LOCK = "update.lock"
HOOKS = ("post-commit", "post-checkout", "post-merge", "post-rewrite")
BEGIN = "# >>> embarch-atlas bump"
END = "# <<< embarch-atlas bump"
DEFAULT_DEBOUNCE_S = 10
POLL_S = 0.5
HANDOFF_S = 5.0     # a worker started while the last one is exiting waits this long for its lock
# git operations that move HEAD many times before they end: a bump waits them out
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
# what a hook's environment carries that would point every `git -C <other repo>` at the firmware repo
GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
           "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH")


def bump_dir(ad: Path) -> Path:
    return Path(ad) / BUMP_DIR


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- locks

def try_lock(ad: Path, name: str) -> Any:
    """An exclusive, non-blocking `flock` on `bump/<name>`: the open file (keep it to hold the lock), or
    None when another process holds it."""
    p = bump_dir(ad) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def wait_lock(ad: Path, name: str, timeout_s: float, poll_s: float = POLL_S) -> Any:
    """`try_lock`, retried until `timeout_s`; None when it never came free."""
    end = time.monotonic() + timeout_s
    while True:
        fh = try_lock(ad, name)
        if fh is not None or time.monotonic() >= end:
            return fh
        time.sleep(poll_s)


def release(fh: Any) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------- state

def state_path(ad: Path) -> Path:
    return bump_dir(ad) / STATE


def read_state(ad: Path) -> dict[str, dict]:
    """target -> the last bump's state: {head, result: running|ok|stopped|failed, reason, started, finished, pid, via}."""
    try:
        s = common.read_json(state_path(ad))
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def write_state(ad: Path, target: str, **fields: Any) -> None:
    """Replace one target's state (written under the bump lock, so no two writers race)."""
    s = read_state(ad)
    s[target] = {k: v for k, v in fields.items() if v is not None}
    p = state_path(ad)
    tmp = p.with_suffix(".tmp")
    common.write_json(tmp, s)
    os.replace(tmp, p)


def busy(ad: Path) -> str:
    """Who holds the bump lock, from the state file: "another bump is running (<target> to <head12>, pid N, since T)"."""
    run = [(t, s) for t, s in read_state(ad).items() if s.get("result") == "running" and pid_alive(s.get("pid"))]
    if not run:
        return "another bump is running"
    t, s = run[0]
    return (f"another bump is running ({t} to {str(s.get('head') or '?')[:12]}, pid {s.get('pid')}, since {s.get('started')}; "
            f"log {BUMP_DIR}/{LOG})")


def stale_reason(ad: Path, target: str | None, head: str | None) -> str | None:
    """Why the atlas of `target` did not follow HEAD, when the last bump to HEAD says so: running, stopped,
    failed, or started and never finished. None when no bump to this HEAD was tried."""
    if not target or not head:
        return None
    s = read_state(ad).get(target) or {}
    if s.get("head") != head:
        return None
    res = s.get("result")
    if res == "running":
        if pid_alive(s.get("pid")):
            return f"a bump to HEAD is running (since {s.get('started')}; {BUMP_DIR}/{LOG})"
        return f"a bump to HEAD started {s.get('started')} and did not finish (see {BUMP_DIR}/{LOG}): run `embarch-atlas update {target}`"
    if res in ("stopped", "failed"):
        return f"the bump to HEAD {res} ({s.get('finished')}): {s.get('reason') or 'no reason recorded'}"
    return None


def joining(ad: Path, atlas: Path, stamp: dict) -> bool:
    """True while a running bump is building or joining this atlas (its target and commit), until its join
    has written the atlas's MAP.md: such an atlas is not current yet, and the previous one keeps answering."""
    s = read_state(ad).get(str(stamp.get("target"))) or {}
    return (s.get("result") == "running" and s.get("head") == stamp.get("commit") and pid_alive(s.get("pid"))
            and not (Path(atlas) / "MAP.md").is_file())


# ---------------------------------------------------------------- hooks

def hooks_dir(fw: Path) -> Path:
    """The firmware repo's hooks dir as git resolves it (`core.hooksPath`, the common dir of a linked worktree)."""
    from .project import _git_path
    p = _git_path(str(fw), "--git-path", "hooks")
    if not p:
        raise RuntimeError(f"{fw} is not a git repository")
    return Path(p)


def _block(hook: str, ad: Path, targets: list[str], debounce_s: float, python: str) -> str:
    d = shlex.quote(str(bump_dir(ad)))
    run = " ".join([shlex.quote(python), "-m", "embarch_atlas.cli", "--atlas-dir", shlex.quote(str(ad)), "hook", "run",
                    "--debounce", f"{debounce_s:g}", *map(shlex.quote, targets)])
    body = [
        f"_ea_d={d}",
        f'mkdir -p "$_ea_d" && pwd > "$_ea_d/{MOVED}"',
        f'if ! {{ [ -f "$_ea_d/{WORKER_PID}" ] && kill -0 "$(cat "$_ea_d/{WORKER_PID}" 2>/dev/null)" 2>/dev/null; }}; then',
        f"  nohup {run} >/dev/null 2>&1 &",
        "fi",
    ]
    if hook == "post-checkout":       # a file checkout ($3 = 0) does not move HEAD
        body = ['if [ "$3" != 0 ]; then'] + ["  " + x for x in body] + ["fi"]
    return "\n".join([f"{BEGIN} (`embarch-atlas hook uninstall` removes this block)", *body, END]) + "\n"


def _strip(text: str) -> str:
    """The hook text without our block."""
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith(BEGIN):
            inside = True
            continue
        if inside:
            inside = not line.startswith(END)
            continue
        out.append(line)
    return "".join(out)


OWN_HEAD = "#!/bin/sh\n# written by embarch-atlas hook install\n"


def install(ad: Path, targets: list[str], debounce_s: float = DEFAULT_DEBOUNCE_S, fw: Path | None = None,
            python: str | None = None) -> list[str]:
    """Write the four hooks (our block replaces an older one; a hook that is not ours keeps its text and gets
    the block appended). Returns what changed."""
    from .project import configured_fw, load_config
    ad = Path(ad).resolve()
    fw = fw or configured_fw(ad, load_config(ad))
    hd = hooks_dir(fw)
    hd.mkdir(parents=True, exist_ok=True)
    python = python or sys.executable
    done = []
    for hook in HOOKS:
        p = hd / hook
        old = p.read_text(encoding="utf-8") if p.is_file() else ""
        rest = _strip(old)
        if not rest.strip():
            rest = OWN_HEAD
        sep = "" if rest.endswith("\n") else "\n"
        new = rest + sep + _block(hook, ad, targets, debounce_s, python)
        if new != old:
            p.write_text(new, encoding="utf-8")
            done.append(f"{p}: {'block replaced' if BEGIN in old else 'block appended to an existing hook' if old else 'written'}")
        p.chmod(p.stat().st_mode | 0o111)
    return done


def uninstall(ad: Path, fw: Path | None = None) -> list[str]:
    """Remove our block from the four hooks; delete a hook that held nothing else."""
    from .project import configured_fw, load_config
    ad = Path(ad).resolve()
    fw = fw or configured_fw(ad, load_config(ad))
    hd = hooks_dir(fw)
    done = []
    for hook in HOOKS:
        p = hd / hook
        if not p.is_file():
            continue
        old = p.read_text(encoding="utf-8")
        if BEGIN not in old:
            continue
        rest = _strip(old)
        if rest.strip() in ("", OWN_HEAD.strip()):
            p.unlink()
            done.append(f"{p}: removed")
        else:
            p.write_text(rest, encoding="utf-8")
            done.append(f"{p}: block removed, the rest of the hook kept")
    return done


def installed(ad: Path, fw: Path | None = None) -> list[str]:
    """The hooks that carry our block."""
    from .project import configured_fw, load_config
    try:
        fw = fw or configured_fw(ad, load_config(ad))
        hd = hooks_dir(fw)
    except (FileNotFoundError, RuntimeError):
        return []
    return [h for h in HOOKS if (hd / h).is_file() and BEGIN in (hd / h).read_text(encoding="utf-8")]


# ---------------------------------------------------------------- the worker

def in_progress(checkout: Path) -> str | None:
    """The git operation still moving HEAD in this checkout (a rebase, a merge...), or None."""
    from .project import _git_path
    for name in IN_PROGRESS:
        p = _git_path(str(checkout), "--git-path", name)
        if p and Path(p).exists():
            return name
    return None


def wait_quiet(ad: Path, debounce_s: float, poll_s: float = POLL_S, log=print) -> tuple[Path, int] | None:
    """Wait until the last recorded move is `debounce_s` old and nothing is still moving HEAD there.
    Returns (the checkout it happened in, the move's mtime in ns), or None when no move is recorded."""
    moved = bump_dir(ad) / MOVED
    said = None
    while True:
        try:
            st = moved.stat()
            checkout = Path(moved.read_text(encoding="utf-8").strip() or ".")
        except OSError:
            return None
        quiet = time.time() - st.st_mtime
        if quiet < debounce_s:
            time.sleep(min(poll_s, debounce_s - quiet))
            continue
        busy_op = in_progress(checkout) if checkout.is_dir() else None
        if busy_op:
            if busy_op != said:
                log(f"waiting: {busy_op} in progress in {checkout}")
                said = busy_op
            time.sleep(poll_s)
            continue
        return checkout, st.st_mtime_ns


def _logger(ad: Path):
    p = bump_dir(ad) / LOG
    p.parent.mkdir(parents=True, exist_ok=True)

    def log(*parts: Any) -> None:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{_now()} {' '.join(str(x) for x in parts)}\n")
    return log


def _moved_ns(ad: Path) -> int:
    try:
        return (bump_dir(ad) / MOVED).stat().st_mtime_ns
    except OSError:
        return 0


def _handled_ns(ad: Path) -> int:
    try:
        return int((bump_dir(ad) / HANDLED).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def run_worker(ad: Path, targets: list[str], debounce_s: float = DEFAULT_DEBOUNCE_S, poll_s: float = POLL_S,
               lock_timeout_s: float = 3600.0, handoff_s: float = HANDOFF_S) -> int:
    """Debounce HEAD moves, then bump each target under the bump lock, until a bump ends with no newer move.
    0: done (or nothing newer than the last bump); 3: another worker is already waiting (it will see this move).

    The hook writes `moved`, then starts a worker unless `worker.pid` names a live one; a worker that is done
    removes `worker.pid` first and looks at `moved` after, so a move is never left with no worker to see it.
    Hooks that fire before the first worker has written its pid start more workers; each waits for the worker
    lock and then finds `handled` (the last move a bump covered) at or past `moved`, and exits."""
    from . import project, update
    ad = Path(ad).resolve()
    for k in GIT_ENV:                       # a hook's environment points git at the firmware repo
        os.environ.pop(k, None)
    wl = wait_lock(ad, WORKER_LOCK, handoff_s, poll_s)
    if wl is None:
        print("hook run: another bump worker is already waiting; it will see this move")
        return 3
    pidf = bump_dir(ad) / WORKER_PID
    pidf.write_text(f"{os.getpid()}\n", encoding="utf-8")
    log = _logger(ad)
    try:
        while True:
            got = wait_quiet(ad, debounce_s, poll_s, log)
            if got is None:
                log("no move recorded; nothing to bump")
                return 0
            checkout, seen = got
            if seen <= _handled_ns(ad):
                return 0                    # a worker before this one bumped past this move
            ul = wait_lock(ad, UPDATE_LOCK, lock_timeout_s, poll_s)
            if ul is None:
                log(f"gave up after {lock_timeout_s:g} s: {busy(ad)}")
                return 1
            try:
                if checkout.is_dir():
                    os.chdir(checkout)      # `project.resolve_fw` follows the checkout HEAD moved in
                for t in targets:
                    log(f"bump start: {t} in {checkout}")
                    rc = update.run(ad, t, "HEAD", log=log, locked=True, via="hook")
                    log(f"bump end: {t} exit {rc}")
                project.status(ad)          # current.json and the MAP's stale line, whatever the outcome
                (bump_dir(ad) / HANDLED).write_text(f"{seen}\n", encoding="utf-8")
            finally:
                release(ul)
            pidf.unlink(missing_ok=True)    # from here a hook starts its own worker...
            if _moved_ns(ad) <= seen:       # ...so a move recorded before this read is ours to bump
                return 0
            pidf.write_text(f"{os.getpid()}\n", encoding="utf-8")
    finally:
        pidf.unlink(missing_ok=True)
        release(wl)


# ---------------------------------------------------------------- CLI

def _cmd_install(args) -> int:
    ad = common.atlas_dir(args.atlas_dir)
    from .project import load_config
    known = {t.get("name") for t in load_config(ad).get("targets") or []}
    unknown = [t for t in args.targets if t not in known]
    if unknown:
        print(f"hook install: no [[targets]] named {', '.join(unknown)} in {ad / 'atlas.toml'}", file=sys.stderr)
        return 2
    for line in install(ad, args.targets, args.debounce):
        print(line)
    print(f"bumps {', '.join(args.targets)} after HEAD has been quiet {args.debounce:g} s; log {bump_dir(ad) / LOG}; "
          "`embarch-atlas hook uninstall` removes the hooks")
    return 0


def _cmd_uninstall(args) -> int:
    ad = common.atlas_dir(args.atlas_dir)
    done = uninstall(ad)
    for line in done or ["no embarch-atlas hook installed"]:
        print(line)
    return 0


def _cmd_run(args) -> int:
    return run_worker(common.atlas_dir(args.atlas_dir), args.targets, args.debounce)


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "hook", "bump the atlas when HEAD moves (git hooks, opt-in)")
    p = g.add_parser("install", help="write post-commit/-checkout/-merge/-rewrite hooks that bump the targets "
                                     "after HEAD has been quiet a while")
    p.add_argument("targets", nargs="+", metavar="target")
    p.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE_S,
                   help=f"seconds HEAD must be quiet before a bump (default {DEFAULT_DEBOUNCE_S})")
    p.set_defaults(func=_cmd_install)
    p = g.add_parser("uninstall", help="remove the bump block from the hooks")
    p.set_defaults(func=_cmd_uninstall)
    p = g.add_parser("run", help="what a hook starts, detached: wait for HEAD to be quiet, then `update` each target "
                                 "under the bump lock (log: bump/log)")
    p.add_argument("targets", nargs="+", metavar="target")
    p.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE_S)
    p.set_defaults(func=_cmd_run)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("run as `embarch-atlas hook`")
