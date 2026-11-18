"""`fetch-text`: local fixtures and a loopback HTTP server only; nothing leaves the machine."""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pymupdf
import pytest

from embarch_atlas import cli

PAGE = """<html><head><title>A &amp; B</title><style>p{color:red}</style></head>
<body><script>var x = "<p>nope</p>";</script><h1>Heading</h1>
<p>First   paragraph &lt;with&gt; entities &amp; &eacute;.</p><p>Second<br>line</p></body></html>"""
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing the site.</body></html>"
LONG = "<html><body><p>" + ("A real article that mentions a captcha in passing. " * 80) + "</p></body></html>"


def make_pdf(path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 100), "Synthetic patent text layer", fontsize=12)
    doc.save(path)
    return path.read_bytes()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    pdf = make_pdf(tmp_path_factory.mktemp("f") / "a.pdf")
    routes = {
        "/page": (200, "text/html; charset=utf-8", PAGE.encode()),
        "/cf": (403, "text/html", CHALLENGE.encode()),
        "/rl": (429, "text/html", b"<p>Too many requests</p>"),
        "/pdf": (200, "application/pdf", pdf),
        "/missing": (404, "text/html", b"<p>gone</p>"),
    }

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            st, ct, body = routes[self.path]
            self.send_response(st)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def run(capsys, *argv):
    rc = cli.main(["fetch-text", *argv])
    return rc, capsys.readouterr()


def test_local_html_is_stripped(tmp_path, capsys):
    f = tmp_path / "p.html"
    f.write_text(PAGE)
    rc, cap = run(capsys, str(f))
    assert rc == 0
    assert "nope" not in cap.out and "color" not in cap.out and "<" in cap.out
    assert "First paragraph <with> entities & é." in cap.out
    assert "Second\nline" in cap.out and "Heading" in cap.out


def test_url_page(server, capsys):
    rc, cap = run(capsys, server + "/page", "--json")
    d = json.loads(cap.out)
    assert rc == 0 and d["blocked"] is False and d["title"] == "A & B" and "Heading" in d["text"]


@pytest.mark.parametrize("path", ["/cf", "/rl"])
def test_challenge_is_blocked_not_printed(server, capsys, path):
    rc, cap = run(capsys, server + path)
    assert rc == 3 and cap.out == "" and "blocked: true" in cap.err
    rc, cap = run(capsys, server + path, "--json")
    assert rc == 3 and json.loads(cap.out)["blocked"] is True


def test_local_challenge_page_is_blocked(tmp_path, capsys):
    f = tmp_path / "c.html"
    f.write_text(CHALLENGE)
    rc, cap = run(capsys, str(f))
    assert rc == 3 and "Checking your browser" not in cap.out


def test_long_article_mentioning_captcha_is_content(tmp_path, capsys):
    f = tmp_path / "l.html"
    f.write_text(LONG)
    rc, cap = run(capsys, str(f))
    assert rc == 0 and "real article" in cap.out


def test_plain_404_is_an_error(server, capsys):
    rc, cap = run(capsys, server + "/missing")
    assert rc == 2 and "HTTP 404" in cap.err


def test_missing_file(tmp_path, capsys):
    rc, cap = run(capsys, str(tmp_path / "nope.html"))
    assert rc == 2


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler not installed")
def test_pdf_response_and_local_pdf(server, tmp_path, capsys):
    rc, cap = run(capsys, server + "/pdf")
    assert rc == 0 and "Synthetic patent text layer" in cap.out
    make_pdf(tmp_path / "l.pdf")
    rc, cap = run(capsys, str(tmp_path / "l.pdf"))
    assert rc == 0 and "Synthetic patent text layer" in cap.out
