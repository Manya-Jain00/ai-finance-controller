# Bring your own data — AI-assisted onboarding

Not one of the graded phases. Built after a direct question: *the agent works
on data we generated — would it work on data a real company hands us, with
different column names?* The honest answer was "the reasoning would, the
loaders wouldn't" — so this closes that gap for the loader side.

## The idea

`tools/loaders.py` only reads files shaped exactly like this project's own
CSVs — it looks for a column literally called `remittance_info`. A different
bank or gateway export uses different headers for the same information.
Traditionally that means an engineer opens the file and writes a loader for
it, by hand, per source, forever.

`onboard/` replaces that with one AI-assisted step: show the model a file's
headers and a few sample rows, ask it to map them onto the same `Payment` /
`Invoice` fields every other part of this project already uses, a human
confirms the mapping once, and everything from there is exactly as
deterministic as the hand-written loaders.

```
  a new CSV, any column names
        │
        ▼
  onboard.mapper.propose_mapping   ← the ONLY step that calls the LLM;
        │                            it only ever sees headers + a few sample rows
        ▼
  a human looks at the mapping and confirms it
        │
        ▼
  onboard.mapper.apply_mapping     ← pure Python, no LLM, deterministic
        │
        ▼
  tools.loaders.Payment / Invoice  ← the SAME objects the rest of the
        │                            project already knows how to use
        ▼
  agent / eval / monitor / qa      ← unmodified
```

**The boundary that matters for a finance tool:** the AI is in the loop
exactly once, for the low-stakes question "which column is which" — never for
reading an actual amount. Once a mapping is confirmed, reading a thousand rows
with it is no riskier than the existing loaders.

## What's in `onboard/`

| File | Role |
|---|---|
| `schema.py` | The target field catalog for `payment` and `invoice` — deliberately the exact fields `tools.loaders.Payment`/`Invoice` already have, so a mapped file needs no further translation. `ColumnMapping` / `FieldGuess` dataclasses. |
| `mapper.py` | `propose_mapping()` — one Gemini tool-call asking the model to map headers → target fields (same throttle/retry pattern as `agent/reconciler.py`). `apply_mapping()` — pure Python; refuses (raises `MappingError`, naming exactly which fields) rather than guessing if a required field was never mapped. |
| `cli.py` | `python -m onboard.cli file.csv --kind payment` — dry-run preview of the mapping; add `--apply` to build the real objects once it looks right. |
| `demo.py` | The end-to-end proof (below). |
| `demo_foreign_bank.csv` | A 4-row file in a made-up bank's format, used by the demo and the tests. |
| `test_onboard.py` | 9 offline tests — `apply_mapping` directly, and `propose_mapping` against a scripted fake client (same pattern as `agent/test_agent.py`). |

## The demo — `python -m onboard.demo`

Feeds `onboard/demo_foreign_bank.csv` — columns `txn_ref, posting_date,
debit_amt, narration, payer`, a shape nothing in this project generated —
through the real pipeline:

1. `propose_mapping` maps it onto `Payment`.
2. `apply_mapping` builds real `Payment` objects.
3. Those objects go straight into `agent.reconciler.Reconciler` — **the exact
   class that resolved the graded 130-payment batch, unmodified.**

Three of the four rows reference real, still-open invoices from
`data/invoices.csv` (INV0005, INV0007, INV0008); the fourth has no invoice
behind it, on purpose, to check the agent doesn't force a match. Transcript:
[`../onboard/demo_output.txt`](../onboard/demo_output.txt).

## Reproduce

```bash
python -m onboard.cli onboard/demo_foreign_bank.csv --kind payment          # dry run
python -m onboard.cli onboard/demo_foreign_bank.csv --kind payment --apply  # build the objects
python -m onboard.demo                                                      # full pipeline
python -m unittest onboard.test_onboard                                     # 9 offline tests
```

## Honest limits

- This closes the **loader** gap. It does not touch the reference-parsing
  regex, the fee-band constant, or the prompt's calibrated ranges — those stay
  tuned to this project's own data by design (see `docs/architecture.md`,
  "the honest caveat"). Changing those risks the graded 129/130 number for no
  benefit before submission, so they were deliberately left alone.
- It maps a **new column layout**, not a **new business reality** — a
  company's real fee percentage or a truly novel payment type still needs a
  person to say what "normal" looks like for their data.
- No ground truth exists for a company's real payments, so there is nothing
  for this to be graded against — the same limit `docs/architecture.md`
  already describes for the base pipeline.
