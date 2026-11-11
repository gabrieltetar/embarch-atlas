"""`code` and `states`: symbols and Zephyr SMF state tables in the sources one build compiled.

The source set is the build's own: every translation unit in its `compile_commands.json`, plus the
headers each one included, read from the build's `.ninja_deps` (the compiler's dependency output,
exact). When the build dir has no `.ninja_deps` (a pinned atlas whose build dir is gone), the headers
are found by following `#include` lines through the unit's `-I`/`-isystem` paths, every branch of
every `#if` taken, so the set is a superset. A variant build, a scratch Zephyr worktree with west
patches applied, or a version bump therefore answers for its own tree, never for the workspace's.

Extraction is a deterministic lexer, not a compiler: comments and string contents are blanked,
preprocessor lines are read for `#define` and for the `#if` regions, and the top-level braces and
semicolons give functions, structs, unions, enums and their enumerators, typedefs, file-scope
variables (with or without an initializer), prototypes and `*_DEFINE(name, ...)` macro objects.
`#if` branches are all parsed (a branch that opens a brace the other does not is followed through
its first branch, as ctags does); each definition keeps the `#if` conditions above it, and a query
drops the ones whose `CONFIG_*` condition the build's `.config` makes false. A condition naming
anything else (a devicetree macro, a toolchain define) is kept as possibly compiled.

Neither libclang nor a tags program ships with the Zephyr SDK, so the lexer is the only extractor.

Caching: each file's extraction is stored under `cache/code/f/<content sha16>.json`, so two builds of
one tree share it; the build's file list, the header-to-unit map and the extractions are stored
together under `cache/code/<key>.json`, keyed by the `compile_commands.json` and `.ninja_deps`
bytes, and revalidated per file by size and mtime on every load.

A file whose mtime is newer than the build (its `zephyr.elf`, else the atlas stamp's `created`) has
changed since it was compiled: what it shows is the file now, and the answer says so.
"""
from __future__ import annotations

import bisect
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

from . import kconfig as KC

INDEX_VERSION = 1
MAX_LIST = 60
DOC_LINES = 40          # longest comment block shown above a definition
WINDOW = 15             # lines either side of a `file:line` that is in no definition

KW = {"if", "for", "while", "switch", "return", "sizeof", "do", "else", "case", "goto", "typeof",
      "__typeof__", "_Static_assert", "static_assert", "defined"}
ATTR = {"__attribute__", "__attribute", "__aligned", "__section", "__in_section", "__used", "__unused",
        "__deprecated", "__weak", "__packed", "__noinit", "__printf_like", "__syscall", "__pinned_func",
        "__boot_func", "__ramfunc", "Z_GENERIC_SECTION", "__isr", "__asm__", "asm", "__asm", "ALWAYS_INLINE",
        "FUNC_NORETURN", "__maybe_unused", "__no_optimization", "__alloc_size", "__must_check", "__fallthrough"}
QUAL = {"static", "const", "volatile", "extern", "inline", "__inline", "__inline__", "register", "unsigned",
        "signed", "struct", "union", "enum", "restrict", "__restrict", "_Noreturn", "auto"}
IDENT = re.compile(r"[A-Za-z_]\w*")
_LEX = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
_DIRECTIVE = re.compile(r"^[ \t]*#[ \t]*(\w*)(.*)$")
_EVENTS = re.compile(r"[{};\x01\x02\x03]")
_DEFINE_OBJ = re.compile(r"^\w*_(DEFINE|DECLARE)\w*$")
_NO_SENTINEL = str.maketrans("\x01\x02\x03", "   ")


# ---------------------------------------------------------------- lexing one file

def blank(text: str) -> str:
    """Comments to spaces, string and character literal contents to spaces; quotes, newlines and
    every offset are kept, so a line number found in the result is the file's."""
    def sub(m: re.Match) -> str:
        s = m.group(0)
        if s[0] in "\"'":
            return s[0] + " " * (len(s) - 2) + s[-1]
        if "\n" not in s:
            return " " * len(s)
        return "".join("\n" if c == "\n" else " " for c in s)
    return _LEX.sub(sub, text)


def _group(text: str, i: int, open_: str = "(", close: str = ")") -> int:
    """Index just past the bracket matching the one at text[i]."""
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def split_args(text: str) -> list[str]:
    """Top-level comma split of an argument list (no outer parentheses)."""
    out, depth, cur = [], 0, []
    for c in text:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    last = "".join(cur).strip()
    if last or out:
        out.append(last)
    return out


def _paren_groups(head: str) -> list[tuple[str, int, int]]:
    """(identifier before it, start, end) for each top-level parenthesised group of a head."""
    out, i, n = [], 0, len(head)
    while i < n:
        c = head[i]
        if c == "(":
            j = _group(head, i)
            m = re.search(r"([A-Za-z_]\w*)\s*$", head[:i])
            out.append((m.group(1) if m else "", i, j))
            i = j
        elif c == "[":
            i = _group(head, i, "[", "]")
        else:
            i += 1
    return out


def _strip_brackets(s: str) -> str:
    return re.sub(r"\[[^\]]*\]", " ", s)


def _decl_name(s: str) -> str | None:
    """The declared name in `type name[...]`, `type *name`, `type (*name)(args)`."""
    m = re.search(r"\(\s*\*+\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*\s*\)", s)
    if m:
        return m.group(1)
    ids = [t for t in IDENT.findall(_strip_brackets(s)) if t not in ATTR]
    return ids[-1] if ids else None


class _Lines:
    def __init__(self, text: str):
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, off: int) -> int:
        return bisect.bisect_right(self.starts, off)


def _cond_of(name: str, rest: str) -> str:
    rest = rest.strip()
    if name == "ifdef":
        return f"defined({rest.split()[0] if rest else ''})"
    if name == "ifndef":
        return f"!defined({rest.split()[0] if rest else ''})"
    return rest


def _cplusplus(cond: str) -> bool | None:
    """True/False when a condition only asks whether this is C++ (never, here), else None."""
    c = re.sub(r"\s+", "", cond)
    if c in ("defined(__cplusplus)", "__cplusplus", "defined__cplusplus"):
        return False
    if c in ("!defined(__cplusplus)", "!__cplusplus", "!defined__cplusplus"):
        return True
    return None


def extract(text: str) -> dict:
    """Every definition in one C file, with the `#if` regions, SMF tables and SMF state setters.

    `d`: [name, kind, line, end, head line, first doc-comment line, extra] where `extra` is the enum of an
    enumerator, the macro of a `*_DEFINE` object, `(` for a function-like macro, the params of a function
    that calls smf_set_state. `c`: [start, end, [condition, ...]] for each `#if` branch (all must hold).
    `smf`: [table, line, [[state, line, entry, run, exit, parent, initial], ...]].
    `w`: {function: [table, parameter index]} for a function that sets a state from its parameter.
    """
    raw_lines = text.split("\n")
    b = blank(text)
    lines = b.split("\n")
    defs: list[list] = []
    conds: list[list] = []
    cstack: list[dict] = []          # one frame per open #if: branch start, exprs so far, skip flags
    out_lines: list[str] = []
    i = 0
    n = len(lines)

    def skipping() -> bool:
        return any(f["skip"] for f in cstack)

    while i < n:
        line = lines[i]
        m = _DIRECTIVE.match(line) if "#" in line else None
        if not m:
            out_lines.append("" if skipping() else line)
            i += 1
            continue
        j = i                           # continuation lines belong to the directive
        while j < n - 1 and lines[j].rstrip().endswith("\\"):
            j += 1
        name, rest = m.group(1), " ".join([m.group(2)] + [lines[k] for k in range(i + 1, j + 1)]).replace("\\", " ")
        sentinel = ""
        if name in ("if", "ifdef", "ifndef"):
            cond = _cond_of(name, rest)
            cpp = _cplusplus(cond)
            skip = cpp is False or re.fullmatch(r"\s*0\s*", cond) is not None
            cstack.append({"start": j + 2, "exprs": [cond], "skip": skip, "cpp": cpp, "zero": skip and cpp is None})
            sentinel = "\x01"
        elif name in ("elif", "else") and cstack:
            f = cstack[-1]
            conds.append([f["start"], i, _branch(f["exprs"])])
            if name == "elif":
                f["exprs"].append(rest.strip())
                f["skip"] = False if not f["zero"] else re.fullmatch(r"\s*0\s*", rest) is not None
            else:
                f["exprs"].append(None)
                f["skip"] = f["cpp"] is True
            f["start"] = j + 2
            sentinel = "\x02"
        elif name == "endif" and cstack:
            f = cstack.pop()
            conds.append([f["start"], i, _branch(f["exprs"])])
            sentinel = "\x03"
        elif name == "define" and not skipping():
            dm = re.match(r"\s*([A-Za-z_]\w*)(\()?", rest)
            if dm:
                defs.append([dm.group(1), "macro", i + 1, j + 1, i + 1, _doc_start(raw_lines, i), "(" if dm.group(2) else None])
        out_lines.append(sentinel)
        out_lines.extend([""] * (j - i))
        i = j + 1
    for f in cstack:                    # an unterminated #if: its region runs to the end
        conds.append([f["start"], n, _branch(f["exprs"])])
    conds = [c for c in conds if c[2] and c[0] <= c[1]]
    code = "\n".join(out_lines)
    res = _top_level(code, raw_lines, defs)
    inited = {d[0] for d in defs if d[1] == "variable" and d[6] == "="}
    for d in defs:                      # `static T x[N];` ahead of `static T x[N] = {...}` declares it
        if d[1] == "variable" and d[6] != "=" and d[0] in inited:
            d[1] = "declaration"
    return {"v": INDEX_VERSION, "d": defs, "c": conds, **res}


def _branch(exprs: list) -> list[str]:
    """The conditions the current branch needs: every earlier branch false, this one true."""
    *earlier, last = exprs
    out = [f"!({e})" for e in earlier if e is not None]
    if last is not None:
        out.append(last)
    return out


def _doc_start(raw: list[str], i: int) -> int:
    """First line (1-based) of the comment block that ends right above 0-based line i, else i+1."""
    k = i - 1
    start = i
    in_block = False
    while k >= 0 and i - k <= DOC_LINES:
        s = raw[k].strip()
        if in_block:
            start = k
            if s.startswith("/*"):
                in_block = False
            k -= 1
            continue
        if s.endswith("*/"):
            start = k
            in_block = not s.startswith("/*") or s == "*/"
            if s.startswith("/*") and s.endswith("*/") and len(s) > 3:
                in_block = False
            k -= 1
            continue
        if s.startswith("//"):
            start = k
            k -= 1
            continue
        break
    return start + 1


def _top_level(code: str, raw: list[str], defs: list[list]) -> dict:
    L = _Lines(code)
    stack: list[list] = []
    cst: list[dict] = []
    head_start = 0
    pending: list | None = None         # a closed struct/enum whose declaration runs to the `;`
    smf: list = []
    wrappers: dict = {}

    for m in _EVENTS.finditer(code):
        c, off = m.group(), m.start()
        if c == "\x01":
            cst.append({"at": list(stack), "first": None})
            continue
        if c == "\x02":
            if cst:
                f = cst[-1]
                if f["first"] is None:
                    f["first"] = list(stack)
                stack[:] = f["at"]
            continue
        if c == "\x03":
            if cst:
                f = cst.pop()
                if f["first"] is not None:
                    stack[:] = f["first"]
            continue
        if c == "{":
            if not stack:
                head = code[head_start:off].translate(_NO_SENTINEL)
                fr = _classify_head(head, head_start, L, raw)
                fr.append(off)
                stack.append(fr)
                pending = None
            else:
                stack.append(["block", None, 0, 0, 0, None, off])
            continue
        if c == "}":
            if not stack:
                head_start = off + 1
                continue
            fr = stack.pop()
            if stack:
                continue
            kind, name, line, hline, doc, extra, open_off = fr
            end = L.line(off)
            if kind in ("struct", "union", "enum", "typedef-agg", "variable", "function", "macro-object"):
                if name:
                    defs.append([name, kind if kind != "typedef-agg" else extra["agg"], line, end, hline, doc,
                                 extra if kind == "macro-object" else ("=" if kind == "variable" else None)])
                if kind == "function":
                    _smf_setter(code, open_off, off, name, extra, wrappers)
                if kind in ("enum",) or (kind == "typedef-agg" and extra["agg"] == "enum"):
                    _enumerators(code, open_off, off, L, raw, name or "", defs)
                if kind == "variable" and "SMF_CREATE_STATE" in code[open_off:off]:
                    smf.append([name, line, _smf_entries(code, open_off, off, L)])
                if kind in ("struct", "union", "enum", "typedef-agg"):
                    pending = [kind, name, hline, doc, extra, end]
            head_start = off + 1
            continue
        if c == ";" and not stack:
            stmt = code[head_start:off].translate(_NO_SENTINEL)
            if pending is not None:
                _after_aggregate(stmt, pending, L, head_start, off, defs)
            else:
                _statement(stmt, head_start, off, L, raw, defs, smf)
            pending = None
            head_start = off + 1
    return {"smf": smf, "w": wrappers}


def _head_line(head: str, head_start: int, L: _Lines) -> int:
    lead = len(head) - len(head.lstrip())
    return L.line(head_start + lead)


def _classify_head(head: str, head_start: int, L: _Lines, raw: list[str]) -> list:
    """[kind, name, name line, head line, doc line, extra] for a top-level `{`."""
    h = head.strip()
    hline = _head_line(head, head_start, L)
    doc = _doc_start(raw, hline - 1)
    if not h:
        return ["block", None, hline, hline, doc, None]

    def at(sub_off: int) -> int:
        return L.line(head_start + (len(head) - len(head.lstrip())) + sub_off)

    typedef = h.startswith("typedef")
    am = re.search(r"\b(struct|union|enum)\s*([A-Za-z_]\w*)?\s*(?:__\w+(?:\(\([^)]*\)\))?\s*)*$", h)
    if am and "=" not in h:
        name = am.group(2)
        line = at(am.start(2)) if name else hline
        if typedef:
            return ["typedef-agg", name, line, hline, doc, {"agg": am.group(1), "tag": name}]
        return [am.group(1), name, line, hline, doc, None]
    eq = _top_eq(h)
    if eq >= 0:
        left = h[:eq]
        name = _decl_name(left)
        nm = list(re.finditer(rf"\b{re.escape(name)}\b", left)) if name else []
        return ["variable", name, at(nm[-1].start()) if nm else hline, hline, doc, None]
    groups = [g for g in _paren_groups(h) if g[0] and g[0] not in ATTR and g[0] not in KW]
    if groups:
        name, gs, ge = groups[0]
        before = [t for t in IDENT.findall(h[:gs]) if t not in ATTR]
        if len(before) == 1 and name.isupper() and not typedef:
            args = split_args(h[gs + 1:ge - 1])
            arg = args[0] if args and IDENT.fullmatch(args[0] or "") else None
            return ["macro-object", arg, at(gs), hline, doc, name] if arg else ["block", None, hline, hline, doc, None]
        params = [_decl_name(p) for p in split_args(h[gs + 1:ge - 1])]
        nm = re.search(rf"\b{re.escape(name)}\s*$", h[:gs])
        return ["function", name, at(nm.start()) if nm else hline, hline, doc, [p or "" for p in params]]
    return ["block", None, hline, hline, doc, None]


def _top_eq(h: str) -> int:
    depth = 0
    for i, c in enumerate(h):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "=" and depth == 0 and h[i + 1:i + 2] != "=" and h[i - 1:i] not in "=!<>":
            return i
    return -1


def _after_aggregate(stmt: str, pending: list, L: _Lines, start: int, end: int, defs: list) -> None:
    """`} name;` after a typedef'd aggregate, or `} var;` after a plain one."""
    kind, tag, hline, doc, extra, close_line = pending
    names = [_decl_name(p) for p in split_args(stmt) if p.strip()]
    for nm_ in names:
        if not nm_:
            continue
        m = re.search(rf"\b{re.escape(nm_)}\b", stmt)
        line = L.line(start + m.start()) if m else close_line
        if kind == "typedef-agg":
            defs.append([nm_, "typedef", line, L.line(end), hline, doc, extra["agg"] + (f" {tag}" if tag else "")])
        else:
            defs.append([nm_, "variable", line, L.line(end), hline, doc, None])


def _statement(stmt: str, start: int, end: int, L: _Lines, raw: list[str], defs: list, smf: list) -> None:
    s = stmt.strip()
    if not s:
        return
    lead = len(stmt) - len(stmt.lstrip())
    hline = L.line(start + lead)

    def line_of(name: str) -> int:
        ms = list(re.finditer(rf"\b{re.escape(name)}\b", stmt))
        return L.line(start + ms[0].start()) if ms else hline

    doc = _doc_start(raw, hline - 1)
    eline = L.line(end)
    if s.startswith("typedef"):
        name = _decl_name(s[len("typedef"):].split("(", 1)[0]) if "(*" not in s and "( *" not in s else _decl_name(s)
        groups = _paren_groups(s)
        if groups and "(*" not in re.sub(r"\s+", "", s):     # typedef int fn_t(int);
            name = groups[0][0] or name
        if name:
            defs.append([name, "typedef", line_of(name), eline, hline, doc, None])
        return
    if re.fullmatch(r"(struct|union|enum)\s+\w+", s):
        return                                                  # a forward declaration of a tag
    eq = _top_eq(s)
    left = s if eq < 0 else s[:eq]
    groups = [g for g in _paren_groups(left) if g[0] not in ATTR and g[0] not in KW]
    if groups and groups[0][0]:
        name, gs, ge = groups[0]
        before = [t for t in IDENT.findall(left[:gs]) if t not in ATTR]
        if len(before) == 1:                                   # a macro invocation at file scope
            if _DEFINE_OBJ.match(name):
                args = split_args(left[gs + 1:ge - 1])
                if args and IDENT.fullmatch(args[0] or ""):
                    defs.append([args[0], "macro-object", line_of(args[0]), eline, hline, doc, name])
            return
        if eq < 0 and "(*" not in re.sub(r"\s+", "", left[:ge]):
            defs.append([name, "declaration", line_of(name), eline, hline, doc, None])
            return
    name = _decl_name(left)
    if not name or name in QUAL:
        return
    ids = [t for t in IDENT.findall(_strip_brackets(left)) if t not in ATTR]
    if len(ids) < 2:
        return
    kind = "declaration" if ids[0] == "extern" else "variable"
    defs.append([name, kind, line_of(name), eline, hline, doc, "=" if eq >= 0 and kind == "variable" else None])
    if eq >= 0 and "SMF_CREATE_STATE" in s:
        m = re.search(r"SMF_CREATE_STATE\s*\(", stmt)
        if m:
            g = _group(stmt, m.end() - 1)
            smf.append([name, line_of(name), [[name, L.line(start + m.start()),
                                              *_smf_args(stmt[m.end():g - 1])]]])


def _enumerators(code: str, open_off: int, close_off: int, L: _Lines, raw: list[str], enum: str, defs: list) -> None:
    body = code[open_off + 1:close_off]
    pos = 0
    for item in split_args(body):
        idx = body.find(item, pos) if item else -1
        if idx >= 0:
            pos = idx + len(item)
        m = re.match(r"\s*([A-Za-z_]\w*)", item or "")
        if not m or idx < 0:
            continue
        line = L.line(open_off + 1 + idx + m.start(1))
        defs.append([m.group(1), "enumerator", line, line, line, _doc_start(raw, line - 1), enum or None])


# ---------------------------------------------------------------- SMF

def _smf_ref(a: str) -> str | None:
    a = a.strip()
    if not a or a in ("NULL", "0", "((void *)0)"):
        return None
    m = re.fullmatch(r"&\s*\(?\s*(\w+)\s*\[\s*(\w+)\s*\]\s*\)?", a)
    if m:
        return m.group(2)
    m = re.fullmatch(r"&\s*(\w+)", a)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\(?\s*(\w+)\s*\)?", a)
    return m.group(1) if m else a


def _smf_args(argtext: str) -> list:
    args = split_args(argtext)
    args += [""] * (5 - len(args))
    entry, run, exit_, parent, initial = (_smf_ref(x) for x in args[:5])
    return [entry, run, exit_, parent, initial]


def _smf_entries(code: str, open_off: int, close_off: int, L: _Lines) -> list:
    out = []
    body = code[open_off + 1:close_off]
    for i, m in enumerate(re.finditer(r"(?:\[\s*(\w+)\s*\]\s*=\s*)?SMF_CREATE_STATE\s*\(", body)):
        g = _group(body, m.end() - 1)
        state = m.group(1) or str(i)
        out.append([state, L.line(open_off + 1 + m.start()), *_smf_args(body[m.end():g - 1])])
    return out


_SET = re.compile(r"\bsmf_set_state\s*\(")


def _smf_setter(code: str, open_off: int, close_off: int, name: str, params: Any, wrappers: dict) -> None:
    """Record a function that moves a table to the state its parameter names (pe_set_state(dev, s))."""
    body = code[open_off:close_off]
    if "smf_set_state" not in body or not isinstance(params, list):
        return
    for m in _SET.finditer(body):
        args = split_args(body[m.end():_group(body, m.end() - 1) - 1])
        if len(args) < 2:
            continue
        t = re.fullmatch(r"&\s*\(?\s*(\w+)\s*\[\s*(\w+)\s*\]\s*\)?", args[1].strip())
        if t and t.group(2) in params:
            wrappers[name] = [t.group(1), params.index(t.group(2))]
            return


def calls_in(body: str) -> list[tuple[int, str, list[str]]]:
    """(offset, callee, args) for every call in a blanked function body."""
    out = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
        if m.group(1) in KW:
            continue
        g = _group(body, m.end() - 1)
        out.append((m.start(), m.group(1), split_args(body[m.end():g - 1])))
    return out


# ---------------------------------------------------------------- the source set

def read_ninja_deps(path: Path) -> dict[str, list[str]] | None:
    """{output: [inputs]} from a `.ninja_deps` log (format 3 or 4; later records win), None if unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    sig = b"# ninjadeps\n"
    if not data.startswith(sig) or len(data) < len(sig) + 4:
        return None
    version = struct.unpack_from("<i", data, len(sig))[0]
    if version not in (3, 4):
        return None
    mt = 8 if version == 4 else 4
    paths: list[str] = []
    deps: dict[int, list[int]] = {}
    i = len(sig) + 4
    while i + 4 <= len(data):
        head = struct.unpack_from("<I", data, i)[0]
        i += 4
        size = head & 0x7FFFFFFF
        rec = data[i:i + size]
        i += size
        if len(rec) < size:
            break
        if head & 0x80000000:
            out = struct.unpack_from("<i", rec, 0)[0]
            ids = struct.unpack_from(f"<{(size - 4 - mt) // 4}i", rec, 4 + mt)
            deps[out] = list(ids)
        else:
            paths.append(rec[:-4].rstrip(b"\0").decode("utf-8", "replace"))
    return {paths[o]: [paths[k] for k in ins if 0 <= k < len(paths)] for o, ins in deps.items() if 0 <= o < len(paths)}


def _flags(entry: dict) -> list[str]:
    if "arguments" in entry:
        return list(entry["arguments"])
    try:
        return shlex.split(entry.get("command", ""))
    except ValueError:
        return entry.get("command", "").split()


_INC = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]', re.M)


def include_scan(entry: dict, cache: dict[str, list[tuple[str, str]]]) -> list[str]:
    """Headers a unit includes, following `#include` through its -I/-isystem/-iquote paths, every
    `#if` branch taken (a superset of what the compiler read)."""
    args = _flags(entry)
    d = Path(entry.get("directory", "."))
    inc, quote, forced = [], [], []
    k = 0
    while k < len(args):
        a = args[k]
        for flag, dst in (("-I", inc), ("-isystem", inc), ("-iquote", quote), ("-include", forced),
                          ("-imacros", forced)):
            if a == flag and k + 1 < len(args):
                dst.append(args[k + 1])
                k += 1
                break
            if a.startswith(flag) and len(a) > len(flag) and flag in ("-I",):
                dst.append(a[len(flag):])
                break
        k += 1
    inc = [str((d / p).resolve()) if not os.path.isabs(p) else os.path.normpath(p) for p in inc]
    quote = [str((d / p).resolve()) if not os.path.isabs(p) else os.path.normpath(p) for p in quote]
    tu = str((d / entry["file"]).resolve()) if not os.path.isabs(entry["file"]) else os.path.normpath(entry["file"])
    seen: dict[str, None] = {}
    todo = [os.path.normpath(f if os.path.isabs(f) else str(d / f)) for f in forced] + [tu]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen[f] = None
        if f not in cache:
            try:
                text = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            cache[f] = [(m.group(1), m.group(2)) for m in _INC.finditer(text)]
        for kind, name in cache[f]:
            dirs = ([os.path.dirname(f)] + quote if kind == '"' else []) + inc
            for base in dirs:
                p = os.path.normpath(os.path.join(base, name))
                if os.path.isfile(p):
                    todo.append(p)
                    break
    seen.pop(tu, None)
    return list(seen)


def source_set(cc_path: Path, bdir: Path | None) -> tuple[list[str], int, dict[str, list[int]], str]:
    """(files with the units first, how many are units, {header: [unit index]}, how the headers were found)."""
    entries = json.loads(cc_path.read_text(encoding="utf-8"))
    units: list[str] = []
    outs: list[str | None] = []
    for e in entries:
        f = e.get("file", "")
        d = Path(e.get("directory", "."))
        p = os.path.normpath(f if os.path.isabs(f) else str(d / f))
        if p in units:
            continue
        units.append(p)
        o = e.get("output")
        outs.append(os.path.realpath(o if os.path.isabs(o) else str(d / o)) if o else None)
    deps = read_ninja_deps(bdir / ".ninja_deps") if bdir else None
    includes: dict[str, list[int]] = {}
    if deps:
        real = bdir.resolve()           # the atlas may reach its build dir through a link
        by_out = {os.path.realpath(o if os.path.isabs(o) else str(real / o)): ins for o, ins in deps.items()}
        how = "ninja deps"
        for ui, (u, o) in enumerate(zip(units, outs)):
            ins = by_out.get(o or "", [])
            for h in ins:
                h = os.path.normpath(h if os.path.isabs(h) else str(real / h))
                if h != u:
                    includes.setdefault(h, []).append(ui)
        if not includes:
            deps = None
    if not deps:
        how = "#include scan (no .ninja_deps)"
        cache: dict = {}
        for ui, e in enumerate(entries):
            f = e.get("file", "")
            d = Path(e.get("directory", "."))
            p = os.path.normpath(f if os.path.isabs(f) else str(d / f))
            if p not in units:
                continue
            for h in include_scan(e, cache):
                includes.setdefault(h, []).append(units.index(p))
    unit_set = set(units)
    headers = sorted(h for h in includes if h not in unit_set)
    return units + headers, len(units), includes, how


# ---------------------------------------------------------------- the build's index

_MEMO: dict[str, tuple[Any, "Index"]] = {}


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stat(p: str) -> list[int] | None:
    try:
        s = os.stat(p)
    except OSError:
        return None
    return [s.st_size, s.st_mtime_ns]


def _file_extract(path: str, cache: Path) -> tuple[str, dict]:
    data = Path(path).read_bytes()
    key = _sha16(data)
    cf = cache / "f" / f"{key}.json"
    got = _load(cf)
    if isinstance(got, dict) and got.get("v") == INDEX_VERSION:
        return key, got
    ex = extract(data.decode("utf-8", errors="replace"))
    _dump(cf, ex)
    return key, ex


class Index:
    """One build's source set and every definition in it."""

    def __init__(self, files: list[str], units: int, includes: dict[str, list[int]], ex: list[dict],
                 how: str, rel, built_at: float | None, config: dict[str, str]):
        self.files, self.units, self.includes, self.ex, self.how = files, units, includes, ex, how
        self.rel_fn = rel
        self.by_name: dict[str, list[tuple[int, int]]] = {}
        for fi, e in enumerate(ex):
            for di, d in enumerate(e.get("d") or []):
                self.by_name.setdefault(d[0], []).append((fi, di))
        self._rel: dict[int, str] = {}
        self.refresh(built_at, config)

    def refresh(self, built_at: float | None, config: dict[str, str]) -> None:
        """Re-read what can move between two queries on an unchanged source set: the build's time
        and .config."""
        self.built_at, self.config = built_at, config
        self._inactive: dict[int, list[tuple[int, int, str]]] = {}
        self.changed = {fi for fi, f in enumerate(self.files)
                        if built_at and (_stat(f) or [0, 0])[1] / 1e9 > built_at}

    def rel(self, fi: int) -> str:
        if fi not in self._rel:
            self._rel[fi] = self.rel_fn(self.files[fi])
        return self._rel[fi]

    def is_unit(self, fi: int) -> bool:
        return fi < self.units

    def inactive(self, fi: int) -> list[tuple[int, int, str]]:
        """(start, end, the false condition) of the `#if` regions this build's .config turns off."""
        if fi not in self._inactive:
            out = []
            for start, end, exprs in self.ex[fi].get("c") or []:
                for e in exprs:
                    if cond_value(e, self.config) is False:
                        out.append((start, end, e))
                        break
            self._inactive[fi] = out
        return self._inactive[fi]

    def off_at(self, fi: int, line: int) -> str | None:
        for start, end, e in self.inactive(fi):
            if start <= line <= end:
                return e
        return None

    def defs(self, name: str) -> list[tuple[int, list]]:
        return [(fi, self.ex[fi]["d"][di]) for fi, di in self.by_name.get(name, [])]

    def text(self, fi: int) -> str:
        try:
            return Path(self.files[fi]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def unit_note(self, fi: int) -> str:
        if self.is_unit(fi):
            return "a compiled unit"
        tus = self.includes.get(self.files[fi]) or []
        names = ", ".join(Path(self.files[t]).name for t in tus[:3])
        return f"included by {len(tus)} compiled unit{'s' if len(tus) != 1 else ''}" + (f" ({names}{', ...' if len(tus) > 3 else ''})" if tus else "")


def built_at(stamp_created: str | None, elf: Path | None) -> float | None:
    if elf and elf.is_file():
        return elf.stat().st_mtime
    if stamp_created:
        try:
            return datetime.fromisoformat(stamp_created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def index(cc_path: Path, bdir: Path | None, cache: Path, rel, when: float | None, config: dict[str, str]) -> Index:
    """The build's index, from `cache/code/` when its inputs have not changed."""
    cc_bytes = cc_path.read_bytes()
    dep = (bdir / ".ninja_deps") if bdir else None
    dep_stat = _stat(str(dep)) if dep else None
    memo_key = str(cc_path)
    stamp = (_sha16(cc_bytes), dep_stat)
    hit = _MEMO.get(memo_key)
    if hit and hit[0][0] == stamp:
        idx = hit[1]
        if all(_stat(f) == s for f, s in zip(idx.files, hit[0][1])):
            idx.refresh(when, config)
            return idx
    dep_bytes = dep.read_bytes() if dep_stat else b""
    key = _sha16(cc_bytes + b"\0" + dep_bytes + b"\0" + str(INDEX_VERSION).encode())
    bf = cache / f"{key}.json"
    got = _load(bf)
    if not (isinstance(got, dict) and got.get("v") == INDEX_VERSION):
        files, units, includes, how = source_set(cc_path, bdir)
        got = {"v": INDEX_VERSION, "files": files, "units": units, "includes": includes, "how": how,
               "stat": [None] * len(files), "keys": [None] * len(files), "ex": [None] * len(files)}
    dirty = False
    for fi, f in enumerate(got["files"]):
        st = _stat(f)
        if st is None:                  # gone since the build: it answers nothing
            if got["stat"][fi] is not None or got["ex"][fi] is None:
                got["ex"][fi], got["stat"][fi], got["keys"][fi] = {"v": INDEX_VERSION, "d": [], "c": [], "smf": [], "w": {}}, None, None
                dirty = True
            continue
        if st != got["stat"][fi] or got["ex"][fi] is None:
            k, ex = _file_extract(f, cache)
            got["stat"][fi], got["keys"][fi], got["ex"][fi] = st, k, ex
            dirty = True
    if dirty:
        _dump(bf, got)
    idx = Index(got["files"], got["units"], got["includes"], got["ex"], got["how"], rel, when, config)
    _MEMO[memo_key] = ((stamp, [got["stat"][i] for i in range(len(got["files"]))]), idx)
    return idx


# ---------------------------------------------------------------- #if conditions against .config

_COND_TOK = re.compile(r"\s*(defined\s*\(\s*\w+\s*\)|defined\s+\w+|IS_ENABLED\s*\(\s*\w+\s*\)|0x[0-9a-fA-F]+[uUlL]*|\d+[uUlL]*|"
                       r"[A-Za-z_]\w*|&&|\|\||==|!=|<=|>=|[!()<>+\-*/%])")


def cond_value(expr: str, cfg: dict[str, str]) -> bool | None:
    """An `#if` expression evaluated over the build's CONFIG_ values: True, False, or None when it names
    anything else (a devicetree macro, a toolchain define) and so cannot be decided here."""
    if not cfg:
        return None
    toks, pos, out = [], 0, []
    s = expr.strip()
    while pos < len(s):
        m = _COND_TOK.match(s, pos)
        if not m:
            return None
        toks.append(m.group(1))
        pos = m.end()
    for t in toks:
        if t.startswith("defined"):
            name = re.sub(r"defined\s*\(?\s*(\w+)\s*\)?", r"\1", t)
            if not name.startswith("CONFIG_"):
                return None
            v = cfg.get(name[7:])
            out.append("1" if v is not None and v != "n" else "0")
        elif t.startswith("IS_ENABLED"):
            name = re.sub(r"IS_ENABLED\s*\(\s*(\w+)\s*\)", r"\1", t)
            if not name.startswith("CONFIG_"):
                return None
            out.append("1" if cfg.get(name[7:]) in ("y", "m") else "0")
        elif re.fullmatch(r"[A-Za-z_]\w*", t):
            if not t.startswith("CONFIG_"):
                return None
            v = cfg.get(t[7:])
            if v is None or v == "n":
                out.append("0")
            elif v in ("y", "m"):
                out.append("1")
            elif re.fullmatch(r"-?\d+|0x[0-9a-fA-F]+", v):
                out.append(str(int(v, 0)))
            else:
                return None
        elif t == "&&":
            out.append(" and ")
        elif t == "||":
            out.append(" or ")
        elif t == "!":
            out.append(" not ")
        elif re.fullmatch(r"0x[0-9a-fA-F]+[uUlL]*|\d+[uUlL]*", t):
            out.append(str(int(t.rstrip("uUlL"), 0)))
        elif t == "/":
            out.append("//")
        else:
            out.append(t)
    try:
        return bool(eval("".join(out), {"__builtins__": {}}, {}))   # noqa: S307  (digits and operators only)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- `code`

def _num(lines: list[str], a: int, b: int) -> str:
    w = len(str(b))
    return "\n".join(f"{k:>{w}} {lines[k - 1]}" for k in range(a, min(b, len(lines)) + 1))


def _match_file(idx: Index, want: str) -> list[int]:
    w = want.strip().lstrip("./")
    if ":" in w and not w.split(":", 1)[0].count("/") and idx.files:      # a `zephyr:` tag
        tag, _, w2 = w.partition(":")
        hits = [fi for fi in range(len(idx.files)) if idx.rel(fi) == f"{tag}:{w2}" or idx.rel(fi).startswith(f"{tag}:") and idx.rel(fi).endswith("/" + w2)]
        if hits:
            return hits
    return [fi for fi, f in enumerate(idx.files) if f == w or f.endswith("/" + w) or idx.rel(fi).endswith(":" + w) or idx.rel(fi).endswith("/" + w)]


_KIND_ORDER = {"function": 0, "struct": 1, "union": 1, "enum": 1, "typedef": 2, "variable": 3, "macro-object": 3,
               "macro": 4, "enumerator": 5, "declaration": 9}


def _label(kind: str, d: list) -> str:
    extra = d[6]
    if kind == "enumerator" and extra:
        return f"enumerator of enum {extra}"
    if kind == "macro-object" and extra:
        return f"object defined by {extra}"
    if kind == "macro" and extra == "(":
        return "function-like macro"
    if kind == "typedef" and isinstance(extra, str):
        return f"typedef ({extra})"
    return kind


def _enclosing(idx: Index, fi: int, line: int) -> str:
    best = None
    for d in idx.ex[fi].get("d") or []:
        if d[1] in ("enumerator", "declaration"):
            continue
        if d[4] <= line <= d[3] and (best is None or d[3] - d[4] < best[3] - best[4]):
            best = d
    if best is None:
        return "(file scope)"
    if best[1] == "macro":
        return f"(macro {best[0]})"
    if best[1] in ("struct", "union", "enum"):
        return f"({best[1]} {best[0]})"
    return best[0]


def references(idx: Index, name: str, skip: set[tuple[int, int]], only: set[int] | None = None) -> tuple[list[str], int]:
    """`file:line function` for every use of `name` in the source set (or the files in `only`), definitions
    in `skip` left out, and how many uses sit in `#if` branches this build does not compile."""
    out: list[str] = []
    off = 0
    pat = re.compile(rf"\b{re.escape(name)}\b")
    needle = name.encode()
    for fi, f in enumerate(idx.files):
        if only is not None and fi not in only:
            continue
        try:
            data = Path(f).read_bytes()
        except OSError:
            continue
        if needle not in data:
            continue
        b = blank(data.decode("utf-8", errors="replace"))
        L = _Lines(b)
        seen_lines = set()
        for m in pat.finditer(b):
            line = L.line(m.start())
            if (fi, line) in skip or line in seen_lines:
                continue
            seen_lines.add(line)
            if idx.off_at(fi, line):
                off += 1
                continue
            out.append(f"{idx.rel(fi)}:{line} {_enclosing(idx, fi, line)}")
    return out, off


def _def_block(idx: Index, fi: int, d: list, lines_cache: dict) -> str:
    if fi not in lines_cache:
        lines_cache[fi] = idx.text(fi).split("\n")
    lines = lines_cache[fi]
    start = min(d[5], d[4])
    return _num(lines, start, d[3])


def _outline(idx: Index, fi: int) -> str:
    rows = []
    for d in sorted(idx.ex[fi].get("d") or [], key=lambda d: d[2]):
        if d[1] == "declaration":
            continue
        off = idx.off_at(fi, d[2])
        rows.append(f"{d[2]}-{d[3]} {_label(d[1], d)} {d[0]}" + (f"  (not compiled: {off})" if off else ""))
    head = f"{idx.rel(fi)}: {idx.unit_note(fi)}, {len(rows)} definitions"
    return head + "\n" + "\n".join(rows)


def code_answer(idx: Index, query: str, show: str = "all") -> str:
    q = query.strip()
    if show not in ("all", "def", "refs"):
        return f"show={show!r}: use all, def or refs"
    m = re.fullmatch(r"(.+?):(\d+)", q)
    if m:
        return _at_line(idx, m.group(1), int(m.group(2)))
    file_part, sym = None, q
    if ":" in q:
        a, _, b = q.rpartition(":")
        if IDENT.fullmatch(b):
            file_part, sym = a, b
    if file_part is None and not IDENT.fullmatch(q.replace("*", "x").replace("?", "x")):
        fis = _match_file(idx, q)
        if len(fis) == 1:
            return _outline(idx, fis[0])
        if fis:
            return f"{len(fis)} compiled files match {q!r}:\n" + "\n".join(idx.rel(f) for f in fis[:MAX_LIST])
        return f"no compiled file or symbol {q!r} in this build ({len(idx.files)} files)"
    if any(c in sym for c in "*?[") and file_part is None:
        names = sorted(n for n in idx.by_name if fnmatch.fnmatchcase(n, sym))
        rows = []
        for n in names[:MAX_LIST]:
            fi, d = sorted(idx.defs(n), key=lambda x: _KIND_ORDER.get(x[1][1], 8))[0]
            rows.append(f"{n}  {_label(d[1], d)}  {idx.rel(fi)}:{d[2]}")
        if len(names) > MAX_LIST:
            rows.append(f"({len(names) - MAX_LIST} more)")
        return "\n".join(rows) if rows else f"no symbol matches {sym!r}"
    found = idx.defs(sym)
    fis: set[int] | None = None
    elsewhere = ""
    if file_part is not None:
        fis = set(_match_file(idx, file_part))
        if not fis:
            return f"no compiled file matches {file_part!r}"
        here = [(fi, d) for fi, d in found if fi in fis]
        if not here and found and show != "def":      # uses in that file of a symbol defined elsewhere
            elsewhere = f"(not defined in {file_part}: its definitions, then its uses there)\n"
        else:
            found = here
    if not found:
        return _nearest(idx, sym, file_part)
    live = [(fi, d) for fi, d in found if not idx.off_at(fi, d[2])]
    off = [(fi, d) for fi, d in found if idx.off_at(fi, d[2])]
    full = [(fi, d) for fi, d in live if d[1] != "declaration"]
    decls = [(fi, d) for fi, d in live if d[1] == "declaration"]
    full.sort(key=lambda x: (_KIND_ORDER.get(x[1][1], 8), not idx.is_unit(x[0]), idx.rel(x[0]), x[1][2]))
    out = []
    shown = full or decls
    summary = [f"{sym}: {len(full)} definition{'s' if len(full) != 1 else ''}" + (f", {len(decls)} declaration{'s' if len(decls) != 1 else ''}" if decls else "")
               + (f" (+{len(off)} in #if branches this build does not compile)" if off else "")]
    for fi, d in full + decls:
        changed = "  CHANGED since the build: this is the file now" if fi in idx.changed else ""
        summary.append(f"{_label(d[1], d)} {idx.rel(fi)}:{d[2]}" + (f"-{d[3]}" if d[3] != d[2] else "")
                       + f"  ({idx.unit_note(fi)}){changed}")
    for fi, d in off:
        summary.append(f"not compiled: {_label(d[1], d)} {idx.rel(fi)}:{d[2]} (#if {idx.off_at(fi, d[2])})")
    out.append(elsewhere + "\n".join(summary))
    if show in ("all", "def") and not elsewhere:
        cache: dict = {}
        for fi, d in shown:
            out.append(f"{idx.rel(fi)}:{min(d[5], d[4])}-{d[3]}\n{_def_block(idx, fi, d, cache)}")
    if show in ("all", "refs"):
        skip = {(fi, d[2]) for fi, d in found}
        refs, roff = references(idx, sym, skip, fis)
        out.append(f"references ({len(refs)}" + (f"; +{roff} in #if branches not compiled" if roff else "") + "):\n"
                   + ("\n".join(refs) if refs else "(none in the compiled sources)"))
    return "\n\n".join(out)


def _nearest(idx: Index, sym: str, file_part: str | None) -> str:
    low = sym.lower()
    pool = [n for n in idx.by_name if low[:4] in n.lower()] or list(idx.by_name)
    close = difflib.get_close_matches(sym, pool[:20000], n=6, cutoff=0.7)
    where = f" in {file_part}" if file_part else ""
    return (f"{sym} is not defined{where} in this build's compiled sources ({len(idx.files)} files)"
            + (f". Nearest: {', '.join(close)}" if close else ""))


def _at_line(idx: Index, file_part: str, line: int) -> str:
    fis = _match_file(idx, file_part)
    if not fis:
        return f"no compiled file matches {file_part!r}"
    if len(fis) > 1:
        return f"{len(fis)} compiled files match {file_part!r}; name one:\n" + "\n".join(idx.rel(f) for f in fis[:MAX_LIST])
    fi = fis[0]
    lines = idx.text(fi).split("\n")
    best = None
    for d in idx.ex[fi].get("d") or []:
        if d[1] in ("enumerator", "declaration"):
            continue
        a = min(d[4], d[5])
        if a <= line <= d[3] and (best is None or d[3] - a < best[3] - min(best[4], best[5])):
            best = d
    changed = " (CHANGED since the build: this is the file now)" if fi in idx.changed else ""
    if best is None or best[3] - min(best[4], best[5]) > 400:
        a, b = max(1, line - WINDOW), min(len(lines), line + WINDOW)
        return f"{idx.rel(fi)}:{a}-{b} (in no definition){changed}\n{_num(lines, a, b)}"
    a = min(best[4], best[5])
    off = idx.off_at(fi, best[2])
    return (f"{_label(best[1], best)} {best[0]} {idx.rel(fi)}:{a}-{best[3]} holds line {line}{changed}"
            + (f" (not compiled: #if {off})" if off else "") + f"\n{_num(lines, a, best[3])}")


# ---------------------------------------------------------------- `states`

class _Tables:
    def __init__(self, idx: Index):
        self.idx = idx
        self.wrappers: dict[str, tuple[str, int]] = {}
        for e in idx.ex:
            for k, v in (e.get("w") or {}).items():
                self.wrappers[k] = (v[0], v[1])
        self._direct: dict[tuple[str, str], list[str]] = {}
        self._funcs: dict[str, tuple[int, list] | None] = {}
        self._calls: dict[tuple[int, int], list] = {}
        self._blank: dict[int, tuple[str, _Lines]] = {}

    def func(self, name: str) -> tuple[int, list] | None:
        if name not in self._funcs:
            c = [(fi, d) for fi, d in self.idx.defs(name) if d[1] == "function" and not self.idx.off_at(fi, d[2])]
            self._funcs[name] = c[0] if c else None
        return self._funcs[name]

    def calls(self, fi: int, d: list) -> list[tuple[int, str, list[str]]]:
        """(line, callee, args) of each call in a function, leaving out lines in `#if` branches this
        build does not compile."""
        key = (fi, d[2])
        if key not in self._calls:
            if fi not in self._blank:
                text = blank(self.idx.text(fi))
                self._blank[fi] = (text, _Lines(text))
            text, L = self._blank[fi]
            a = L.starts[d[4] - 1] if d[4] - 1 < len(L.starts) else 0
            b = L.starts[d[3]] if d[3] < len(L.starts) else len(text)
            out = []
            for off, callee, args in calls_in(text[a:b]):
                line = L.line(a + off)
                if not self.idx.off_at(fi, line):
                    out.append((line, callee, args))
            self._calls[key] = out
        return self._calls[key]

    def direct(self, table: str, fname: str) -> list[str]:
        """States `fname` moves `table` to by itself: smf_set_state(&table[X]) or a setter call."""
        key = (table, fname)
        if key in self._direct:
            return self._direct[key]
        self._direct[key] = []
        f = self.func(fname)
        out: list[str] = []
        if f:
            for line, callee, args in self.calls(*f):
                t = None
                if callee == "smf_set_state" and len(args) >= 2:
                    m = re.fullmatch(r"&\s*\(?\s*(\w+)\s*\[\s*(.+?)\s*\]\s*\)?", args[1])
                    if m and m.group(1) == table:
                        t = m.group(2)
                elif callee in self.wrappers and callee != fname and self.wrappers[callee][0] == table:
                    k = self.wrappers[callee][1]
                    t = args[k] if k < len(args) else None
                if t and t not in out:
                    out.append(t)
        self._direct[key] = out
        return out

    def moves(self, table: str, fname: str | None) -> tuple[list[str], list[tuple[str, list[str]]]]:
        """(direct targets, [(helper, its targets)]) for one entry/run/exit function: one call level."""
        if not fname:
            return [], []
        direct = self.direct(table, fname)
        via = []
        f = self.func(fname)
        if f:
            seen = set()
            for _, callee, _args in self.calls(*f):
                if callee in seen or callee == fname or callee in self.wrappers or callee == "smf_set_state":
                    continue
                seen.add(callee)
                if self.func(callee):
                    t = self.direct(table, callee)
                    if t:
                        via.append((callee, t))
        return direct, via


def _fn_cell(tb: _Tables, name: str | None) -> str:
    if not name:
        return "-"
    f = tb.func(name)
    if not f:
        return f"{name} (not found)"
    fi, d = f
    return f"{name} {Path(tb.idx.files[fi]).name}:{d[2]}"


def states_answer(idx: Index, query: str) -> str:
    q = query.strip().rstrip("/")
    tables = [(fi, t) for fi, e in enumerate(idx.ex) for t in e.get("smf") or [] if not idx.off_at(fi, t[1])]
    if not tables:
        return f"no SMF_CREATE_STATE table in this build's compiled sources ({len(idx.files)} files)"
    pick = [(fi, t) for fi, t in tables if t[0] == q]
    if not pick:
        tag, _, path = q.rpartition(":") if ":" in q else ("", "", q)
        def under(fi: int) -> bool:
            r = idx.rel(fi)
            rtag, _, rpath = r.partition(":")
            if tag and rtag != tag:
                return False
            return rpath == path or rpath.startswith(path + "/") or ("/" + rpath).find("/" + path + "/") >= 0 or idx.files[fi].startswith(path + "/")
        pick = [(fi, t) for fi, t in tables if under(fi)]
    if not pick:
        names = ", ".join(f"{t[0]} ({idx.rel(fi)})" for fi, t in tables[:MAX_LIST])
        return f"no SMF table or directory {query!r} among this build's tables: {names}"
    tb = _Tables(idx)
    out = []
    for fi, (table, line, entries) in pick:
        live = [e for e in entries if not idx.off_at(fi, e[1])]
        names = {e[0] for e in live}
        off = len(entries) - len(live)
        setters = sorted(w for w, (t, _) in tb.wrappers.items() if t == table)
        changed = "  CHANGED since the build: this is the file now" if fi in idx.changed else ""
        out.append(f"table {table} {idx.rel(fi)}:{line}  {len(live)} states"
                   + (f" (+{off} in #if branches this build does not compile)" if off else "")
                   + (f"; set through {', '.join(setters)}" if setters else "") + changed)
        for state, sline, entry, run, exit_, parent, initial in live:
            row = [f"{state}", f"parent {parent or '-'}"]
            if initial:
                row.append(f"initial {initial}")
            row += [f"entry {_fn_cell(tb, entry)}", f"run {_fn_cell(tb, run)}", f"exit {_fn_cell(tb, exit_)}"]
            line_ = " | ".join(row)
            for what, fn in (("run", run), ("entry", entry), ("exit", exit_)):
                direct, via = tb.moves(table, fn)
                if not direct and not via:
                    continue
                mark = lambda t: t if t in names else f"{t} (not in this build's table)"   # noqa: E731
                parts = [mark(t) for t in direct] + [f"{', '.join(mark(x) for x in t)} via {h}" for h, t in via]
                line_ += f" | {what} -> {'; '.join(parts)}"
            out.append(line_)
        out.append("")
    return "\n".join(out).rstrip("\n")


# ---------------------------------------------------------------- one build, as buildq names it

def for_source(src) -> Index:
    """The index of a `buildq.Source`: its compile_commands.json, .ninja_deps, .config and cache."""
    if not src.cc or not src.cc.is_file():
        raise FileNotFoundError(f"no compile_commands.json in this build ({src.cc}): `code` and `states` read the "
                                "sources it lists")
    cfg = KC.read_dotconfig(src.dotconfig) if src.dotconfig and src.dotconfig.is_file() else {}
    return index(src.cc, src.bdir if src.bdir and (src.bdir / ".ninja_deps").is_file() else None,
                 src.cache / "code", src.rel, src.built, cfg)


def src_line(src, idx: Index) -> str:
    """The `src:` text: the build, its Zephyr version, the source set and how its headers were found."""
    label = src.label if "zephyr " in src.label else f"{src.label} (zephyr {src.zephyr or '?'})"
    changed = f"; {len(idx.changed)} changed since the build" if idx.changed else ""
    return f"{label} sources, {idx.units} units + {len(idx.files) - idx.units} headers by {idx.how}{changed}"
