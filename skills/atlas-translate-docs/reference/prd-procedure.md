# Distil a PRD (product requirements document)

Output: `docs/<prd_doc_id>/records/requirement.jsonl`, `card.md`, `s/<secid>.md`.

1. `embarch-atlas doc prep <pdf>`. PRDs exported from word processors are often
   image-heavy (figures, tables pasted as pictures). Read the page classes in
   `toc.md` and plan image Reads for the image-heavy pages only.
2. Walk the outline section by section. For every statement that constrains the
   product, write one `requirement` record:
   - `rid`: a stable id derived from the section number and a slug of the
     statement's subject (`prd.4.2.charge-temp-range`), never an ordinal, so it
     survives edits of the document;
   - `text`: your normalized one-line statement;
   - `q`: the **verbatim** sentence from the text layer (null only when the
     statement is in an image; then `src: figure`);
   - `p`: its page;
   - `fw_relevant`: true when firmware could enforce, measure or report it (limits,
     timing, states, user-visible behaviour, protocols), false for mechanical,
     cosmetic or business statements.
3. Numbers and units in `text` must appear in `q` (the checker verifies it).
4. `card.md`: the product in 10–15 bullets, the firmware-relevant requirement
   count per section, and the requirements that disagree with each other (as
   `conflict` records too).
5. **Do not link requirements to code here.** Proposed trace links are the
   engineer's to confirm, and they are made at build time, where the firmware is
   known. LLM trace-link recovery scores poorly in the literature (F1 0.24–0.53),
   so a link is only ever a proposal.
