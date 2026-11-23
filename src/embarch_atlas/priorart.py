"""`prior-art search`: keyword and class queries against DEPATISnet's expert search as one hit table.

A standalone research verb outside the document pipeline, like `fetch-text`: nothing it fetches is
distilled or enters the library or an atlas, and no atlas is needed. Standard library only.

One run is a session, a bounded number of requests and no more: one GET of the expert form (for its
cookie), one POST of the query (the hit list), and one `bibdat` page per hit shown (the hit list
carries only publication numbers). Requests are spaced `DELAY_S` apart and the run stops at the first
403, 429 or 503, or a bot-challenge page, and says `blocked`. The terms for the service, read
2026-11-23, allow automated requests below 5,000 a day per person or address and say the service is
not for bulk downloads; a run here makes at most `MAX_HITS_LIMIT + 2` requests.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import fetchtext

EXIT_BLOCKED = fetchtext.EXIT_BLOCKED
SOURCE = "depatisnet"
BASE_URL = "https://depatisnet.dpma.de/DepatisNet/depatisnet"
USER_AGENT = "embarch-atlas-prior-art/1"
DELAY_S = 2.0
TIMEOUT_S = 60
MAX_BYTES = 5 * 1024 * 1024
DEFAULT_HITS = 5
MAX_HITS_LIMIT = 10
ABSTRACT_MD_CHARS = 200

_TREFFER = "window=1&space=main&content=experte&action=treffer&firstdoc=1"


class PriorArtError(Exception):
    pass


class Blocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- query

_WORD = re.compile(r"[^\W_][\w\-]*", re.U)


def _words(text: str, what: str) -> list[str]:
    words = _WORD.findall(text)
    if not words:
        raise PriorArtError(f"{what} {text!r} has no searchable word")
    return words


def build_query(keywords: list[str], classes: list[str], applicants: list[str], field: str = "ti") -> str:
    """The expert-search string. Every word is its own `field=word` term joined by `and`: a
    parenthesised `ti=(...)` and a multi-word `pa=` value were refused by the source, a bare
    `field=word` is not. Several classes, or several applicants, are alternatives (`or`)."""
    if field not in ("ti", "ab"):
        raise PriorArtError(f"--field is ti or ab, not {field!r}")
    if not keywords and not classes and not applicants:
        raise PriorArtError("give at least one keyword, --class or --applicant")
    parts = [f"{field}={w}" for kw in keywords for w in _words(kw, "keyword")]
    if classes:
        cls = []
        for c in classes:
            c = re.sub(r"\s+", "", c)
            if not re.fullmatch(r"[A-HY]\d{2}[A-Z]?(\d+/\d+)?", c, re.I):
                raise PriorArtError(f"--class {c!r} is not a CPC/IPC class such as G01D5/24 or H03K")
            cls.append(f"ic={c.upper()}")
        parts.append(cls[0] if len(cls) == 1 else "(" + " or ".join(cls) + ")")
    if applicants:
        pas = [" and ".join(f"pa={w}" for w in _words(a, "applicant")) for a in applicants]
        parts.append(pas[0] if len(pas) == 1 else "(" + " or ".join(f"({p})" for p in pas) + ")")
    return " and ".join(parts)


# ---------------------------------------------------------------- HTTP

class Session:
    """A cookie-keeping client that spaces its requests and refuses to continue past a block."""

    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.requests = 0
        self._last = 0.0

    def get(self, url: str, data: dict | None = None) -> str:
        wait = DELAY_S - (time.monotonic() - self._last)
        if self.requests and wait > 0:
            time.sleep(wait)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                status, ctype, raw = r.status, r.headers.get("Content-Type", ""), r.read(MAX_BYTES)
        except urllib.error.HTTPError as e:
            status, ctype, raw = e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read(MAX_BYTES)
        except (urllib.error.URLError, OSError) as e:
            raise PriorArtError(f"{url}: {getattr(e, 'reason', e)}") from None
        finally:
            self._last = time.monotonic()
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        try:
            page = raw.decode(m.group(1) if m else "utf-8", errors="replace")
        except LookupError:
            page = raw.decode("utf-8", errors="replace")
        if status in fetchtext.BLOCK_STATUSES:
            title, text = fetchtext.html_to_text(page)
            raise Blocked(fetchtext.blocked_reason(status, title, text) or f"HTTP {status}")
        if status >= 400:
            raise PriorArtError(f"{url}: HTTP {status}")
        return page


# ---------------------------------------------------------------- parsing

def _text(html: str) -> str:
    return fetchtext.html_to_text(html)[1]


def parse_hit_list(html: str) -> tuple[int, list[str]]:
    """(total hit count, publication numbers in order) from a hit-list page; (0, []) for no hits."""
    text = _text(html)
    ids = list(dict.fromkeys(re.findall(r"action=bibdat&(?:amp;)?docid=([A-Z0-9]+)", html)))
    m = re.search(r"Gesamttreffer\s+([\d.,]+)", text) or re.search(r"Total hits\s+([\d.,]+)", text, re.I)
    if m:
        return int(re.sub(r"\D", "", m.group(1))), ids
    if re.search(r"lieferte keine Treffer|returned no hits|no hits", text, re.I):
        return 0, []
    if ids:
        return len(ids), ids
    err = re.search(r'id="errormsg".*?</p>\s*<p>(.*?)</p>', html, re.S)
    detail = re.sub(r"\s+", " ", _text(err.group(1))) if err else re.sub(r"\s+", " ", text)[:200]
    raise PriorArtError(f"the hit list was not recognised ({detail})")


class _Rows(HTMLParser):
    """Table rows as lists of cells, each cell a list of its lines (a <br> or block ends a line)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[list[str]]] = []
        self._row: list[list[str]] | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = [""]
        elif tag in ("br", "p", "div", "li") and self._cell is not None:
            self._cell.append("")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append([re.sub(r"\s+", " ", ln).strip() for ln in self._cell if ln.strip()])
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell[-1] += data


def _iso(date: str) -> str | None:
    """dd.mm.yyyy (the source) or yyyymmdd (a priority line) as yyyy-mm-dd; None for anything else."""
    m = re.fullmatch(r"(\d\d)\.(\d\d)\.(\d{4})", date)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.fullmatch(r"(\d{4})(\d\d)(\d\d)", date)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _lang(value: str) -> tuple[str | None, str]:
    m = re.match(r"\[([A-Z]{2})\]\s*(.*)", value, re.S)
    return (m.group(1), m.group(2)) if m else (None, value)


def _party(line: str) -> dict:
    name, _, country = line.rpartition(", ")
    if name and re.fullmatch(r"[A-Z]{2}", country):
        return {"name": name, "country": country}
    return {"name": line, "country": None}


def parse_bibdat(html: str, docid: str) -> dict:
    """One hit from a bibliographic-data page; fields the page leaves empty stay None or []."""
    p = _Rows()
    p.feed(html)
    fields: dict[str, list[str]] = {}
    for row in p.rows:
        if len(row) < 4:
            continue
        codes, values = row[2], row[3]
        if len(codes) > 1 and len(codes) == len(values):
            for c, v in zip(codes, values):
                fields.setdefault(c, []).append(v)
        elif len(codes) == 1:
            fields.setdefault(codes[0], []).extend(values)
    if not fields:
        raise PriorArtError(f"{docid}: no bibliographic data in the page")
    one = lambda k: (fields.get(k) or [None])[0]  # noqa: E731
    lang, title = _lang(one("TI") or "")
    abs_lang, abstract = _lang(" ".join(fields.get("AB", [])))
    prio = [{"country": c, "number": n, "date": _iso(d)} for c, n, d in zip(
        fields.get("PRC", []), fields.get("PRN", []), fields.get("PRD", []))]
    return {
        "source": SOURCE,
        "publication": docid,
        "title": title or None,
        "title_lang": lang,
        "applicants": [_party(x) for x in fields.get("PA", [])],
        "inventors": [_party(x) for x in fields.get("IN", [])],
        "filing_date": _iso(one("AD") or ""),
        "publication_date": _iso(one("PUB") or ""),
        "priority": prio,
        "cpc": fields.get("CPC", []),
        "ipc": [x for k in ("ICM", "ICS") for x in fields.get(k, []) if not x.startswith("(")],
        "family": None,
        "abstract": abstract or None,
        "abstract_lang": abs_lang,
        "url": f"{BASE_URL}?action=bibdat&docid={docid}",
    }


# ---------------------------------------------------------------- the search

def _parsed(parse, page: str, *args):
    """Run a parser; a page it cannot read that is a challenge page is `Blocked`, not a parse error."""
    try:
        return parse(page, *args)
    except PriorArtError:
        title, text = fetchtext.html_to_text(page)
        reason = fetchtext.blocked_reason(200, title, text)
        if reason:
            raise Blocked(reason) from None
        raise

def search(keywords: list[str], classes: list[str], applicants: list[str], max_hits: int = DEFAULT_HITS,
           field: str = "ti") -> dict:
    """Run the query; the result's `blocked` is True, with `reason`, when the source answered with a
    block, and `hits` then holds what was read before it."""
    if not 1 <= max_hits <= MAX_HITS_LIMIT:
        raise PriorArtError(f"--max-hits is 1 to {MAX_HITS_LIMIT}")
    expert = build_query(keywords, classes, applicants, field)
    res = {"source": SOURCE, "query": {"keywords": keywords, "classes": classes, "applicants": applicants,
                                       "field": field, "expert": expert},
           "total_hits": None, "returned": 0, "blocked": False, "hits": []}
    s = Session()
    try:
        s.get(f"{BASE_URL}?action=experte")
        page = s.get(f"{BASE_URL}?{_TREFFER}", {"query": expert, "hitsPerPage": str(MAX_HITS_LIMIT),
                                                 "maxHitsUser": str(MAX_HITS_LIMIT), "tlForm": "tabelle"})
        res["total_hits"], ids = _parsed(parse_hit_list, page)
        for docid in ids[:max_hits]:
            res["hits"].append(_parsed(parse_bibdat, s.get(f"{BASE_URL}?action=bibdat&docid={docid}"), docid))
    except Blocked as b:
        res["blocked"], res["reason"] = True, b.reason
    res["returned"] = len(res["hits"])
    res["requests"] = s.requests
    return res


# ---------------------------------------------------------------- output

def _cell(s: str | None) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(res: dict) -> str:
    q = res["query"]
    out = [f"# Prior-art search: {SOURCE}", "", f"Query: `{q['expert']}`", "",
           f"Hits: {res['total_hits'] if res['total_hits'] is not None else 'unknown'}"
           f", shown {res['returned']}. Family status: not given by this source.", ""]
    if res["blocked"]:
        out += [f"**Blocked** ({res['reason']}): the source refused, the table below is what was read before it.", ""]
    if res["hits"]:
        out += ["| # | Publication | Title | Applicant | Filed | Priority | Abstract |", "|---|---|---|---|---|---|---|"]
        for i, h in enumerate(res["hits"], 1):
            abstract = h["abstract"] or ""
            if len(abstract) > ABSTRACT_MD_CHARS:
                abstract = abstract[:ABSTRACT_MD_CHARS].rstrip() + "..."
            prio = ", ".join(f"{p['country']} {p['number']} ({p['date']})" for p in h["priority"])
            out.append("| " + " | ".join([str(i), _cell(h["publication"]), _cell(h["title"]),
                                           _cell("; ".join(a["name"] for a in h["applicants"])),
                                           _cell(h["filing_date"]), _cell(prio), _cell(abstract)]) + " |")
    else:
        out.append("No hits.")
    return "\n".join(out)


def cmd_search(args) -> int:
    try:
        res = search(args.keyword, args.cls, args.applicant, args.max_hits, args.field)
    except PriorArtError as e:
        print(f"prior-art search: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else to_markdown(res))
    if res["blocked"]:
        print(f"blocked: true ({res['reason']}); stopped after {res['requests']} requests", file=sys.stderr)
    return EXIT_BLOCKED if res["blocked"] else 0


def register(sub, groups) -> None:
    from . import common
    g = common.verb_group(sub, groups, "prior-art", "patent prior-art research, no atlas needed")
    p = g.add_parser("search", help="DEPATISnet expert search as one hit table (publication, title, applicant, dates, abstract)")
    p.add_argument("keyword", nargs="*", help="generic technology words, each matched in the title (or abstract with --field ab)")
    p.add_argument("--class", dest="cls", action="append", default=[], metavar="CPC_IPC",
                   help="a CPC/IPC class such as G01D5/24 (repeatable: alternatives)")
    p.add_argument("--applicant", action="append", default=[], help="an applicant name, matched word by word (repeatable: alternatives)")
    p.add_argument("--field", default="ti", choices=["ti", "ab"], help="where the keywords are searched (default: title)")
    p.add_argument("--max-hits", type=int, default=DEFAULT_HITS,
                   help=f"hits shown, one request each (default {DEFAULT_HITS}, at most {MAX_HITS_LIMIT})")
    p.add_argument("--json", action="store_true", help="print the result as JSON instead of a markdown table")
    p.set_defaults(func=cmd_search)
