# Architecture

An LLM tool-use **agent** reconciles messy, multi-source incoming payments
against an open-invoice ledger. Every decision it makes — a single-invoice match,
a multi-invoice combination, a partial payment, a fee-deducted payment, or an
honest exception — is written to one append-only **audit trail** with a
confidence score and stated reasoning. Three layers read that trail: an
**evaluator** that grades it against a hidden answer key, a **live monitor** that
watches for the agent degrading mid-run, and a **Q&A layer** that answers
natural-language questions about it.

Built for **Razorpay AI Buildathon, Track 04 — AI Finance Controller.** Scoped to
multi-source reconciliation + settlement Q&A; forecasting and tax-matching are
out ([`scope.md`](scope.md)).

---

## The pipeline

```
 DATA  (Phase 1)  ── data/generate_data.py, seeded (random.seed(42)) ────────────────┐
                                                                                     │
   bank_wire_transactions.csv     transaction_id, value_date, amount,                 │  130 payments
                                  remittance_info, sender_name                        │  180 invoices
   gateway_settlements.csv        settlement_id, txn_date, gross_amount, fee,          │  5 mess types
                                  net_amount, payer_reference, payer_email            │  10 planted orphans
   invoices.csv                   invoice_id, customer_id, customer_name, ...          │
   ground_truth.json  (HIDDEN — read only by eval/evaluate.py) ◄───────────────────────┘
        │
        │  tools/loaders.py  normalises the two schemas → Payment / Invoice dataclasses
        ▼
 TOOLS  (Phase 2)  ── tools/reconciliation_tools.py — 5 pure functions, no LLM ────────
   parse_remittance_reference · find_invoices_by_amount · find_invoices_by_customer
   check_combination · check_fee_schedule
        │
        ▼
 PRE-FLIGHT  (Phase 4)  ── agent/preflight.py ────────────────────────────────────────
   runs those same tools deterministically in plain Python and hands the agent a
   first-pass *suggestion* — so a clean payment resolves in one model call, not five
        │
        ▼
 AGENT  (Phase 3)  ── agent/reconciler.py — Gemini tool-use loop, one payment at a time
   · decides which tool(s) to call, reads the results, decides again (max 12 turns)
   · must try a 2nd distinct strategy before it is allowed to return an exception
   · finishes with submit_resolution: match_type + invoice_ids + confidence + reasoning
        │
        ▼
 ══════════════════════════════════════════════════════════════════════════════════════
   agent/decision_log.jsonl   —   THE AUDIT TRAIL   —   one DecisionRecord per payment
 ══════════════════════════════════════════════════════════════════════════════════════
        │                              │                                │
        ▼                              ▼                                ▼
 EVAL  (Phase 4)              MONITOR  (Phase 5)                 Q&A  (Phase 6)
 eval/evaluate.py             monitor/tracker.py + alerts.py     qa/chat.py
 compares each decision to    sliding-window health signals      natural-language questions
 ground_truth.json            (match rate, confidence, ...)      answered from the log via
 → eval/metrics.json          + 6 alert rules with hysteresis    4 read-only query tools
 match rate · accuracy ·      → fires when the agent degrades    → never re-runs the agent,
 throughput · exception       live, with no answer key           never reads ground truth
 quality
```

The agent **never sees `ground_truth.json`**. Only `eval/evaluate.py` opens it,
and only to score. The pre-flight, the monitor, and the Q&A layer never touch it
either.

---

## Components

| Component | Files | What it does |
|---|---|---|
| **Data** | `data/generate_data.py`, `data/*.csv`, `data/ground_truth.json` | Ground-truth-first generator: it decides each payment's correct answer, records it in `ground_truth.json`, **then** layers the mess on top (shorthand refs, gateway fees, partials, combined sums, timing offsets) and plants 10 unmatchable orphans. 130 payments across two deliberately mismatched schemas. Seeded, so it reproduces exactly. |
| **Tools** | `tools/reconciliation_tools.py`, `tools/loaders.py` | The agent's five "hands", each a plain testable function: decode a remittance reference (`INV0042`, `INV0002+INV0044`, `INV0077-0079`, null tokens), find invoices by amount / by customer, test whether a subset of invoices sums to a target, and decide whether a shortfall is a standard 1.5–3% gateway fee or a genuine partial. 39 unit tests. |
| **Pre-flight** | `agent/preflight.py` | Runs the tools deterministically before the agent starts and passes the raw results + a suggested resolution into the agent's first prompt. Cuts model calls from 3.89 to 2.45 per payment. Same tools, no LLM, no answer key. |
| **Agent** | `agent/reconciler.py`, `agent/tools_bridge.py`, `agent/prompts.py`, `agent/schema.py` | A manual Gemini tool-use loop (automatic function calling disabled so the loop can enforce its own rules). Retry logic: a low-confidence or exception verdict is rejected until a second, materially different strategy has been tried. Every resolution is a structured `DecisionRecord`. |
| **Audit trail** | `agent/decision_log.py`, `agent/decision_log.jsonl` | JSONL, one record per payment, appended the moment each finishes. Holds the inputs the agent saw, the pre-flight, every tool call and its result verbatim, the resolution (type, invoices, confidence, reasoning), and run metadata. This is the single source of truth for Phases 4–6. |
| **Eval** | `eval/run_batch.py`, `eval/evaluate.py`, `eval/metrics.json` | `run_batch` runs all 130 payments (resumable — a killed run loses nothing). `evaluate` is the only ground-truth reader: it computes match rate, accuracy, throughput and exception quality, and prints every wrong answer with the agent's own reasoning. |
| **Monitor** | `monitor/tracker.py`, `monitor/alerts.py`, `monitor/live.py`, `monitor/demo.py` | A sliding window (last 20 records) over operational signals the agent produces about *itself* — match rate, mean confidence, exception rate, forced-retry rate, pre-flight-disagreement rate. Six alert rules with hysteresis (trip ≠ clear). Demonstrated firing on an injected "bad batch" and then clearing. Reads no ground truth — live, there is no answer key. |
| **Q&A** | `qa/store.py`, `qa/query_tools.py`, `qa/assistant.py`, `qa/chat.py` | A thin conversational layer: a question goes through a Gemini loop with four read-only query tools over the decision log (`get_payment`, `search_payments`, `summarize_batch`, `group_by`) and comes back grounded in the log, citing payment ids and numbers. Does not re-run the agent. |
| **Dashboard** | `docs/build_dashboard.py` → `docs/dashboard.html` | A generator that reads `metrics.json`, `demo_summary.json` and the decision log and bakes them into one self-contained page for a non-technical reader — headline accuracy, the outcome breakdown, the monitor dip, the unmatched list, the one wrong answer. No numbers typed by hand. |
| **Onboarding** (extra, not a graded phase) | `onboard/mapper.py`, `onboard/schema.py`, `onboard/cli.py` | Answers "would this work on a company's own data?" for the loader layer: shows the model a new file's headers + a few sample rows, it proposes a mapping onto `Payment`/`Invoice`, a human confirms once, and the rest is plain deterministic Python. `onboard/demo.py` feeds a made-up bank's CSV through this and into the **unmodified** Phase-3 agent. See [`onboarding.md`](onboarding.md). |

Per-phase detail: [`phase4-evaluation.md`](phase4-evaluation.md) ·
[`phase5-monitoring.md`](phase5-monitoring.md) · [`phase6-qa.md`](phase6-qa.md).

---

## Mapping to the Track 04 brief

> *"Run the books and the cash position."* Agents closing finance-operations
> loops across 50+ synthetic records, reporting match rates and unresolved
> exceptions. Examples: multi-source reconciliation, settlement Q&A, cash
> forecasting, tax matching. Success demands throughput metrics, measured
> accuracy, and transparent exception documentation.

| Phrase from the brief | Where it lives | Evidence |
|---|---|---|
| **"Run the books"** | The reconciliation loop: for every incoming payment, which invoice(s) it settles, or why it can't be placed | `agent/decision_log.jsonl` — 130 payments resolved to a typed outcome |
| **"…and the cash position"** | The books resolved *is* the cash position — settled invoices, partials, and the unexplained residue | `eval/metrics.json`: ₹66,02,965 matched of ₹68,04,475 received; 9 payments unplaced |
| **"Agents closing finance-operations loops"** | `agent/reconciler.py` — an autonomous tool-use loop that takes one payment and closes it end-to-end: decide → call tools → verify amounts → commit or escalate | Loop runs 2–12 turns/payment; 2.45 tool calls/payment |
| **"across 50+ synthetic records"** | `data/generate_data.py` — **130** payments, 180 invoices, all synthetic, seeded | Floor is 50; we run 130 |
| **"reporting match rates"** | `eval/evaluate.py` → `metrics.json` | Match rate **121/130 (100% of the 120 solvable)** |
| **"and unresolved exceptions"** | `exception` records in the log carry an `exception_reason` and full reasoning; the evaluator scores exception quality; the Q&A layer answers *"why didn't PAY0121 match?"* | **9 exceptions, 100% precision** (all real orphans); 0 solvable payments dumped |
| **"multi-source reconciliation"** | Two sources with different schemas — bank wire (`transaction_id, remittance_info, sender_name`) and gateway (`settlement_id, payer_reference, payer_email, gross/fee/net`) — normalised in `tools/loaders.py`; 5 mess types on top | `data/bank_wire_transactions.csv` + `data/gateway_settlements.csv` |
| **"settlement Q&A"** | `qa/` — natural-language questions over the decision log | 7/7 judge-style questions answered, all log-grounded ([`qa/demo_output.txt`](../qa/demo_output.txt)) |
| **"cash forecasting, tax matching"** | **Deliberately out of scope** — these are the brief's *examples*, not requirements | [`scope.md`](scope.md) |
| **"throughput metrics"** | `metrics.json` throughput block | ~5 payments/min (free-tier bound), 2.45 model calls/payment, ~7.3k tokens/payment, 26 min for 130 |
| **"measured accuracy"** | `eval/evaluate.py` compares `(match_type, invoice_ids)` per payment against the hidden key — measured, not estimated | **129/130 (99.2%) exact**; per type 65/65, 20/20, 20/20, 15/15, 9/10 |
| **"transparent exception documentation"** | Every decision — not just exceptions — carries a confidence and a written rationale plus the verbatim tool transcript; the evaluator prints each wrong answer with its reasoning; the Q&A layer makes the trail interrogable | `agent/decision_log.jsonl`; `docs/phase4-evaluation.md` "the one wrong answer" |

---

## The honest caveat

The deterministic pre-flight, run alone against the answer key, scores **130/130**
— the synthetic data is fully deterministic and the tools were built for exactly
these five mess types. So on *this* dataset the LLM is mostly confirming a
verdict the plain-Python path already reached.

What the agent adds, and why it's the right shape for the real problem:

- **A reasoning narrative and a calibrated confidence** on every decision — the
  thing a human controller actually needs to trust or override a match.
- **The audit trail** that Phases 5 and 6 depend on.
- **Generalisation** — the pre-flight's rules are hand-fitted to these mess
  types; the agent degrades gracefully on payment shapes nobody anticipated (and
  the monitor is built to catch exactly that — see the bad batch, where the
  tools all whiff and the agent correctly refuses to guess).

The one miss, **PAY0123**, is a planted orphan whose amount happens to land 2.11%
below a real invoice — inside the gateway fee band — so the agent called it
`fee_deducted`. Its reasoning is internally sound; it's a genuine data-design
collision, kept as a hard case for the demo.

---

## 5-minute walkthrough (video script)

Every command below runs from a fresh clone. Beats 2–4 need no API key.

| Beat | Command | What to show / say |
|---|---|---|
| **1. The agent reasoning on a hard case** | `python -m agent.run_handpicked PAY0123 --log agent/decision_log_hardcase.jsonl` | The loop parsing the reference, pulling invoice amounts, running the fee check, and *deciding* — not a scripted win. Note the calibrated 0.80 confidence and that it overrode the pre-flight. "This is the one payment of 130 it gets wrong, and you can see exactly why from the log." *(The `--log` flag keeps this one-off run from overwriting the full 130-record log the next beats read.)* |
| **2. Measured accuracy + throughput** | `python -m eval.evaluate` | The report: 129/130 exact, 100% match rate on solvable, 0 solvable payments abandoned, ~5 payments/min. "Graded against a hidden answer key the agent never sees." |
| **3. The monitor catching degradation** | `python -m monitor.demo` | The windowed match rate collapsing 100% → 30% as the injected bad batch lands, `LOW MATCH RATE` / `EXCEPTION SPIKE` / `LOW CONFIDENCE` firing mid-stream, then clearing as healthy payments resume. "No answer key here — it's watching the signals the agent produces about itself." |
| **4. Settlement Q&A** | `python -m qa.chat "why didn't payment PAY0121 match?"` then `"show every combined payment above 100000"` | A plain-English question answered from the audit trail, citing payment ids and amounts, with `--show-tools` revealing the log queries behind it. |

Close on **`docs/dashboard.html`** (open it in a browser) — the whole run on one
plain-language page — and the brief-mapping table above.

**Optional bonus beat, if there's time:** `python -m onboard.demo` — a CSV in a
made-up bank's format, with columns nothing in this project generated, gets
mapped by the model and fed into the *same unmodified agent* from beat 1. It
resolves three real invoices exactly and correctly refuses the one with no
invoice behind it. Directly answers "would this only work on your own data?"
— see [`onboarding.md`](onboarding.md).
