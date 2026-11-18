"""`fetch-text <url|file>`: a web page or local HTML/PDF as plain text, or a report that it was blocked.

Standard library only for HTML (`html.parser`); a PDF, fetched or local, goes through `pdftotext`
via the `pdf pages` helpers. A bot challenge, captcha or rate-limit page is never printed as
content: the command exits 3 and says `blocked`; `--json` prints `{"blocked": true, ...}` instead.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import pdfpages

EXIT_BLOCKED = 3
FETCH_TIMEOUT_S = 30
MAX_BYTES = 20 * 1024 * 1024
USER_AGENT = "embarch-atlas-fetch-text/1"
BLOCK_STATUSES = {403, 429, 503}
# A short page with one of these is an interstitial; a long article that merely quotes one is not.
CHALLENGE_PATTERNS = [
    r"just a moment\.\.\.",
    r"checking your browser before accessing",
    r"verify(?:ing)? (?:that )?you are (?:a )?human",
    r"enable javascript and cookies to continue",
    r"automated queries",
    r"unusual traffic from your computer",
    r"are you a robot",
    r"captcha",
    r"proof[- ]of[- ]work",
    r"too many requests",
    r"rate limit(?:ed| exceeded)?",
    r"access denied",
    r"attention required",
]
CHALLENGE_RE = re.compile("|".join(CHALLENGE_PATTERNS), re.I)
# Visible text above this many characters is a document, not an interstitial.
INTERSTITIAL_MAX_CHARS = 2500

_SKIP = {"script", "style", "noscript", "template", "head", "svg"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
          "header", "footer", "table", "ul", "ol", "pre", "blockquote", "hr", "dt", "dd", "form"}


class FetchError(Exception):
    pass


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in _SKIP and tag != "head":
            self._skip += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and tag != "head" and self._skip:
            self._skip -= 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, body text): scripts, styles and tags removed, entities unescaped, whitespace collapsed."""
    p = _Text()
    p.feed(html)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r"\s+", " ", p.title).strip(), out


def blocked_reason(status: int | None, title: str, text: str) -> str | None:
    """Why this response is a challenge or rate-limit page, or None when it reads as content."""
    hit = CHALLENGE_RE.search(f"{title}\n{text}")
    if status in BLOCK_STATUSES and (hit or len(text) < INTERSTITIAL_MAX_CHARS):
        return f"HTTP {status}" + (f" with '{hit.group(0).lower()}' in the body" if hit else " with a short body")
    if hit and len(text) < INTERSTITIAL_MAX_CHARS:
        return f"challenge page ('{hit.group(0).lower()}')"
    return None


def pdf_to_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="atlas-fetch-text-") as d:
        path = Path(d) / "page.pdf"
        path.write_bytes(data)
        try:
            pdfpages.page_count(path)  # rejects a non-PDF with a clear message
            out = pdfpages._run(["pdftotext", "-layout", str(path), "-"])
        except pdfpages.PagesError as e:
            raise FetchError(str(e)) from None
        if out.returncode != 0:
            raise FetchError(f"pdftotext failed: {out.stderr.strip() or 'no output'}")
        return out.stdout.strip()


def _fetch(url: str) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read(MAX_BYTES)
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"{url}: {getattr(e, 'reason', e)}") from None


def load(source: str) -> tuple[int | None, str, bytes]:
    """(HTTP status or None for a file, content type, bytes)."""
    if re.match(r"https?://", source, re.I):
        return _fetch(source)
    if "://" in source:
        raise FetchError(f"{source}: only http(s) URLs and local files are supported")
    path = Path(source)
    if not path.is_file():
        raise FetchError(f"{source}: no such file")
    return None, "", path.read_bytes()


def convert(source: str) -> dict:
    status, ctype, data = load(source)
    is_pdf = data[:5] == b"%PDF-" or "application/pdf" in ctype.lower()
    if is_pdf:
        if status in BLOCK_STATUSES:
            return {"source": source, "status": status, "blocked": True, "reason": f"HTTP {status}", "kind": "pdf"}
        return {"source": source, "status": status, "blocked": False, "kind": "pdf", "text": pdf_to_text(data)}
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    try:
        html = data.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        html = data.decode("utf-8", errors="replace")
    title, text = html_to_text(html)
    reason = blocked_reason(status, title, text)
    if reason:
        return {"source": source, "status": status, "blocked": True, "reason": reason, "kind": "html"}
    if status is not None and status >= 400:
        raise FetchError(f"{source}: HTTP {status}")
    return {"source": source, "status": status, "blocked": False, "kind": "html", "title": title, "text": text}


def cmd_fetch_text(args) -> int:
    try:
        res = convert(args.source)
    except FetchError as e:
        print(f"fetch-text: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif res["blocked"]:
        print(f"blocked: true ({res['reason']}); the page is a bot challenge or rate limit, not content", file=sys.stderr)
    else:
        print(res["text"])
    return EXIT_BLOCKED if res["blocked"] else 0


def register(sub, groups) -> None:
    p = sub.add_parser("fetch-text", help="a URL, local HTML or PDF as plain text; a bot challenge is reported blocked")
    p.add_argument("source", help="http(s) URL or a local HTML/PDF file")
    p.add_argument("--json", action="store_true", help="print {source,status,blocked,kind,text|reason} as JSON")
    p.set_defaults(func=cmd_fetch_text)
