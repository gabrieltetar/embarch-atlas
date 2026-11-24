"""`live write-back`: a live finding becomes a `measured` record whose receipt is Core's, against a fake embarch-api
that answers `call-log --seq` the way `embarch-api --json` does (`call_log` with `entries` of `{entry, hash}`)."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from embarch_atlas import live, livelog, measured, records
from embarch_atlas.common import write_jsonl

from test_live import make

FAKE = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
verb = args[3]
def out(o, code=0):
    print(json.dumps(o)); sys.exit(code)
if (here / "down").exists():
    print("connection refused", file=sys.stderr); sys.exit(1)
if verb == "status":
    imgs = here / "images.json"
    out({{"success": True, "status": "ok", "probes": [], "last_images": json.loads(imgs.read_text()) if imgs.exists() else None}})
if verb == "lease-show":
    out({{"success": True, "lease": None}})
if verb == "call-log":
    seq = int(args[args.index("--seq") + 1])
    log = json.loads((here / "call-log.json").read_text())
    ents = [e for e in log["entries"] if e["entry"]["seq"] == seq]
    out({{"success": True, "call_log": {{"verified": log.get("verified", True), "error": log.get("error"),
         "length": len(log["entries"]), "head": "h", "from": seq, "to": seq + 1, "truncated": False, "entries": ents}}}})
out({{"success": False, "error": "unexpected " + verb}}, 1)
"""

IMG = "a" * 64
OTHER = "b" * 64


def call(seq, status=200, board=None, at=1_795_000_000_000, typ="call", route="/live/mem-read"):
    body = {"type": typ, "seq": seq, "at_utc_ms": at + seq * 1000, "method": "POST", "route": route, "status": status,
            "board": board if board is not None else {"role": "dut", "probe_serial": "P1", "chip": "DEMO1",
                                                      "image_sha256": IMG}}
    return {"entry": body, "hash": f"h{seq}"}


def setup(tmp: Path, entries=None, images=None, verified=True):
    ad = make(tmp, {})
    api = ad / "fake-api"
    api.write_text(FAKE.format(python=sys.executable))
    api.chmod(api.stat().st_mode | stat.S_IEXEC)
    (ad / "call-log.json").write_text(json.dumps({"verified": verified, "entries": entries or [call(4), call(5)]}))
    if images is not None:
        (ad / "images.json").write_text(json.dumps(images))
    write_jsonl(ad / "docs" / "demo-ds" / "records" / "limit.jsonl",
                [{"kind": "limit", "id": "demo.abs.vmax", "doc": "demo-ds@1", "p": 3, "src": "table"}])
    return ad


def pin(monkeypatch, same):
    from embarch_atlas import buildq
    import types
    monkeypatch.setattr(buildq, "source", lambda *a: types.SimpleNamespace(label="b", elf=Path("z.elf")))
    monkeypatch.setattr(live, "image_check", lambda *a: (same, "flash matches the ELF at 3 places" if same
                                                          else "flash differs from the ELF at 1 of 3 places"))


def written(ad: Path) -> list[dict]:
    return measured.load(ad)


def test_a_write_back_writes_a_schema_valid_record_with_cores_receipt(tmp_path, monkeypatch):
    ad = setup(tmp_path, images=[{"chip": "DEMO1", "probe_serial": "P1", "sha256": IMG}])
    pin(monkeypatch, True)
    out = measured.write_back(ad, [4, 5], fact="demo.abs.vmax", value="3.31", unit="V")
    assert out.startswith("Wrote measured.demo-abs-vmax.s4-5") and "read back" in out
    (rec,) = written(ad)
    assert records.validate(rec) == []
    assert rec["prov"] == "measured" and rec["via"] == "live" and rec["src"] == "live" and rec["kind"] == "measurement"
    assert rec["fact"] == "demo.abs.vmax" and rec["doc"] == "demo-ds@1" and rec["value"] == 3.31 and rec["unit"] == "V"
    r = rec["receipt"]
    assert [(c["seq"], c["hash"], c["route"], c["status"]) for c in r["calls"]] == [
        (4, "h4", "/live/mem-read", 200), (5, "h5", "/live/mem-read", 200)]
    assert r["board"] == {"role": "dut", "probe_serial": "P1", "chip": "DEMO1"}
    assert r["time"] == "2026-11-18T11:06:44Z"            # entry 4, as Core stamped it
    assert r["image"]["source"] == "read back" and r["image"]["build"] == "pinned" and r["image"]["sha256"] == IMG
    assert "flag" not in rec and r["live_log"] is None


def test_the_receipt_is_cores_not_the_callers_and_the_live_log_line_sits_beside_it(tmp_path, monkeypatch):
    ad = setup(tmp_path, images=[{"chip": "DEMO1", "probe_serial": "P1", "sha256": IMG}])
    pin(monkeypatch, True)
    folder = tmp_path / "fw" / "embarch" / "live"
    folder.mkdir(parents=True)
    line = {"schema": "embarch-live/1", "writer": "embarch-atlas", "tool": "reg_read", "t": "2026-11-23T21:00:00Z",
            "board": {"role": "other", "probe_serial": "P1", "chip": "DEMO1"}, "answer": {"sha256": "ff"},
            "core": [{"seq": 4, "hash": "h4"}]}
    (folder / "2026-11-23-no-lease.jsonl").write_text(json.dumps({"writer": "embarch-api", "core": []}) + "\n"
                                                      + json.dumps(line) + "\n")
    measured.write_back(ad, [4], entity="net VDD", value="3.3")
    (rec,) = written(ad)
    assert rec["entity"] == "net VDD" and rec["doc"] == "measured" and "fact" not in rec
    assert rec["receipt"]["board"]["role"] == "dut"                      # Core's entry wins over the line's
    ll = rec["receipt"]["live_log"]
    assert ll["file"] == "2026-11-23-no-lease.jsonl" and ll["line"] == 2 and ll["tool"] == "reg_read"
    assert ll["disagrees"] == ["role"] and ll["answer_sha256"] == "ff"


@pytest.mark.parametrize("bad", ["missing", "refused", "fold", "unverified", "down"])
def test_an_entry_core_does_not_hold_or_cannot_vouch_for_is_refused(tmp_path, bad):
    ents = {"refused": [call(4, status=409)], "fold": [call(4, typ="fold")]}.get(bad, [call(5)])
    ad = setup(tmp_path, ents, verified=bad != "unverified")
    if bad == "down":
        (ad / "down").write_text("")
    out = measured.write_back(ad, [4], fact="demo.abs.vmax", value="1")
    assert out.startswith("Refused:") and "Nothing was written" in out
    assert written(ad) == [] and not measured.path(ad).exists()


def test_an_unknown_fact_and_a_missing_or_doubled_target_are_refused(tmp_path):
    ad = setup(tmp_path)
    assert "no fact 'demo.nope'" in measured.write_back(ad, [4], fact="demo.nope", value="1")
    assert "one of the two" in measured.write_back(ad, [4], value="1")
    assert "one of the two" in measured.write_back(ad, [4], fact="demo.abs.vmax", entity="x", value="1")
    assert "name the value" in measured.write_back(ad, [4], entity="x", value=" ")
    assert written(ad) == []


def test_another_build_is_written_flagged_and_names_the_last_flashed_image(tmp_path, monkeypatch):
    ad = setup(tmp_path, images=[{"chip": "DEMO1", "probe_serial": "P1", "sha256": OTHER}])
    pin(monkeypatch, False)
    out = measured.write_back(ad, [4], fact="demo.abs.vmax", value="3.4")
    assert "measured on a different build" in out
    (rec,) = written(ad)
    assert records.validate(rec) == [] and rec["flag"] == "measured on a different build"
    img = rec["receipt"]["image"]
    assert img["source"] == "as last flashed by Core" and img["build"] == "different" and img["sha256"] == OTHER


def test_no_read_back_and_no_last_image_is_unknown_not_refused(tmp_path, monkeypatch):
    ad = setup(tmp_path, [call(4, board={"role": "dut", "probe_serial": "P1", "chip": "DEMO1"})])
    from embarch_atlas import buildq
    monkeypatch.setattr(buildq, "source", lambda *a: (_ for _ in ()).throw(LookupError("no pinned build")))
    measured.write_back(ad, [4], entity="U1", value="ok")
    (rec,) = written(ad)
    img = rec["receipt"]["image"]
    assert img["source"] == "unknown" and img["build"] == "unknown" and "no pinned build" in img["check"]
    assert "flag" not in rec


def test_a_board_reflashed_since_the_call_is_a_different_build(tmp_path, monkeypatch):
    ad = setup(tmp_path, images=[{"chip": "DEMO1", "probe_serial": "P1", "sha256": OTHER}])    # the call saw IMG
    pin(monkeypatch, True)
    measured.write_back(ad, [4], entity="U1", value="1")
    (rec,) = written(ad)
    assert rec["receipt"]["image"]["source"] == "read back" and rec["flag"] == "measured on a different build"


def test_the_plane_is_append_only_and_ids_do_not_collide(tmp_path, monkeypatch):
    ad = setup(tmp_path)
    pin(monkeypatch, True)
    measured.write_back(ad, [4], entity="U1", value="1")
    first = measured.path(ad).read_text()
    measured.write_back(ad, [4], entity="U1", value="2")
    text = measured.path(ad).read_text()
    assert text.startswith(first) and len(text.splitlines()) == 2
    assert [r["id"] for r in written(ad)] == ["measured.u1.s4", "measured.u1.s4.2"]


def test_the_cli_verb_and_the_mcp_tool_write_the_same_record(tmp_path, monkeypatch, capsys):
    from embarch_atlas import cli
    ad = setup(tmp_path)
    pin(monkeypatch, True)
    assert cli.main(["--atlas-dir", str(ad), "live", "write-back", "--seq", "4", "--entity", "U1", "--value", "7"]) == 0
    assert "Wrote measured.u1.s4" in capsys.readouterr().out
    assert cli.main(["--atlas-dir", str(ad), "live", "write-back", "--seq", "9", "--entity", "U1", "--value", "7"]) == 1
    assert "Refused: Core holds no call-log entry 9" in capsys.readouterr().out
    from embarch_atlas import mcp_server
    server = mcp_server.build_server(ad, None, True)
    import asyncio
    names = [t.name for t in asyncio.run(server.list_tools())]
    assert "write_back" in names
