"""`pdf pages <file>`: pages of any PDF as PNG files an agent can open with the image Read tool.

Works on an arbitrary PDF, outside an atlas build, with poppler only (`pdfinfo`, `pdftotext`,
`pdftoppm`): no imaging library. Prints, per page and first, whether a usable text layer exists
(a caller uses `pdftotext` when it does), then one line per PNG: page, tile, pixel size, path.

A page can be cropped to a box in PDF points (origin top-left of the page as displayed, the
frame `pdftoppm -x -y -W -H` uses once scaled by dpi/72) and split into a COLSxROWS tile grid;
tiles divide the crop (or the whole page) evenly, the last column and row taking the remainder.
"""
from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# Non-whitespace characters on a page below which its text layer cannot carry the page: a scan
# with an OCR-less footer or a page number still counts as having no usable layer.
MIN_TEXT_CHARS = 40
DEFAULT_DPI = 80
MAX_DPI = 600


class PagesError(Exception):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    except FileNotFoundError:
        raise PagesError(f"{cmd[0]} not found: install poppler-utils") from None
    except subprocess.TimeoutExpired:
        raise PagesError(f"{cmd[0]} timed out") from None


def page_count(pdf: Path) -> int:
    out = _run(["pdfinfo", str(pdf)])
    if out.returncode != 0:
        raise PagesError(f"pdfinfo could not read {pdf}: {out.stderr.strip() or 'unreadable PDF'}")
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    if not m:
        raise PagesError(f"pdfinfo printed no page count for {pdf}")
    return int(m.group(1))


def page_sizes(pdf: Path, first: int, last: int) -> dict[int, tuple[float, float]]:
    """Displayed page size in points per page, rotation applied."""
    out = _run(["pdfinfo", "-f", str(first), "-l", str(last), str(pdf)]).stdout
    sizes: dict[int, tuple[float, float]] = {}
    rots: dict[int, int] = {}
    for m in re.finditer(r"^Page\s+(\d+)\s+size:\s+([\d.]+) x ([\d.]+) pts", out, re.M):
        sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    for m in re.finditer(r"^Page\s+(\d+)\s+rot:\s+(\d+)", out, re.M):
        rots[int(m.group(1))] = int(m.group(2))
    for p, (w, h) in list(sizes.items()):
        if rots.get(p, 0) % 180 == 90:
            sizes[p] = (h, w)
    return sizes


def text_chars(pdf: Path, page: int) -> int:
    out = _run(["pdftotext", "-f", str(page), "-l", str(page), str(pdf), "-"]).stdout
    return len(re.sub(r"\s", "", out))


def parse_pages(spec: str, total: int) -> list[int]:
    """`4-7`, `1,3,5-6`, `9-` (to the end): sorted, unique, inside 1..total."""
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)(?:(-)(\d*))?", part)
        if not m:
            raise PagesError(f"--pages: cannot read '{part}' (use 4, 4-7, 9- or a comma list)")
        a = int(m.group(1))
        b = a if not m.group(2) else (int(m.group(3)) if m.group(3) else total)
        if a < 1 or b < a or b > total:
            raise PagesError(f"--pages: '{part}' is outside 1..{total} (the PDF has {total} pages)")
        pages.update(range(a, b + 1))
    return sorted(pages)


def parse_tiles(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)", spec)
    if not m or int(m.group(1)) < 1 or int(m.group(2)) < 1:
        raise PagesError(f"--tiles: '{spec}' is not COLSxROWS, for example 2x2")
    return int(m.group(1)), int(m.group(2))


def parse_crop(spec: str) -> tuple[float, float, float, float]:
    try:
        x, y, w, h = (float(v) for v in spec.split(","))
    except ValueError:
        raise PagesError(f"--crop: '{spec}' is not X,Y,W,H in PDF points") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise PagesError("--crop: X and Y must be >= 0 and W and H > 0")
    return x, y, w, h


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise PagesError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def _render(pdf: Path, page: int, dpi: int, box: tuple[int, int, int, int] | None, out_stem: Path) -> Path:
    cmd = ["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile"]
    if box:
        x, y, w, h = box
        cmd += ["-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h)]
    cmd += [str(pdf), str(out_stem)]
    r = _run(cmd)
    png = out_stem.with_suffix(".png")
    if r.returncode != 0 or not png.is_file():
        raise PagesError(f"pdftoppm failed on page {page}: {r.stderr.strip() or 'no output'}")
    return png


def _splits(total: int, n: int) -> list[tuple[int, int]]:
    """n (offset, length) pieces of `total` pixels; the last takes the remainder."""
    step = total // n
    return [(i * step, step if i < n - 1 else total - i * step) for i in range(n)]


def render_pages(pdf: Path, pages: list[int], out: Path, dpi: int = DEFAULT_DPI,
                 tiles: tuple[int, int] = (1, 1), crop: tuple[float, float, float, float] | None = None,
                 emit=print) -> list[Path]:
    sizes = page_sizes(pdf, pages[0], pages[-1])
    out.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    cols, rows = tiles
    scale = dpi / 72.0
    for page in pages:
        n = text_chars(pdf, page)
        if n >= MIN_TEXT_CHARS:
            emit(f"p{page}: text layer yes ({n} chars); `pdftotext -f {page} -l {page}` reads it")
        else:
            emit(f"p{page}: text layer no ({n} chars); image-only, read the PNGs")
        if page not in sizes:
            raise PagesError(f"pdfinfo gave no size for page {page}")
        pw, ph = sizes[page]
        x0, y0, w0, h0 = crop if crop else (0.0, 0.0, pw, ph)
        if x0 >= pw or y0 >= ph:
            raise PagesError(f"--crop starts outside page {page} ({pw:.0f} x {ph:.0f} pt)")
        w0, h0 = min(w0, pw - x0), min(h0, ph - y0)
        px0, py0 = round(x0 * scale), round(y0 * scale)
        # poppler sizes a page at the ceiling of points x dpi/72
        pw_px, ph_px = max(1, math.ceil(w0 * scale - 1e-6)), max(1, math.ceil(h0 * scale - 1e-6))
        if cols > pw_px or rows > ph_px:
            raise PagesError(f"--tiles {cols}x{rows} is finer than the {pw_px}x{ph_px} px region at {dpi} dpi")
        for r, (ty, th) in enumerate(_splits(ph_px, rows), 1):
            for c, (tx, tw) in enumerate(_splits(pw_px, cols), 1):
                whole = tiles == (1, 1) and not crop
                name = f"p{page:03d}" if whole else f"p{page:03d}_r{r}c{c}"
                box = None if whole else (px0 + tx, py0 + ty, tw, th)
                png = _render(pdf, page, dpi, box, out / name)
                w, h = png_size(png)
                tile = "-" if tiles == (1, 1) else f"r{r}c{c}"
                emit(f"  p{page} tile {tile} {w}x{h} px {png}")
                files.append(png)
    return files


def cmd_pages(args) -> int:
    pdf = Path(args.pdf)
    try:
        if not pdf.is_file():
            raise PagesError(f"{pdf}: no such file")
        for tool in ("pdfinfo", "pdftotext", "pdftoppm"):
            if not shutil.which(tool):
                raise PagesError(f"{tool} not found: install poppler-utils")
        if not 10 <= args.dpi <= MAX_DPI:
            raise PagesError(f"--dpi must be 10..{MAX_DPI}")
        total = page_count(pdf)
        pages = parse_pages(args.pages, total) if args.pages else list(range(1, total + 1))
        tiles = parse_tiles(args.tiles)
        crop = parse_crop(args.crop) if args.crop else None
        out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="atlas-pdf-pages-"))
        print(f"{pdf}: {total} pages; rendering {len(pages)} at {args.dpi} dpi into {out}")
        render_pages(pdf, pages, out, args.dpi, tiles, crop)
    except PagesError as e:
        print(f"pdf pages: {e}", file=sys.stderr)
        return 2
    return 0


def register(sub, groups) -> None:
    from . import common
    g = common.verb_group(sub, groups, "pdf", "any PDF, no atlas needed: pages")
    p = g.add_parser("pages", help="render pages of any PDF (scanned or not) to PNGs for the image Read tool")
    p.add_argument("pdf")
    p.add_argument("--pages", help="4, 4-7, 9- or a comma list (default: every page)")
    p.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"render resolution (default {DEFAULT_DPI})")
    p.add_argument("--tiles", default="1x1", help="COLSxROWS grid per page (default 1x1); tiles divide the crop")
    p.add_argument("--crop", help="X,Y,W,H in PDF points from the page's top-left; applied before tiling")
    p.add_argument("--out", help="output directory (default: a new temp directory, printed)")
    p.set_defaults(func=cmd_pages)
