"""The live log: the decoded line each live call appends beside embarch-api's raw lines, against a fake embarch-api
that answers like `embarch-api --json` does with `core_calls` (embarch-doc `embarch-api/interfaces/live-log.md`)."""
from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

from embarch_atlas import live, livelog
from embarch_atlas.corestate import core_state

from test_live import BASE, make, make_ext, SENSE

# A fake that numbers Core's call-log entries (`seq`) per gated call and cites them in `core_calls`, answers
# `lease-show` from lease.json (404-ish: no lease) and, for `signal-exchange`, from exchange.json: {"error": ..}
# refuses with a 409, otherwise the text is the echoed command plus `out`. It echoes the lease token it is
# given into its refusal, to prove the line scrubs it.
FAKE_LOGGING = """#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
verb = args[3]
def seq():
    p = here / "seq"
    n = int(p.read_text()) + 1 if p.exists() else 1
    p.write_text(str(n))
    return n
def entry(method, path, status=200):
    n = seq()
    return {{"seq": n, "hash": "h%d" % n, "repeat": None, "method": method, "path": path, "status": status}}
def out(o, code=0):
    print(json.dumps(o)); sys.exit(code)
if verb == "status":
    out({{"success": True, "status": "ok", "probes": []}})
if verb == "lease-show":
    lease = here / "lease.json"
    out({{"success": True, "lease": json.loads(lease.read_text()) if lease.exists() else None}})
if verb == "list-signals":
    out({{"success": True, "signals": [{{"name": "dut-console", "origin_role": "dut", "direction": "bidirectional",
                                       "route": {{"kind": "direct"}}}}]}})
if verb == "mem-read":
    ranges = [a.split(":") for i, a in enumerate(args) if i and args[i - 1] == "--range"]
    mem = {{int(k, 16): v for k, v in json.loads((here / "memory.json").read_text()).items()}}
    rs = [{{"address": int(a, 16), "words": [mem.get(int(a, 16) + 4 * i, 0) for i in range(int(n))]}} for a, n in ranges]
    out({{"success": True, "read": {{"role": "dut", "chip": "DEMO1", "probe_serial": "P1", "ranges": rs, "halted": True,
         "halted_us": 850}}, "core_calls": [entry("POST", "/live/mem-read")]}})
cmd = args[args.index("--write") + 1]
ex = json.loads((here / "exchange.json").read_text()) if (here / "exchange.json").exists() else {{}}
if "error" in ex:
    out({{"success": False, "error": ex["error"] + " token=" + os.environ.get("EMBARCH_LEASE_TOKEN", ""),
         "core_calls": [entry("POST", "/signals/exchange", 409)]}}, 1)
words = cmd.split()
body = ""
if words[0] == "i2c":
    body = "Output: 0x%x\\r\\n" % (0x12 if words[-1].endswith("21") else 0x34)
text = "\\x1b[1;32muart:~$ \\x1b[m" + cmd + "\\r\\n" + body + "\\x1b[1;32muart:~$ "
out({{"success": True, "exchange": {{"signal": "dut-console", "port": "COM9", "text": text, "until_seen": True,
      "echo_seen": True, "written": True, "port_lost": False}}, "core_calls": [entry("POST", "/signals/exchange")]}})
"""


def fake(ad: Path) -> Path:
    api = ad / "fake-api"
    api.write_text(FAKE_LOGGING.format(python=sys.executable))
    api.chmod(api.stat().st_mode | stat.S_IEXEC)
    return ad


def lines(ad: Path) -> list[dict]:
    fw = ad.parent.parent
    return [json.loads(l) for f in sorted((fw / "embarch" / "live").glob("*.jsonl"))
            for l in f.read_text().splitlines()]


def lease_file(ad: Path, holder="claude gabriel", started="2026-11-23T21:00:00Z") -> None:
    (ad / "lease.json").write_text(json.dumps({"holder": holder, "started_at": started}))


def test_a_reg_read_appends_one_decoded_line_naming_the_core_entries(tmp_path):
    ad = fake(make(tmp_path, {BASE + 0x0C: 0x1234}))
    lease_file(ad)
    out = live.reg_read(ad, "UART1.BRR")
    assert "0x1234" in out
    log = tmp_path / "fw" / "embarch" / "live" / "2026-11-23-claude-gabriel-210000.jsonl"
    (line,) = [json.loads(l) for l in log.read_text().splitlines()]
    assert line["schema"] == "embarch-live/1" and line["writer"] == "embarch-atlas"
    assert line["tool"] == "reg_read" and line["ok"] is True and line["outcome"] == "answered"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line["t"])
    assert [c["seq"] for c in line["core"]] == [1] and line["core"][0]["path"] == "/live/mem-read"
    assert line["board"] == {"role": "dut", "probe_serial": "P1", "chip": "DEMO1"}
    assert line["session"] == {"holder": "claude gabriel", "started_at": "2026-11-23T21:00:00Z"}
    assert line["image_sha256"] == "unknown"
    assert "0x00001234" in line["answer"]["text"] and line["answer"]["bytes"] == len(out.encode())
    assert line["sent"] == "reg_read UART1.BRR"        # a library call has no MCP arguments: the CLI's string form


def test_the_decoded_line_follows_the_raw_lines_in_the_same_file(tmp_path):
    ad = fake(make(tmp_path, {}))
    lease_file(ad)
    log = tmp_path / "fw" / "embarch" / "live" / "2026-11-23-claude-gabriel-210000.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"schema":"embarch-live/1","writer":"embarch-api","tool":"mem-read"}\n')
    live.reg_read(ad, "UART1.BRR")
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["writer"] for r in rows] == ["embarch-api", "embarch-atlas"]


def test_with_no_lease_the_file_is_the_no_lease_one(tmp_path):
    ad = fake(make(tmp_path, {}))
    live.reg_read(ad, "UART1.BRR")
    names = [p.name for p in (tmp_path / "fw" / "embarch" / "live").glob("*")]
    assert len(names) == 1 and re.fullmatch(r"\d{4}-\d\d-\d\d-no-lease\.jsonl", names[0])
    assert lines(ad)[0]["session"] is None


def test_the_holder_keeps_only_ascii_letters_and_digits():
    s = {"holder": "a.b_c/é 1", "started_at": "2026-01-02T03:04:05Z"}
    from datetime import datetime, timezone
    assert livelog.file_name(s, datetime.now(timezone.utc)) == "2026-01-02-a-b-c---1-030405.jsonl"


def test_an_external_register_read_over_several_exchanges_names_every_entry(tmp_path):
    ad = fake(make_ext(tmp_path, SENSE))
    out = live.reg_read(ad, "sense_vbus.VBUS")
    assert "src: live dut shell" in out
    (line,) = lines(ad)
    assert line["tool"] == "reg_read" and [c["path"] for c in line["core"]] == ["/signals/exchange"] * len(line["core"])
    assert len(line["core"]) >= 1
    ad = fake(make_ext(tmp_path / "b", SENSE))
    live.reg_read(ad, "sense_vbus.LIM")                   # an LSB + MSB pair: one `i2c read_byte` each
    (line,) = lines(ad)
    assert [c["seq"] for c in line["core"]] == [1, 2]


def test_a_shell_command_with_retval_names_both_exchanges(tmp_path):
    ad = fake(make_ext(tmp_path, SENSE))
    out, code = live.shell_status(ad, "budget set 5")
    assert code == 0
    (line,) = lines(ad)
    assert line["tool"] == "shell" and line["outcome"] == "answered" and len(line["core"]) >= 1
    assert line["answer"]["text"] == out


def test_a_call_refused_at_the_write_appends_a_line_saying_it_did_not_run(tmp_path):
    ad = fake(make_ext(tmp_path, SENSE))
    (ad / "exchange.json").write_text(json.dumps({"error": "Core refused: 409 the bench lease is held elsewhere"}))
    out, code = live.shell_status(ad, "kernel uptime")
    assert code == 1 and "did not run" in out
    (line,) = lines(ad)
    assert line["ok"] is False and line["outcome"] == "did-not-run"
    assert line["core"] and line["core"][0]["status"] == 409
    assert "The command did not run" in line["answer"]["text"]


def test_a_call_that_reached_no_gated_route_writes_nothing(tmp_path):
    ad = fake(make_ext(tmp_path, SENSE))
    out = live.shell_read(ad, "kernel reboot cold")           # refused by the read-only list, nothing sent
    assert out.startswith("src: none\n\nRefused")
    out = live.reg_read(ad, "UART1.NOPE")                   # an unknown register
    assert not (tmp_path / "fw" / "embarch" / "live").exists()


def test_each_shell_exit_status_has_its_outcome_word():
    assert live.SHELL_OUTCOME == {0: "answered", 1: "did-not-run", 2: "no-reply", 3: "may-have-run"}


def test_no_line_carries_the_lease_token(tmp_path):
    ad = fake(make_ext(tmp_path, SENSE))
    (ad / "exchange.json").write_text(json.dumps({"error": "refused: lease"}))
    token = "tok-SECRET-0123456789"
    with live.lease(token, "shell", "mcp"):
        out, code = live.shell_status(ad, "kernel uptime")
    assert token not in out and "<lease token>" in out         # the fake echoed it into the error: scrubbed
    text = "".join(f.read_text() for f in (tmp_path / "fw" / "embarch" / "live").glob("*.jsonl"))
    assert text and token not in text and "<lease token>" in text


def test_an_mcp_call_records_its_arguments_as_an_object_and_never_the_token(tmp_path):
    ad = fake(make(tmp_path, {}))
    with live.lease("tok-SECRET-0123456789", "reg_read", "mcp"):
        live.reg_read(ad, "UART1.BRR", role="dut")
    (line,) = lines(ad)
    assert line["via"] == "mcp" and line["tool"] == "reg_read"
    assert line["sent"] == {"target": "UART1.BRR", "role": "dut"}


def test_the_cli_names_its_subcommand_and_writes_through_the_same_path(tmp_path, monkeypatch, capsys):
    ad = fake(make(tmp_path, {}))
    from embarch_atlas import cli
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--atlas-dir", str(ad), "live", "reg-read", "UART1.BRR", "--role", "dut"]) == 0
    (line,) = lines(ad)
    assert line["via"] == "cli" and line["tool"] == "reg-read"
    assert line["sent"] == "reg-read UART1.BRR --role dut"


def test_core_state_and_live_stacks_write_a_line_each(tmp_path):
    ad = fake(make(tmp_path, {}))
    out = live.live_stacks(ad)
    assert out.startswith("src:")
    (line,) = lines(ad)
    assert line["tool"] == "live_stacks" and line["core"]


def test_a_long_answer_is_cut_but_its_full_length_and_hash_are_kept():
    a = livelog.answer_block("é" * 3000)                    # 6000 bytes
    assert a["truncated"] and a["bytes"] == 6000 and len(a["text"].encode()) <= 4096
    assert len(a["sha256"]) == 64


def test_a_log_that_cannot_be_written_is_said_in_the_text_for_mcp(tmp_path):
    ad = fake(make(tmp_path, {}))
    (tmp_path / "fw" / "embarch" / "live").write_text("a file where the directory should be")
    with live.lease(None, "reg_read", "mcp"):
        out = live.reg_read(ad, "UART1.BRR")
    assert "0x00000000" in out and out.rstrip().splitlines()[-1].startswith("live log: no line written (")


def test_the_repo_is_the_nearest_embarch_folder_up_from_the_working_directory(tmp_path):
    ad = fake(make(tmp_path, {}))
    other = tmp_path / "other"
    (other / "embarch").mkdir(parents=True)
    (other / ".git").mkdir()
    sub = other / "src" / "deep"
    sub.mkdir(parents=True)
    assert livelog.log_repo(ad, sub) == other
    assert livelog.log_repo(ad, tmp_path / "fw") == tmp_path / "fw"       # no .git, but its own embarch/ folder
