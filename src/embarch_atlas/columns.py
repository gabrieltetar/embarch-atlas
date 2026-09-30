"""`doc columns <doc_id> <page>`: one table page as a grid of rows and cells, in reading orientation.

A wide table set sideways on a portrait page (a pinout with one column per package, a register
summary) comes out of `pdftotext -layout` scrambled: every header is a vertical run and the cells
of one row are spread down the page. This verb reads the page's words and ruling lines with
PyMuPDF, turns them so the dominant text direction reads left to right, and cuts the table into
cells:

- **columns** at the vertical rules of the turned page (the table's own grid), else at the gaps
  between word columns;
- **rows** at the horizontal rules, else by word lines (so without rules a cell wrapped over two
  lines comes out as two rows);
- a cell's words joined in reading order.

It prints one line per row, `r<N>: <cell> | <cell> | ...`, with empty cells kept, so column k of
every row is the same printed column. The helper reads positions only; it never names a column or
decides what a cell means. The LLM reads the header rows it prints and writes the records, quoting
the text layer as always (the grid is for reading, not a quote source).
"""
from __future__ import annotations

import collections
import statistics
import sys
from pathlib import Path
from typing import Any

from . import common

# a ruling line: at least this long (points) and this close to axis-parallel
MIN_RULE = 8.0
AXIS_TOL = 1.0
# gaps between rule positions closer than this are one rule (double lines, cell borders)
MERGE = 2.0


def reading_frame(lines: list[dict[str, Any]]) -> tuple[float, float]:
    """The dominant text direction (ux, uy) of a page, weighted by characters."""
    w: collections.Counter = collections.Counter()
    for ln in lines:
        d = tuple(round(x) for x in ln["dir"])
        w[d] += sum(len(s["text"]) for s in ln["spans"])
    if not w:
        return (1.0, 0.0)
    ux, uy = w.most_common(1)[0][0]
    return (float(ux), float(uy))


def turn(x: float, y: float, u: tuple[float, float]) -> tuple[float, float]:
    """Page point -> reading frame: X along the text direction u, Y along u turned 90° clockwise
    (screen coordinates, y down), so rows run top to bottom."""
    ux, uy = u
    return (x * ux + y * uy, -x * uy + y * ux)


def turn_rect(r, u) -> tuple[float, float, float, float]:
    pts = [turn(x, y, u) for x in (r[0], r[2]) for y in (r[1], r[3])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _merge(vals: list[float]) -> list[float]:
    out: list[float] = []
    for v in sorted(vals):
        if out and v - out[-1] <= MERGE:
            continue
        out.append(v)
    return out


def rules(drawings: list[dict[str, Any]], u) -> tuple[list[float], list[float]]:
    """(X positions of vertical rules, Y positions of horizontal rules), in the reading frame."""
    xs, ys = [], []
    for d in drawings:
        for it in d.get("items", []):
            segs = []
            if it[0] == "l":
                segs.append((it[1], it[2]))
            elif it[0] == "re":
                r = it[1]
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                if r.width <= AXIS_TOL * 2:                   # a thin filled bar is one rule
                    segs.append(((cx, r.y0), (cx, r.y1)))
                elif r.height <= AXIS_TOL * 2:
                    segs.append(((r.x0, cy), (r.x1, cy)))
                else:                                         # a cell border: its four sides
                    segs += [((r.x0, r.y0), (r.x1, r.y0)), ((r.x0, r.y1), (r.x1, r.y1)),
                             ((r.x0, r.y0), (r.x0, r.y1)), ((r.x1, r.y0), (r.x1, r.y1))]
            for a, b in segs:
                ax, ay = turn(a[0], a[1], u)
                bx, by = turn(b[0], b[1], u)
                if abs(ax - bx) <= AXIS_TOL and abs(ay - by) >= MIN_RULE:
                    xs.append((ax + bx) / 2)
                elif abs(ay - by) <= AXIS_TOL and abs(ax - bx) >= MIN_RULE:
                    ys.append((ay + by) / 2)
    return _merge(xs), _merge(ys)


def page_words(page) -> list[dict[str, Any]]:
    """Words with their own text direction: characters of one line split at spaces. A table on a
    sideways page often sets its column headers at 90° to its body, so each word keeps its line's
    `dir` (PyMuPDF text coordinates are the unrotated page's, like the drawings)."""
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            d = tuple(float(round(x)) for x in ln["dir"])
            cur: list[dict] = []

            def flush():
                if cur:
                    out.append({"text": "".join(c["c"] for c in cur), "dir": d,
                                "bbox": (min(c["bbox"][0] for c in cur), min(c["bbox"][1] for c in cur),
                                         max(c["bbox"][2] for c in cur), max(c["bbox"][3] for c in cur))})
                    cur.clear()
            for s in ln["spans"]:
                for c in s["chars"]:
                    if c["c"].isspace():
                        flush()
                    else:
                        cur.append(c)
                flush()
    return out


def words_turned(page, u) -> list[dict[str, Any]]:
    out = []
    for w in page_words(page):
        x0, y0, x1, y1 = turn_rect(w["bbox"], u)
        # the word's own reading order: along its direction, lines stacked across it
        cx, cy = (w["bbox"][0] + w["bbox"][2]) / 2, (w["bbox"][1] + w["bbox"][3]) / 2
        along, across = turn(cx, cy, w["dir"])
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": w["text"], "dir": w["dir"],
                    "along": along, "across": across})
    return out


def _bands(vals: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Merge [lo, hi] extents that overlap (within tol) into bands."""
    out: list[list[float]] = []
    for lo, hi in sorted(vals):
        if out and lo <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def _cuts_from_bands(bands: list[tuple[float, float]]) -> list[float]:
    return [(bands[i][1] + bands[i + 1][0]) / 2 for i in range(len(bands) - 1)]


def _index(v: float, cuts: list[float]) -> int:
    i = 0
    while i < len(cuts) and v > cuts[i]:
        i += 1
    return i


def grid(page) -> dict[str, Any]:
    """Rows of cells of a table page, in reading orientation. Returns
    {frame, method: {columns, rows}, rows: [[cell text, ...]]}."""
    lines = [ln for b in page.get_text("dict")["blocks"] for ln in b.get("lines", [])]
    u = reading_frame(lines)
    ws = [w for w in words_turned(page, u) if w["text"].strip()]
    if not ws:
        return {"frame": u, "method": {}, "rows": []}
    vx, hy = rules(page.get_drawings(), u)
    lo_x, hi_x = min(w["x0"] for w in ws), max(w["x1"] for w in ws)
    lo_y, hi_y = min(w["y0"] for w in ws), max(w["y1"] for w in ws)
    vx = [x for x in vx if lo_x < x < hi_x]
    hy = [y for y in hy if lo_y < y < hi_y]
    height = statistics.median(w["y1"] - w["y0"] for w in ws)
    if len(vx) >= 2:
        ccuts, cmeth = vx, "rules"
    else:
        ccuts, cmeth = _cuts_from_bands(_bands([(w["x0"], w["x1"]) for w in ws], height * 0.8)), "gaps"
    if len(hy) >= 2:
        rcuts, rmeth = hy, "rules"
    else:
        rcuts, rmeth = _cuts_from_bands(_bands([(w["y0"], w["y1"]) for w in ws], -height * 0.3)), "lines"
    ncol = len(ccuts) + 1
    cells: dict[tuple[int, int], list[dict]] = collections.defaultdict(list)
    for w in ws:
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
        cells[(_index(cy, rcuts), _index(cx, ccuts))].append(w)
    rows = []
    for r in sorted({k[0] for k in cells}):
        row = []
        for c in range(ncol):
            got = sorted(cells.get((r, c), []),
                         key=lambda w: (w["dir"] != u, round(w["across"] / max(height, 1e-6)), w["along"]))
            row.append(" ".join(w["text"] for w in got))
        rows.append(row)
    while rows and ncol and all(not row[-1] for row in rows):          # an empty margin column
        rows = [row[:-1] for row in rows]
        ncol -= 1
    while rows and ncol and all(not row[0] for row in rows):
        rows = [row[1:] for row in rows]
        ncol -= 1
    return {"frame": u, "method": {"columns": cmeth, "rows": rmeth}, "rows": rows}


def render_grid(g: dict[str, Any], page_no: int) -> str:
    ux, uy = g["frame"]
    turned = {(1.0, 0.0): "as printed", (0.0, -1.0): "turned 90° (text ran upward)",
              (0.0, 1.0): "turned 90° (text ran downward)", (-1.0, 0.0): "turned 180°"}.get((ux, uy), f"dir {ux},{uy}")
    m = g["method"]
    head = (f"page {page_no}: {len(g['rows'])} rows x {len(g['rows'][0]) if g['rows'] else 0} columns, {turned}; "
            f"columns by {m.get('columns', '-')}, rows by {m.get('rows', '-')}. Read positions only: quote the "
            f"text layer, and check a surprising cell against the page image.")
    return "\n".join([head] + [f"r{i}: " + " | ".join(c.replace("|", "/") for c in row)
                               for i, row in enumerate(g["rows"], 1)]) + "\n"


def cmd_columns(args) -> int:
    import pymupdf
    from .check import resolve_pdf_path
    adir = common.atlas_dir(args.atlas_dir)
    docdir = adir / "docs" / args.doc_id
    if not (docdir / "doc.json").is_file():
        print(f"doc columns: no docs/{args.doc_id}/doc.json (run doc prep)", file=sys.stderr)
        return 2
    path = resolve_pdf_path(docdir, common.read_json(docdir / "doc.json"))
    if path is None:
        print(f"doc columns: the source PDF of {args.doc_id} is not found", file=sys.stderr)
        return 2
    doc = pymupdf.open(str(path))
    rc = 0
    for n in args.pages:
        if not 1 <= n <= doc.page_count:
            print(f"doc columns: page {n} is outside 1..{doc.page_count}", file=sys.stderr)
            rc = 2
            continue
        sys.stdout.write(render_grid(grid(doc[n - 1]), n))
    return rc


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("columns", help="a table page as rows and cells in reading orientation (rotated tables)")
    p.add_argument("doc_id")
    p.add_argument("pages", type=int, nargs="+", help="1-based physical pages")
    p.set_defaults(func=cmd_columns)


def main(pdf: str, page: int) -> None:  # pragma: no cover - manual use
    import pymupdf
    print(render_grid(grid(pymupdf.open(Path(pdf))[page - 1]), page), end="")
