"""Follow a lookup's `next=` offsets to the end, with a tiny budget so a small fixture answer is cut."""
import re

from embarch_atlas import query

CUT = re.compile(r"\n… truncated: call again with offset=(\d+) \(next=(\d+)\)\n\Z")


def split_header(text: str) -> tuple[str, str]:
    head, sep, body = text.partition("\n\n")
    return head + sep, body


def follow(monkeypatch, tool: str, call, tokens: int = 2):
    """`call(offset)` -> the tool's answer. Returns (uncut answer, pages) where each page is
    (header, body without its truncation line). The answer is cut at `tokens` tokens."""
    uncut = call(0)
    assert CUT.search(uncut) is None, f"{tool}: the uncut answer is already cut"
    monkeypatch.setitem(query.BUDGET, tool, tokens)
    pages, off = [], 0
    while True:
        out = call(off)
        m = CUT.search(out)
        head, body = split_header(CUT.sub("", out) if m else out)
        pages.append((head, body))
        if not m:
            return uncut, pages
        assert int(m.group(1)) == int(m.group(2)) > off, (tool, off, m.group(0))
        off = int(m.group(1))
        assert len(pages) < 10000


def assert_exact(monkeypatch, tool: str, call, tokens: int = 2):
    """The pages, followed through `next=`, give the uncut body once: no line lost or repeated, and the
    `src:` header is the same on every page."""
    uncut, pages = follow(monkeypatch, tool, call, tokens)
    head, body = split_header(uncut)
    assert len(pages) > 1, f"{tool}: fixture answer is not larger than a {tokens}-token budget"
    assert {h for h, _ in pages} == {head}, tool
    assert "".join(b for _, b in pages) == body, tool
