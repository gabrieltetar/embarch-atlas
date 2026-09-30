# Distil a PRD (product requirements document)

Output: `docs/<prd_doc_id>/records/requirement.jsonl`, `card.md`, `s/<secid>.md`.

1. `embarch-atlas doc prep <pdf>`. PRDs exported from word processors are often
   image-heavy (figures, tables pasted as pictures). Read the page classes in
   `toc.md` and plan image Reads for the image-heavy pages only.
2. Walk the outline section by section. For every statement that constrains the
   product, write one `requirement` record:
   - `id`: `<prd-part>.req.<section>.<subject>`, and `rid`: `prd.<section>.<subject>`,
     where `<section>` is the section number (`4.2`) or, for a document with
     unnumbered headings, a slug of the heading's first 3-5 words with parentheticals
     and notes dropped (`pricing`, not `pricing-note-the-pricing-below-is-not-final`),
     and `<subject>` a slug of what the statement constrains
     (`prd.4.2.charge-temp-range`). Never an ordinal, so it survives edits;
   - `text`: your normalized one-line statement;
   - `q`: the **verbatim** sentence from the text layer (null only when the
     statement is in an image; then `src: figure`);
   - `p`: its page;
   - `fw_relevant`: true when firmware could enforce, measure or report it (limits,
     timing, states, user-visible behaviour, protocols), false for mechanical,
     cosmetic or business statements;
   - `modality`: `shall` (must, will, required), `should`, `may`, or `nice-to-have`,
     as the document words it; `priority` / `order` for ranked lists (a fallback
     order, an allocation order). Proposals ("Solutions", "Details") are requirements
     only when the document adopts them; otherwise they are `note`s.
3. Numbers and units in `text` must appear in `q` (the checker verifies it).
   **Also Read any figure the text points to as the definition of a requirement**
   ("defined in the illustration below"), even on a page `doc prep` did not flag.
   A figure that only illustrates is a `figure` record; a figure that states a
   requirement gives a `requirement` with `src: figure`, `q: null`, which stays
   `llm-unverified` until the second look. Candidate tables (a selection matrix)
   are not requirements; the chosen row is, when the document says it is chosen.
4. `card.md`: the product in 10–15 bullets, the firmware-relevant requirement
   count per section (a count you computed: tag it `(derived)`), and the
   requirements that disagree with each other, as `conflict` records too. A
   conflict with a figure on one side is `src: figure`. A conflict proves only that
   both quotes exist, not that they disagree, so it stays `llm-unverified` until the
   second look confirms the reading.
5. `embarch-atlas doc render`, `doc check`, `doc freeze`, as for a datasheet.
6. **Do not link requirements to code here.** Proposed trace links are the
   engineer's to confirm, and they are made at build time, where the firmware is
   known. LLM trace-link recovery scores poorly in the literature (F1 0.24–0.53),
   so a link is only ever a proposal.
