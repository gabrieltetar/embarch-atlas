"""Protocol records: a standard's applicability cells (`applicability`, check L, the `applies` lookup) and its state
machines (`state`, `transition`, check E, the `transitions` lookup). A synthetic, invented standard: two
applicability tables, an invented four-state machine with one timer and one message trigger, and a second
machine that shares a state name with it."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from embarch_atlas import check, cli, query, records as R, render
from embarch_atlas.common import read_json, write_json, write_jsonl, write_text
from embarch_atlas.pdftext import normalize

DOC = "demolink-spec@r1.0"
HUB, NODE = "Demo Hub Port", "Demo Node Port"
PAGES = {
    1: "Demo Link Specification\nRevision 1.0\n",
    2: "3 Applicability\n5 State Machines\n",
    3: ("Table 3-1 Message Applicability\n"
        "Message        Hub           Node\n"
        "Widget_Query   Normative     Recommended\n"
        "Widget_Reply   Recommended   Normative\n"
        "Fault_Notice   N/A (2)       Optional (1)\n"
        "(1) Normative for a Node that reports faults.\n"
        "(2) Optional for a Hub that is a bridge.\n"),
    4: ("Table 3-2 Sequence Applicability\n"
        "               Hub                       Node\n"
        "Sequence       Initiator    Responder    Initiator    Responder\n"
        "Link Reset     Normative    Normative    Optional     Normative\n"
        "Widget Swap    Shall Not    N/A          Optional     Recommended\n"),
    5: "Figure 5-1 Demo Hub Port State Diagram\nIdle   Query_Sent   Ready   Fault\n",
    6: ("5.2.1 Idle State\n"
        "On entry to the Idle state the Hub shall reset the QueryCounter.\n"
        "The Hub shall transition to the Query_Sent state when the link comes up.\n"
        "5.2.2 Query_Sent State\n"
        "On entry to the Query_Sent state the Hub shall send a Widget_Query Message and start the ReplyWaitTimer.\n"
        "The Hub shall transition to the Ready state when a reply is received.\n"
        "The Hub shall transition to the Fault state when the ReplyWaitTimer times out.\n"),
    7: ("5.2.3 Ready State\n"
        "On entry to the Ready state the Hub shall stop the ReplyWaitTimer.\n"
        "The Hub shall transition to the Idle state when the link goes down.\n"
        "5.2.4 Fault State\n"
        "On entry to the Fault state the Hub shall send a Fault_Notice Message.\n"
        "The Hub shall transition to the Idle state when the fault is cleared.\n"
        "5.3.1 Idle State\n"
        "The Node shall transition to the Answering state when a Widget_Query Message is received.\n"
        "5.3.2 Answering State\n"
        "The Node shall transition to the Idle state once the Widget_Reply Message is sent.\n"),
}
SECTIONS = [("3", "Applicability", 1, [3, 4]), ("5", "State Machines", 1, [5, 7])]


def cell(table: str, p: int, item: str, kind: str, role: str, level: str, row: str, as_: str | None = None,
         cond: str | None = None, also: str | None = None) -> dict:
    rid = ".".join(x for x in ("demolink.appl", table, R.slug(item), role.lower(), as_, R.slug(cond) if cond else None) if x)
    return {"id": rid, "kind": "applicability", "doc": DOC, "p": p, "src": "table", "q": [row, also] if also else row,
            "item": item, "item_kind": kind, "role": role, "level": level, "table": f"Table {table}",
            "prov": "llm-verified", **({"as": as_} if as_ else {}), **({"cond": cond} if cond else {})}


def cells() -> list[dict]:
    out = []
    for item, row, hub, node in (("Widget_Query", "Widget_Query Normative Recommended", "Normative", "Recommended"),
                                 ("Widget_Reply", "Widget_Reply Recommended Normative", "Recommended", "Normative"),
                                 ("Fault_Notice", "Fault_Notice N/A (2) Optional (1)", "N/A", "Optional")):
        out += [cell("3-1", 3, item, "message", "Hub", hub, row), cell("3-1", 3, item, "message", "Node", node, row)]
    out.append(cell("3-1", 3, "Fault_Notice", "message", "Node", "Normative", "Fault_Notice N/A (2) Optional (1)",
                    cond="reports faults", also="Normative for a Node that reports faults"))
    out.append(cell("3-1", 3, "Fault_Notice", "message", "Hub", "Optional", "Fault_Notice N/A (2) Optional (1)",
                    cond="is a bridge", also="Optional for a Hub that is a bridge"))
    for item, row, levels in (("Link Reset", "Link Reset Normative Normative Optional Normative",
                               ("Normative", "Normative", "Optional", "Normative")),
                              ("Widget Swap", "Widget Swap Shall Not N/A Optional Recommended",
                               ("Shall Not", "N/A", "Optional", "Recommended"))):
        for (role, as_), lv in zip((("Hub", "initiator"), ("Hub", "responder"), ("Node", "initiator"),
                                    ("Node", "responder")), levels):
            out.append(cell("3-2", 4, item, "sequence", role, lv, row, as_=as_))
    return out


def st(machine: str, name: str, p: int, q: str, entry: tuple[str, ...] = ()) -> dict:
    return {"id": f"demolink.state.{R.slug(machine)}.{name}", "kind": "state", "doc": DOC, "p": p, "src": "text",
            "q": q, "machine": machine, "name": name, "prov": "llm-verified", **({"entry": list(entry)} if entry else {})}


def tr(machine: str, frm: str, to: str, p: int, q: str, event: str, trigger: str | None = None) -> dict:
    return {"id": f"demolink.trans.{R.slug(machine)}.{frm}.{to}", "kind": "transition", "doc": DOC, "p": p,
            "src": "text", "q": q, "machine": machine, "from": frm, "to": to, "event": event, "prov": "llm-verified",
            **({"trigger": trigger} if trigger else {})}


def machine_records() -> list[dict]:
    return [
        st(HUB, "Idle", 6, "On entry to the Idle state the Hub shall reset the QueryCounter", ("Reset QueryCounter",)),
        st(HUB, "Query_Sent", 6, "On entry to the Query_Sent state the Hub shall send a Widget_Query Message and start "
           "the ReplyWaitTimer", ("Send Widget_Query Message", "Start ReplyWaitTimer")),
        st(HUB, "Ready", 7, "On entry to the Ready state the Hub shall stop the ReplyWaitTimer", ("Stop ReplyWaitTimer",)),
        st(HUB, "Fault", 7, "On entry to the Fault state the Hub shall send a Fault_Notice Message",
           ("Send Fault_Notice Message",)),
        tr(HUB, "Idle", "Query_Sent", 6, "The Hub shall transition to the Query_Sent state when the link comes up",
           "the link comes up"),
        tr(HUB, "Query_Sent", "Ready", 6, "The Hub shall transition to the Ready state when a reply is received",
           "a reply is received", "Widget_Reply"),                                                 # the message trigger
        tr(HUB, "Query_Sent", "Fault", 6, "The Hub shall transition to the Fault state when the ReplyWaitTimer times out",
           "the ReplyWaitTimer times out", "ReplyWaitTimer"),                                      # the timer trigger
        tr(HUB, "Ready", "Idle", 7, "The Hub shall transition to the Idle state when the link goes down",
           "the link goes down"),
        tr(HUB, "Fault", "Idle", 7, "The Hub shall transition to the Idle state when the fault is cleared",
           "the fault is cleared"),
        st(NODE, "Idle", 7, "5.3.1 Idle State"),
        st(NODE, "Answering", 7, "5.3.2 Answering State"),
        tr(NODE, "Idle", "Answering", 7, "The Node shall transition to the Answering state when a Widget_Query Message "
           "is received", "a Widget_Query Message is received", "Widget_Query"),
        tr(NODE, "Answering", "Idle", 7, "The Node shall transition to the Idle state once the Widget_Reply Message is "
           "sent", "the Widget_Reply Message is sent"),
    ]


FIGURE = {"id": "demolink.fig.5-1", "kind": "figure", "doc": DOC, "p": 5, "src": "figure", "q": None,
          "fig": "Figure 5-1", "caption": "Demo Hub Port State Diagram", "machine": HUB,
          "states": ["Idle", "Query_Sent", "Ready", "Fault"]}


@pytest.fixture()
def std(tmp_path: Path) -> Path:
    """An atlas dir holding the invented standard: pages, toc, applicability, state and transition records."""
    ad = tmp_path / "atlas"
    d = ad / "docs" / DOC
    write_json(d / "doc.json", {"doc_id": DOC, "kind": "standard", "pages": len(PAGES), "file": "DemoLink_Spec.pdf",
                                "part": None, "rev": "r1.0"})
    for p, text in PAGES.items():
        write_text(d / "pages" / f"p{p:03d}.txt", text)
    write_json(d / "toc.json", {"doc_id": DOC, "sections": [
        {"id": i, "title": t, "level": lv, "pages": pg, "tokens": 100} for i, t, lv, pg in SECTIONS]})
    write_jsonl(d / "records" / "applicability.jsonl", cells())
    write_jsonl(d / "records" / "state.jsonl", machine_records())
    write_jsonl(d / "records" / "figure.jsonl", [FIGURE])
    return ad


def _body(out: str) -> str:
    return out.partition("\n\n")[2]


# ---------------------------------------------------------------- schema

def test_the_three_kinds_validate_and_reject_what_they_do_not_take():
    for r in [*cells(), *machine_records(), FIGURE]:
        assert R.validate(r) == [], r["id"]
    bad = dict(cells()[0], item_kind="table")
    assert any("not one of" in e for e in R.validate(bad))
    assert any(e.startswith(f"{cells()[0]['id']}.as:") for e in R.validate(dict(cells()[0], **{"as": "both"})))
    assert any("missing required field 'event'" in e for e in R.validate({k: v for k, v in machine_records()[4].items()
                                                                           if k != "event"}))
    assert any("unknown field 'state'" in e for e in R.validate(dict(machine_records()[0], state="x")))


def test_facts_lines_name_the_cell_and_the_edge():
    line = R.facts_line(cell("3-2", 4, "Link Reset", "sequence", "Hub", "Normative", "row", as_="initiator"))
    assert "kind=applicability; item=Link Reset; item_kind=sequence; role=Hub; as=initiator; level=Normative" in line
    line = R.facts_line(machine_records()[6])
    assert "kind=transition; machine=Demo Hub Port; from=Query_Sent; to=Fault; event=the ReplyWaitTimer times out; " \
           "trigger=ReplyWaitTimer" in line


# ---------------------------------------------------------------- check L

@pytest.mark.parametrize("term, row, ok", [
    ("Widget_Query", "Widget_Query Normative Recommended", True),
    ("Widget_Query", "Widget_\nQuery Normative", True),          # a text layer wraps a long name
    ("Query", "Widget_Query Normative", False),                  # a longer name around it is another name
    ("Widget", "Widget_Query Normative", False),
    ("N/A", "Fault_Notice N/A (2) Optional (1)", True),
    ("Shall Not", "Widget Swap Shall Not N/A", True),
    ("Optional", "Widget_Query Normative Recommended", False),
])
def test_in_row_finds_a_whole_name(term, row, ok):
    assert check.in_row(term, normalize(row)) is ok


def test_check_l_holds_item_and_level_to_the_quoted_row(std):
    d = std / "docs" / DOC
    rep = check.run_check(d, write=False, use_pdf=False)
    appl = [x for x in rep["records"] if x["kind"] == "applicability"]
    assert len(appl) == 16 and all(x["checks"]["L"] == "ok" and x["status"] == "verified" for x in appl), \
        [x for x in appl if x["status"] != "verified"]
    assert rep["summary"]["L"] == {"ok": 16, "of": 16} and rep["summary"]["failed"] == 0
    recs = cells()
    recs[0]["level"] = "Optional"                       # the Hub's Widget_Query cell, read from the wrong column
    recs[2]["item"] = "Widget"                          # a name the row prints only as part of a longer one
    write_jsonl(d / "records" / "applicability.jsonl", recs)
    by = {x["id"]: x for x in check.run_check(d, write=False, use_pdf=False)["records"]}
    assert by[recs[0]["id"]]["checks"]["L"] == "not in the quoted row: level 'Optional'"
    assert by[recs[2]["id"]]["checks"]["L"] == "not in the quoted row: item 'Widget'"
    assert by[recs[0]["id"]]["status"] == "failed"


# ---------------------------------------------------------------- check E

def test_check_e_passes_a_complete_machine(std):
    rep = check.run_check(std / "docs" / DOC, write=False, use_pdf=False)
    e = {x["id"]: x["checks"]["E"] for x in rep["records"] if "E" in x["checks"]}
    assert set(e) == {r["id"] for r in machine_records() if r["kind"] == "transition"} | {FIGURE["id"]}
    assert set(e.values()) == {"ok"} and rep["summary"]["E"] == {"ok": 8, "of": 8}
    assert rep["summary"]["failed"] == 0 and not rep["warnings"]


def test_check_e_fails_an_edge_to_no_state_and_a_drawn_state_with_no_record(std):
    d = std / "docs" / DOC
    recs = machine_records()
    recs[7]["to"] = "Readdy"                                       # Ready -> Readdy
    recs[8]["to"] = "Answering"                                    # a state of the other machine
    recs.append(st(HUB, "Standby", 7, "5.2.3 Ready State"))        # a state the figure does not draw
    write_jsonl(d / "records" / "state.jsonl", recs)
    write_jsonl(d / "records" / "figure.jsonl", [dict(FIGURE, states=FIGURE["states"] + ["Sleep"]),
                                                  dict(FIGURE, id="demolink.fig.5-2", machine=None)])
    rep = check.run_check(d, write=False, use_pdf=False)
    by = {x["id"]: x for x in rep["records"]}
    assert by[recs[7]["id"]]["checks"]["E"] == "not a state record of machine 'Demo Hub Port': to 'Readdy'"
    assert by[recs[8]["id"]]["checks"]["E"] == ("not a state record of machine 'Demo Hub Port': "
                                                "to 'Answering' (a state of 'Demo Node Port')")
    assert by[FIGURE["id"]]["checks"]["E"] == "no state record of machine 'Demo Hub Port' for Sleep"
    assert by[FIGURE["id"]]["status"] == "failed"
    assert by["demolink.fig.5-2"]["checks"]["E"] == "lists states but names no machine"
    assert rep["summary"]["E"] == {"ok": 5, "of": 9}
    assert any(w.startswith("E: 1 state records are not among") and "Demo Hub Port: Standby" in w for w in rep["warnings"])


def test_doc_check_prints_l_and_e(std, capsys):
    assert cli.main(["--atlas-dir", str(std), "doc", "check", DOC, "--no-pdf"]) == 0
    assert "L applicability rows 16/16 | E state machines 8/8" in capsys.readouterr().out


def test_a_figure_with_no_states_and_a_machine_with_no_figure_are_not_checked(std):
    d = std / "docs" / DOC
    write_jsonl(d / "records" / "figure.jsonl", [{k: v for k, v in FIGURE.items() if k != "states"}])
    rep = check.run_check(d, write=False, use_pdf=False)
    assert rep["summary"]["E"] == {"ok": 7, "of": 7} and not rep["warnings"]


# ---------------------------------------------------------------- applies

def test_applies_role_groups_every_item_by_level_strongest_first(std):
    out = query.applies(query.Atlas(std), DOC, role="Hub")
    assert out.startswith(f"src: {DOC} applicability records cells=16 query=\"role='Hub'\"")
    assert _body(out) == (
        "## Normative (3)\n"
        "- Widget_Query (message) (Table 3-1 p.3)\n"
        "- Link Reset (sequence, as initiator) (Table 3-2 p.4)\n"
        "- Link Reset (sequence, as responder) (Table 3-2 p.4)\n\n"
        "## Recommended (1)\n"
        "- Widget_Reply (message) (Table 3-1 p.3)\n\n"
        "## Optional (1)\n"
        "- Fault_Notice (message, if is a bridge) (Table 3-1 p.3)\n\n"
        "## Shall Not (1)\n"
        "- Widget Swap (sequence, as initiator) (Table 3-2 p.4)\n\n"
        "## N/A (2)\n"
        "- Fault_Notice (message) (Table 3-1 p.3)\n"
        "- Widget Swap (sequence, as responder) (Table 3-2 p.4)\n")


def test_applies_cond_keeps_that_condition_and_the_unconditional_cells(std):
    a = query.Atlas(std)
    assert "if is a bridge" in query.applies(a, DOC, role="hub", cond="bridge")      # role case is not significant
    faults = query.applies(a, DOC, role="Hub", cond="faults")                        # another role's condition
    assert "if is a bridge" not in faults and "- Fault_Notice (message) (Table 3-1 p.3)" in faults
    node = query.applies(a, DOC, role="Node", cond="reports faults")
    assert "## Normative (3)\n- Fault_Notice (message, if reports faults)" in node
    miss = query.applies(a, DOC, role="Node", cond="extended range")
    assert miss.startswith("src: none") and "Conditions: " in miss and "reports faults" in miss


def test_applies_item_lists_its_level_for_every_role(std):
    out = query.applies(query.Atlas(std), DOC, item="link reset")
    assert _body(out) == ("## Link Reset (sequence)\n"
                          "- Hub as initiator: Normative (Table 3-2 p.4)\n"
                          "- Hub as responder: Normative (Table 3-2 p.4)\n"
                          "- Node as initiator: Optional (Table 3-2 p.4)\n"
                          "- Node as responder: Normative (Table 3-2 p.4)\n")
    both = query.applies(query.Atlas(std), DOC, item="Fault_Notice", role="Node")
    assert "- Node: Optional (Table 3-1 p.3)" in both and "- Node if reports faults: Normative" in both
    assert "Hub" not in _body(both)


def test_applies_misses_list_the_nearest_names_and_the_overview_lists_roles(std):
    a = query.Atlas(std)
    miss = query.applies(a, DOC, item="Widget_Qurey")
    assert miss.startswith("src: none") and "Nearest items: Widget_Query" in miss
    assert "Nearest roles: Node" in query.applies(a, DOC, role="Nod")
    over = _body(query.applies(a, DOC))
    assert over.startswith("Roles: Hub (8 cells), Node (8 cells)\nConditions: is a bridge (1 cells), "
                           "reports faults (1 cells)\nTables: Table 3-1 (p.3), Table 3-2 (p.4)\n"
                           "Items: 5 (3 messages, 2 sequences)\n")
    assert "Pass role=<role>" in over


def test_applies_on_a_document_with_no_cells_says_where_the_tables_are(std):
    d = std / "docs" / DOC
    (d / "records" / "applicability.jsonl").unlink()
    out = query.applies(query.Atlas(std), DOC, role="Hub")
    assert out.startswith("src: none") and f"find_text doc={DOC}" in out and "/atlas-translate-docs standard" in out


def test_an_unverified_cell_says_so(std):
    d = std / "docs" / DOC
    write_jsonl(d / "records" / "applicability.jsonl", [dict(cells()[0], prov="llm-unverified")])
    assert "- Widget_Query (message) (Table 3-1 p.3) (unverified)" in query.applies(query.Atlas(std), DOC, role="Hub")


# ---------------------------------------------------------------- transitions

def test_transitions_state_gives_entry_actions_exits_and_the_states_that_lead_to_it(std):
    out = query.transitions(query.Atlas(std), DOC, state="Query_Sent")
    assert out.startswith(f"src: {DOC} state machines states=6 transitions=7 query=\"state='Query_Sent'\"")
    assert _body(out) == (
        "## Demo Hub Port: Query_Sent (p.6)\n"
        "On entry:\n"
        "- Send Widget_Query Message\n"
        "- Start ReplyWaitTimer\n"
        "Exits:\n"
        "- to Ready on a reply is received [trigger Widget_Reply] (p.6)\n"
        "- to Fault on the ReplyWaitTimer times out (p.6)\n"
        "Entered from:\n"
        "- Idle on the link comes up (p.6)\n")


def test_a_state_two_machines_share_asks_for_the_machine(std):
    a = query.Atlas(std)
    out = query.transitions(a, DOC, state="Idle")
    assert _body(out) == ("2 machines have a state named 'Idle'; pass one as `machine`:\n"
                          "- Demo Hub Port (p.6)\n- Demo Node Port (p.7)\n")
    hub = _body(query.transitions(a, DOC, state="Idle", machine="hub"))
    assert hub.startswith("## Demo Hub Port: Idle (p.6)\nOn entry:\n- Reset QueryCounter\nExits:\n"
                          "- to Query_Sent on the link comes up (p.6)\nEntered from:\n")
    assert "- Ready on the link goes down (p.7)" in hub and "- Fault on the fault is cleared (p.7)" in hub
    assert "Answering" not in hub
    assert "2 machines match 'demo'" in query.transitions(a, DOC, state="Idle", machine="demo")
    assert "Machines: Demo Hub Port, Demo Node Port" in query.transitions(a, DOC, machine="bridge")


def test_transitions_on_a_timer_and_on_a_message(std):
    a = query.Atlas(std)
    assert _body(query.transitions(a, DOC, on="ReplyWaitTimer")) == (
        "## Demo Hub Port\n"
        "Transitions:\n"
        "- Query_Sent -> Fault on the ReplyWaitTimer times out (p.6)\n"
        "Entry actions:\n"
        "- Query_Sent: Start ReplyWaitTimer (p.6)\n"
        "- Ready: Stop ReplyWaitTimer (p.7)\n")
    msg = _body(query.transitions(a, DOC, on="widget_query"))
    assert msg == ("## Demo Hub Port\n"
                   "Entry actions:\n"
                   "- Query_Sent: Send Widget_Query Message (p.6)\n\n"
                   "## Demo Node Port\n"
                   "Transitions:\n"
                   "- Idle -> Answering on a Widget_Query Message is received (p.7)\n")
    by_trigger = _body(query.transitions(a, DOC, on="Widget_Reply"))           # the event does not name it
    assert "- Query_Sent -> Ready on a reply is received [trigger Widget_Reply] (p.6)" in by_trigger
    one = _body(query.transitions(a, DOC, state="Query_Sent", on="ReplyWaitTimer"))
    assert "- Start ReplyWaitTimer" in one and "Send Widget_Query" not in one and "to Ready" not in one
    assert "Entered from:\n- (none recorded naming ReplyWaitTimer)" in one


def test_transitions_misses_list_the_nearest_names(std):
    a = query.Atlas(std)
    miss = query.transitions(a, DOC, state="Query_Snt")
    assert miss.startswith("src: none") and "Nearest states:\n- Query_Sent (Demo Hub Port)" in miss
    timer = query.transitions(a, DOC, on="ReplyWait")                          # a part of a name is not the name
    assert timer.startswith("src: none") and "Nearest messages and timers: ReplyWaitTimer" in timer


def test_transitions_overview_lists_each_machine_its_states_and_its_figure(std):
    out = _body(query.transitions(query.Atlas(std), DOC))
    assert out.startswith("## Demo Hub Port (4 states, 5 transitions; Figure 5-1 p.5)\n"
                          "Idle (p.6), Query_Sent (p.6), Fault (p.7), Ready (p.7)\n\n"
                          "## Demo Node Port (2 states, 2 transitions)\n"
                          "Answering (p.7), Idle (p.7)\n")


def test_transitions_on_a_document_with_no_machine_says_where_to_look(std):
    d = std / "docs" / DOC
    (d / "records" / "state.jsonl").unlink()
    out = query.transitions(query.Atlas(std), DOC, state="Idle")
    assert out.startswith("src: none") and "state machine records of" in out and f"doc_toc doc={DOC}" in out


# ---------------------------------------------------------------- doors: paging, CLI, MCP

def test_both_lookups_page_to_the_uncut_answer(std, monkeypatch):
    from paging_synth import assert_exact

    a = query.Atlas(std)
    assert_exact(monkeypatch, "applies", lambda o: query.applies(a, DOC, role="Node", offset=o))
    assert_exact(monkeypatch, "transitions", lambda o: query.transitions(a, DOC, offset=o))


def test_q_verbs_answer_like_the_functions(std, capsys):
    assert cli.main(["--atlas-dir", str(std), "q", "applies", DOC, "--item", "Widget Swap"]) == 0
    assert "- Hub as initiator: Shall Not (Table 3-2 p.4)" in capsys.readouterr().out
    assert cli.main(["--atlas-dir", str(std), "q", "transitions", DOC, "--state", "Idle", "--machine", "node"]) == 0
    assert "## Demo Node Port: Idle (p.7)" in capsys.readouterr().out


def test_mcp_tools_answer(std):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    server = build_server(std, live=False)
    for tool, args, want in (("applies", {"doc": DOC, "role": "Node"}, "Widget_Reply (message)"),
                             ("transitions", {"doc": DOC, "on": "ReplyWaitTimer"}, "Query_Sent -> Fault")):
        res = asyncio.run(server.call_tool(tool, args))
        assert want in json.dumps(res.model_dump() if hasattr(res, "model_dump") else res, default=str), tool


def test_the_procedure_examples_are_valid_records():
    text = (Path(__file__).parents[1] / "skills" / "atlas-translate-docs" / "reference" / "standard-procedure.md").read_text()
    blocks = [b.split("```", 1)[0] for b in text.split("```json\n")[1:]]
    dec, recs = json.JSONDecoder(), []
    for b in blocks:
        i = 0
        while b[i:].strip():
            i += len(b[i:]) - len(b[i:].lstrip())
            rec, i = dec.raw_decode(b, i)
            recs.append(rec)
    assert {r["kind"] for r in recs} == {"applicability", "state", "transition"}
    assert all(R.validate(r) == [] for r in recs), [R.validate(r) for r in recs]
    for r in recs:                                     # the examples quote this test's invented pages
        page = normalize(PAGES[r["p"]])
        assert normalize(r["q"]) in page, r["id"]
    appl = next(r for r in recs if r["kind"] == "applicability")
    assert check.applicability_check(appl) == "ok"


# ---------------------------------------------------------------- render

def test_render_tables_the_three_kinds_and_check_m_anchors_them(std):
    d = std / "docs" / DOC
    write_text(d / "s" / "3.md", "# Applicability\n\n<!-- atlas:table kind=applicability p=3 -->\n<!-- atlas:end -->\n\n"
                                 "<!-- atlas:table kind=applicability p=4 role=Node -->\n<!-- atlas:end -->\n")
    write_text(d / "s" / "5.md", "# State Machines\n\n<!-- atlas:table kind=state -->\n<!-- atlas:end -->\n\n"
                                 "<!-- atlas:table kind=transition p=6 -->\n<!-- atlas:end -->\n")
    render.render_doc(d)
    s3 = (d / "s" / "3.md").read_text()
    assert "| Item | Kind | Role | Condition | Table | Level | Note | p |" in s3
    assert "| Fault_Notice | message | Node | reports faults | Table 3-1 | Normative |  | p.3 |" in s3
    assert "| Item | Kind | Role | As | Table | Level | Note | p |" in s3          # p=4 cells have `as`, no cond
    s5 = (d / "s" / "5.md").read_text()
    assert "| Machine | State | On entry | Note | p |" in s5
    assert "| Demo Hub Port | Query_Sent | Send Widget_Query Message; Start ReplyWaitTimer |  | p.6 |" in s5
    assert "| From | To | Event | Trigger | Note | p |" in s5                    # one machine: no Machine column
    assert "| Query_Sent | Fault | the ReplyWaitTimer times out | ReplyWaitTimer |  | p.6 |" in s5
    facts = (d / "facts.txt").read_text()
    assert "kind=state; machine=Demo Hub Port; name=Ready; entry=Stop ReplyWaitTimer" in facts
    rep = check.run_check(d, write=False, use_pdf=False)
    assert not [m for m in rep["M"] if m["stats"]["unanchored"]], [m["unanchored"] for m in rep["M"]]
    assert rep["summary"]["exit"] == 0, rep["summary"]
    assert read_json(d / "doc.json")["kind"] == "standard"
