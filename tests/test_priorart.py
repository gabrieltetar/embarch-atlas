"""`prior-art search`: recorded responses served from a loopback server; nothing leaves the machine.

The live smoke test runs only when EMBARCH_ATLAS_LIVE_PRIOR_ART=1 (about four requests).
"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from embarch_atlas import cli, priorart

FIX = Path(__file__).parent / "fixtures" / "prior_art"
HITLIST = (FIX / "hitlist.html").read_bytes()
NOHITS = (FIX / "nohits.html").read_bytes()
BIB = {"US020190324073A1": (FIX / "bibdat_1.html").read_bytes(), "US020180202838A1": (FIX / "bibdat_2.html").read_bytes()}


class Source:
    """A stand-in for the source: records every request and answers from `plan`."""

    def __init__(self):
        self.seen: list[tuple[str, str, str]] = []
        self.list_response = (200, HITLIST)
        self.bib_status = 200

    def handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _answer(self, body=b"", status=200):
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                outer.seen.append(("GET", self.path, ""))
                if q.get("action") == ["experte"]:
                    return self._answer(b"<html><body>form</body></html>")
                if q.get("action") == ["bibdat"]:
                    if outer.bib_status != 200:
                        return self._answer(b"<p>Too many requests</p>", outer.bib_status)
                    return self._answer(BIB[q["docid"][0]])
                self._answer(b"", 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                outer.seen.append(("POST", self.path, body))
                status, page = outer.list_response
                self._answer(page, status)

            def log_message(self, *a):
                pass

        return H


@pytest.fixture
def src(monkeypatch):
    s = Source()
    srv = HTTPServer(("127.0.0.1", 0), s.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(priorart, "BASE_URL", f"http://127.0.0.1:{srv.server_port}/DepatisNet/depatisnet")
    monkeypatch.setattr(priorart, "DELAY_S", 0)
    yield s
    srv.shutdown()


def run(capsys, *argv):
    rc = cli.main(["prior-art", "search", *argv])
    return rc, capsys.readouterr()


def test_build_query_is_word_by_word():
    q = priorart.build_query(["capacitive sensing", "guard"], ["g01d5/24"], ["Example Sensors"])
    assert q == "ti=capacitive and ti=sensing and ti=guard and ic=G01D5/24 and pa=Example and pa=Sensors"
    assert priorart.build_query(["x"], ["G01D", "H03K"], [], "ab") == "ab=x and (ic=G01D or ic=H03K)"
    for bad in (lambda: priorart.build_query([], [], []), lambda: priorart.build_query(["--"], [], []),
                lambda: priorart.build_query([], ["not a class"], [])):
        with pytest.raises(priorart.PriorArtError):
            bad()


def test_parse_hit_list_and_no_hits():
    assert priorart.parse_hit_list(HITLIST.decode()) == (1574, ["US020190324073A1", "US020180202838A1"])
    assert priorart.parse_hit_list(NOHITS.decode()) == (0, [])
    with pytest.raises(priorart.PriorArtError, match="not recognised"):
        priorart.parse_hit_list("<html><body>something else</body></html>")


def test_parse_bibdat_fields():
    h = priorart.parse_bibdat(BIB["US020190324073A1"].decode(), "US020190324073A1")
    assert h["title"] == "CAPACITIVE SENSOR AND CAPACITIVE SENSOR HEAD" and h["title_lang"] == "EN"
    assert h["applicants"] == [{"name": "EXAMPLE SENSORS LTD", "country": "JP"}]
    assert (h["filing_date"], h["publication_date"]) == ("2018-06-18", "2019-10-24")
    assert h["priority"] == [{"country": "JP", "number": "2018080531", "date": "2018-04-19"}]
    assert "G01D 5/24" in h["cpc"] and h["family"] is None
    assert h["abstract"].startswith("A capacitive sensor includes a sensor head")
    with pytest.raises(priorart.PriorArtError, match="no bibliographic data"):
        priorart.parse_bibdat("<html><table><tr><td>x</td></tr></table></html>", "X")


def test_search_json_and_request_budget(src, capsys):
    rc, cap = run(capsys, "capacitive", "sensor", "--class", "G01D5/24", "--max-hits", "2", "--json")
    assert rc == 0
    res = json.loads(cap.out)
    assert res["total_hits"] == 1574 and res["returned"] == 2 and not res["blocked"]
    assert [h["publication"] for h in res["hits"]] == ["US020190324073A1", "US020180202838A1"]
    assert res["hits"][1]["applicants"][0]["name"] == "SECOND EXAMPLE GMBH"
    assert res["requests"] == 4 and len(src.seen) == 4  # form, query, two bibdat pages
    method, _, body = src.seen[1]
    assert method == "POST"
    assert urllib.parse.parse_qs(body)["query"] == ["ti=capacitive and ti=sensor and ic=G01D5/24"]


def test_search_markdown_table(src, capsys):
    rc, cap = run(capsys, "capacitive", "--max-hits", "1")
    assert rc == 0
    assert "| 1 | US020190324073A1 | CAPACITIVE SENSOR AND CAPACITIVE SENSOR HEAD | EXAMPLE SENSORS LTD | 2018-06-18 |" in cap.out
    assert "Hits: 1574, shown 1." in cap.out and "Family status: not given" in cap.out


def test_pipe_in_a_title_is_escaped(src, capsys):
    rc, cap = run(capsys, "capacitive", "--max-hits", "2")
    assert "CAPACITIVE SENSING ARRANGEMENT \\| WITH PIPE" in cap.out


def test_no_hits(src, capsys):
    src.list_response = (200, NOHITS)
    rc, cap = run(capsys, "zzqxjvw")
    assert rc == 0 and "No hits." in cap.out
    assert len(src.seen) == 2  # no bibdat request for an empty list


def test_rate_limit_on_the_query_stops_at_once(src, capsys):
    src.list_response = (429, b"<p>Too many requests</p>")
    rc, cap = run(capsys, "capacitive", "--json")
    res = json.loads(cap.out)
    assert rc == priorart.EXIT_BLOCKED and res["blocked"] and "429" in res["reason"] and res["hits"] == []
    assert len(src.seen) == 2  # form, then the refused query: nothing after it
    assert "blocked: true" in cap.err


def test_block_mid_run_keeps_what_was_read_and_stops(src, capsys):
    calls = []
    orig = priorart.Session.get

    def flaky(self, url, data=None):
        calls.append(url)
        if "docid=US020180202838A1" in url:
            raise priorart.Blocked("HTTP 503 with a short body")
        return orig(self, url, data)

    priorart.Session.get = flaky
    try:
        rc, cap = run(capsys, "capacitive", "--max-hits", "5", "--json")
    finally:
        priorart.Session.get = orig
    res = json.loads(cap.out)
    assert rc == priorart.EXIT_BLOCKED and res["returned"] == 1 and res["blocked"]
    assert len(calls) == 4  # nothing requested after the block


def test_503_on_a_hit_page(src, capsys):
    src.bib_status = 503
    rc, cap = run(capsys, "capacitive", "--max-hits", "2", "--json")
    res = json.loads(cap.out)
    assert rc == priorart.EXIT_BLOCKED and res["blocked"] and res["hits"] == []
    assert sum(1 for m, p, _ in src.seen if "bibdat" in p) == 1  # stopped at the first refusal


def test_challenge_page_with_status_200_is_blocked(src, capsys):
    src.list_response = (200, b"<html><title>Just a moment...</title><body>Checking your browser before accessing</body></html>")
    rc, cap = run(capsys, "capacitive", "--json")
    assert rc == priorart.EXIT_BLOCKED and json.loads(cap.out)["blocked"]


def test_bad_arguments_exit_2(src, capsys):
    rc, cap = run(capsys)
    assert rc == 2 and "at least one keyword" in cap.err
    rc, cap = run(capsys, "x", "--max-hits", "11")
    assert rc == 2 and src.seen == []


@pytest.mark.skipif(os.environ.get("EMBARCH_ATLAS_LIVE_PRIOR_ART") != "1", reason="live smoke test: set EMBARCH_ATLAS_LIVE_PRIOR_ART=1")
def test_live_smoke(capsys):
    rc, cap = run(capsys, "capacitive", "sensor", "--class", "G01D5/24", "--max-hits", "1", "--json")
    res = json.loads(cap.out)
    assert rc == 0 and res["total_hits"] > 0 and res["hits"][0]["title"]
