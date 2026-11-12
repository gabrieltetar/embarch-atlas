# Distil a standard or specification (lazily)

A standard (a bus, protocol or power specification, an IEC/IEEE/JEDEC standard) is hundreds of
pages, and a firmware project implements a few of its sections. So, as for the reference manual,
only the sections someone asks for are distilled; the rest stays readable through `toc.md` and the
page text.

Output, all under `docs/<std_doc_id>/`:

| File | Who writes it |
|---|---|
| `doc.json`, `pages/`, `toc.md`, `toc.json` | `doc prep` |
| `worklist.json` | `doc sections <doc_id> --add ...`: the sections asked for, why, pages, content key, status |
| `card.md` | you, on the first pass: what the standard covers, its roles, the revision; the list of distilled sections is a block `doc sections` and `doc render` write, do not edit it |
| `records/requirement.jsonl` | you: the normative statements of the sections asked for |
| `records/limit.jsonl`, `records/timing.jsonl` | you: the parameter tables of those sections, as in a datasheet |
| `records/quirk.jsonl`, `records/figure.jsonl` | you: statements that take back another, state machines and sequence figures |
| `records/applicability.jsonl` | you: one record per cell of an applicability table (below) |
| `records/state.jsonl` | you: the `state` and `transition` records of a state machine written one subsection per state (below) |
| `s/<secid>.md` | you: one tier-2 file per section asked for, at most 4K tokens |

The distillation is **board-independent**, like a datasheet's: no board, no part number, no
"this firmware does X".

## Steps

```
- [ ] 1 prep      embarch-atlas doc prep <pdf> --kind standard   (when inventory did not classify it)
- [ ] 2 card      read the front matter and toc.md only; write card.md (scope, roles, revision, distilled: none yet)
- [ ] 3 ask       which sections does the firmware implement? ASK the engineer, or take the ones a session asked for;
                  `embarch-atlas doc sections <doc_id> --suggest` ranks the sections sessions' lookups landed in;
                  embarch-atlas doc sections <doc_id> --add <sec>[,<sec>...] --why "<reason>"
- [ ] 4 read      per section with status todo: its pages' text; image Reads only for figures and mangled tables
- [ ] 5 write     records (a fw_check on each rule a configuration value can break, below), then s/<secid>.md;
                  update the card's list of distilled sections
- [ ] 6 check     embarch-atlas doc render, doc check (repeat until exit 0 or only llm-unverified remain)
- [ ] 7 freeze    embarch-atlas doc freeze <doc_id> --model <model>; doc sections <doc_id> shows the sections frozen
```

## Records

- **requirement** is the main kind: one per normative statement (a sentence with Shall, Should,
  May, Shall Not, Should Not, as the standard capitalises them).
  - `id`: `<std>.req.<section>.<subject>`, `rid`: `<std>.<section>.<subject>`; never an ordinal.
  - `q`: the sentence, verbatim (a bullet's sentence, not the bullet's lead-in alone).
  - `modality`: the keyword as printed: `shall`, `shall-not`, `should`, `should-not`, `may`.
    `doc check` W verifies it against the quote.
  - `role`: whom it binds, as the standard names it (`Source`, `Sink`, `DRP`, `Cable`), when it says.
  - `cond`: when it applies (a mode, a state, a revision), as printed.
  - `fw_relevant`: true when firmware enforces, measures or reports it.
  - `text`: your one-line statement. Its numbers must be in `q`. A requirement read from a table row
    whose `text` names a column header's values quotes the header line as a second `q` part.
- **limit / timing**: parameter tables, one record per row and condition, as in
  `datasheet-procedure.md` (group `ec` for electrical parameters, so the column check K runs).
- **quirk**: a statement that takes back or narrows another (class `text-vs-text`, `narrow-condition`),
  a deprecated feature kept for compatibility (`variant`).
- **figure**: a state machine or a message sequence that a requirement depends on (`src: figure`). A state
  diagram also takes `machine` and `states` (below).
- **applicability**, **state**, **transition**: the two shapes below. Each replaces a pile of requirements
  that would say the same thing less exactly.

## Applicability tables

A table that lists sequences or messages down the side, roles across the top and a level in each cell
(Normative, Recommended, Optional, Shall Not, N/A) is **one `applicability` record per cell**, not one
requirement per row. The `applies` lookup answers "what must a Hub support" from these.

```
- [ ] a  read the table's pages; an image Read when the text layer loses the column alignment
- [ ] b  per row, per role column (per initiator/responder column when the table splits a role): one record
- [ ] c  a footnote or a column heading that adds a condition: `cond` as printed, and the footnote or
         heading line as a second `q` part; the unconditional cell stays its own record
- [ ] d  doc check: L wants the item and the level in the quoted row
```

- `id`: `<std>.appl.<table>.<item-slug>.<role>[.<as>][.<cond-slug>]`.
- `item`, `level`: as printed, both in `q` (check L). `level` is the cell's word, never a synonym:
  Recommended is not Required.
- `item_kind`: `sequence` or `message`. `role`: the column heading as printed. `as`: `initiator` or
  `responder` when the table splits the role. `table`: the caption's number.
- `q`: the row as the text layer prints it. L cannot see columns: read the level from the right column, and
  Read the page image when the row's cells are not in column order.

Invented example. Table 3-2 of a demo link spec has a row `Link Reset  Normative  Normative  Optional
Normative` under the headings Hub Initiator, Hub Responder, Node Initiator, Node Responder. Its Node Initiator
cell is:

```json
{"id": "demolink.appl.3-2.link-reset.node.initiator", "kind": "applicability", "doc": "demolink-spec@r1.0",
 "p": 4, "src": "table", "q": "Link Reset Normative Normative Optional Normative", "item": "Link Reset",
 "item_kind": "sequence", "role": "Node", "as": "initiator", "level": "Optional", "table": "Table 3-2"}
```

A footnote `(1) Normative for a Node that reports faults.` on a `Fault_Notice ... Optional (1)` row gives two
records: the Optional one, and a Normative one with `cond: "reports faults"` and
`q: [<row>, "Normative for a Node that reports faults"]`.

## State sections

A state machine that the standard (or a datasheet: a charger's, a peripheral's) writes **one subsection per
state**, with a state diagram figure, is one `state` record per state, one `transition` record per exit, and
the diagram as a `figure` record. The `transitions` lookup answers a state's entry actions, exits and
predecessors, and which states react to a message or a timer.

```
- [ ] a  the diagram: a figure record with `machine` (one name for the machine, used on all its records) and
         `states` (every state it draws, as printed); an image Read of the figure page
- [ ] b  per state subsection: a state record; `entry` holds what it does on entry, one action each, as printed
         (a message sent, a timer started or stopped, a counter reset); `q` is the sentence that says it
- [ ] c  per exit the subsection gives: a transition record; `event` the event or condition as printed,
         `trigger` the message or timer that fires it (none for a condition alone); `q` the exit's sentence
- [ ] d  doc check: E wants every transition's `from` and `to`, and every state on the figure, to be a state
         record of the same machine; a state record missing from the figure is a warning to look at
```

- `id`: `<std>.state.<machine-slug>.<name>`, `<std>.trans.<machine-slug>.<from>.<to>[.<event-slug>]` (the event
  slug when two exits join the same pair of states).
- Names as printed, the same string on every record: a transition naming `Query_Sent` needs a state record
  named `Query_Sent`, not `Query Sent`.
- Two machines may share a state name (each has its own `Idle`): `machine` keeps them apart, and the lookup
  asks for it.

Invented example. A demo link spec's section 5.2.2 says "On entry to the Query_Sent state the Hub shall send a
Widget_Query Message and start the ReplyWaitTimer", then "The Hub shall transition to the Fault state when the
ReplyWaitTimer times out":

```json
{"id": "demolink.state.demo-hub-port.Query_Sent", "kind": "state", "doc": "demolink-spec@r1.0", "p": 6,
 "src": "text", "q": "On entry to the Query_Sent state the Hub shall send a Widget_Query Message and start the ReplyWaitTimer",
 "machine": "Demo Hub Port", "name": "Query_Sent", "entry": ["Send Widget_Query Message", "Start ReplyWaitTimer"]}
{"id": "demolink.trans.demo-hub-port.Query_Sent.Fault", "kind": "transition", "doc": "demolink-spec@r1.0", "p": 6,
 "src": "text", "q": "The Hub shall transition to the Fault state when the ReplyWaitTimer times out",
 "machine": "Demo Hub Port", "from": "Query_Sent", "to": "Fault", "event": "the ReplyWaitTimer times out",
 "trigger": "ReplyWaitTimer"}
```

A `<!-- atlas:table kind=state -->` or `kind=transition` block in `s/<secid>.md` renders them, as does
`kind=applicability` for the cells.

## Rules a configuration value can break (`fw_check`)

A requirement whose force lands on a devicetree property or a Kconfig symbol gets a `fw_check`, and `join`
evaluates it against the build (DESIGN.md "Firmware configuration against a standard"). Put it on the
requirement when one statement is one rule; when a rule encodes a table of requirements (a power-rules table, one
row each), write a `check` record with the `fw_check` and `refs` naming those requirement ids.

- **Stay board-independent.** Name the devicetree by binding, never by a board's node label: `dt` is a
  compatible (`usb-c-connector`) and `prop` a binding property (`source-pdos`). Kconfig is named by the
  subsystem's symbol. A value the standard's rule needs and the firmware does not state (a port's maximum power)
  is `{"declared": "<key>"}`; the engineer declares it, and which node is which port, in `atlas.toml`.
- **`expr` is true when the configuration complies**, for a Shall Not as for a Shall. `applies_when` holds the
  condition the requirement's `cond` or `role` states (`CONFIG_<SYM> == 'y'`): false skips the rule, which is
  folded as expected, never a failure.
- **Decode before comparing.** `"decode": "pd_pdo"` turns each Source_Capabilities word into `type`, `mv`, `ma`,
  `min_mv`, `max_mv`, `pdp_w` ...; `pd_sink_pdo` reads Sink_Capabilities words.
- The numbers in an expression are not checked against the page yet: copy them from the quote, digit for digit.

Worked example, on an invented "Demo PD" standard. **Every value below is invented, not the standard's**: read the
real ones from the cited page.

1. Fixed supplies against the power rule for the declared power (a `check` naming the table's rows):

   ```json
   {"id": "demopd.check.fixed_at_pdp", "kind": "check", "doc": "demo-pd@r1.0", "p": 41, "q": null, "src": "table",
    "refs": ["demopd.req.10.2.fixed-5v", "demopd.req.10.2.fixed-above-5v"],
    "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"},
                            "pdp_w": {"declared": "pdp_w"}},
                 "expr": "all(p.ma <= min(2500, pdp_w * 1000000 // p.mv) for p in src if p.type == 'fixed')"}}
   ```

2. PPS and AVS APDO ranges against theirs (on the requirement):

   ```json
   "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"}},
                "expr": "all(3000 <= p.min_mv and p.max_mv <= 12500 for p in src if p.type == 'pps') and all(p.pdp_w <= 90 for p in src if p.type == 'epr-avs')"}
   ```

3. The first Source_Capabilities' 5 V current against the Rp current the port advertises:

   ```json
   "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"},
                           "rp": {"dt": "usb-c-connector", "prop": "typec-power-opmode"}},
                "applies_when": "CONFIG_USBC_CSM_SOURCE_ONLY == 'y'",
                "expr": "src[0].ma <= {'default': 400, '1.5A': 1200, '3.0A': 2600}[rp]"}
   ```

4. A role chosen in Kconfig against the sequences the standard makes normative for that role needs records saying
   which sequences apply to which role; until those exist, write the requirement without a `fw_check`.

The engineer declares, in `atlas.toml` (a port's value overrides the standard's):

```toml
[standards."demo-pd"]
pdp_w = 30
[standards."demo-pd".ports.left]
node = "usbc_left"
pdp_w = 15
```

`join` then lists each rule in `checks.md` ("Firmware configuration vs standards"), and
`mismatch_report cls=config-vs-standard` gives each failure with the requirement's page and the file:line that set
the value; `mismatch_report build=<dir>` runs the rules on a branch build without rebuilding the atlas.

## When a session asks

`atlas-use` reads an undistilled section from `toc.md` and the pages directly, and suggests
`/atlas-translate-docs standard <doc_id> <section>`. That runs steps 3-7 for the section only;
frozen sections are never redone (key: section id + page-text hashes + procedure version).
