# AI Finance Controller

**Razorpay AI Buildathon — Track 04.** An LLM tool-use agent that reconciles
messy, multi-source incoming payments against an open-invoice ledger — resolving
each one to a single invoice, a combination, a partial payment, a fee-deducted
payment, or an honest exception — with a graded accuracy number, a live
self-monitoring layer, and a natural-language Q&A layer over the audit trail.

> **Scope.** We scope to **multi-source reconciliation** and **settlement Q&A**;
> forecasting and tax-matching are separate loops we deliberately did not attempt.
> ([`docs/scope.md`](docs/scope.md))

---

## What it does

Payments arrive from two sources with **different schemas** — a bank wire file
and a gateway settlement file — and don't line up cleanly with invoices:
shorthand references, gateway fees skimmed off the top, timing offsets, one
payment covering several invoices, partial payments, and genuine orphans that
match nothing. The agent takes one payment at a time, decides which tools to
call, checks the amounts, and commits to a resolution with a **confidence score
and written reasoning**. Every decision lands in an audit log.

```
 data/*.csv ─┬─► eval/run_batch.py ──► agent/  ──────────► agent/decision_log.jsonl
             │      (per payment)     tool-use loop         (the audit trail)
             │                             ▲                        │
   agent/preflight.py (deterministic) ─────┘             ┌──────────┼──────────┐
   runs the Phase-2 tools in plain Python                ▼          ▼          ▼
   and hands the agent a head start              eval/evaluate  monitor/    qa/
                                                  (vs hidden    (live       (natural-language
                                                   ground        alerts on   questions over
                                                   truth)        degradation) the log)
```

The agent **never sees the answer key** (`data/ground_truth.json`). Only
`eval/evaluate.py` opens it, and only to score.

---

## Repo layout

| Folder | What's in it | Phase |
|---|---|---|
| [`data/`](data/) | Synthetic data generators + the generated CSVs, the hidden `ground_truth.json`, and the injected "bad batch" for the monitor demo | 1 |
| [`tools/`](tools/) | The five reconciliation functions the agent calls (`parse_remittance_reference`, `find_invoices_by_amount`, `check_combination`, `check_fee_schedule`, …) — plain, LLM-free, unit-tested | 2 |
| [`agent/`](agent/) | The Gemini tool-use loop (`reconciler.py`), the decision-log schema, the deterministic `preflight.py` | 3 |
| [`eval/`](eval/) | The batch runner (`run_batch.py`, resumable) and the grader (`evaluate.py` → `metrics.json`) | 4 |
| [`monitor/`](monitor/) | Sliding-window health tracker + alert rules; a demo that shows an alert firing mid-run and clearing | 5 |
| [`qa/`](qa/) | The settlement Q&A layer — ask the decision log questions in plain English (`python -m qa.chat`) | 6 |
| [`docs/`](docs/) | Scope statement and a per-phase writeup | — |

Start with [`docs/architecture.md`](docs/architecture.md) — the full pipeline, a
component table, and how each part maps to the Track 04 brief. Per-phase detail:
[`docs/phase4-evaluation.md`](docs/phase4-evaluation.md) ·
[`docs/phase5-monitoring.md`](docs/phase5-monitoring.md) ·
[`docs/phase6-qa.md`](docs/phase6-qa.md).

---

## Quickstart

Requires **Python 3.10+** (developed on 3.14).

```bash
pip install -r requirements.txt
```

### Runs with no API key (uses committed data)

```bash
# 1. all tests — 91, offline, ~0.2s
python -m unittest discover -s . -p "test_*.py"

# 2. the Phase-2 tools, each cracking one real payment
python -m tools.demo

# 3. the graded reconciliation report (reproduces eval/metrics.json
#    from the committed 130-decision snapshot)
python -m eval.evaluate

# 4. the live monitor catching a bad batch mid-run — alerts FIRE then CLEAR
python -m monitor.demo

# 5. rebuild the plain-language dashboard, then open docs/dashboard.html
python -m docs.build_dashboard
```

**[`docs/dashboard.html`](docs/dashboard.html)** is a single self-contained page
(no server, open it in any browser) that turns the run into something a
non-technical reader can follow — the headline accuracy, how the payments broke
down, the live-monitor dip, the unmatched list, and the one wrong answer.

### Runs that call the Gemini API

Get a free key at <https://aistudio.google.com/apikey>, then:

```bash
cp .env.example .env        # and paste your key into GEMINI_API_KEY
```

```bash
# the agent reasoning through a few hand-picked payments, one of each mess type
python -m agent.run_handpicked

# ask the decision log questions in plain English
python -m qa.chat
python -m qa.chat "why didn't payment PAY0121 match?"
python -m qa.chat "show every combined payment above 100000" --show-tools

# the seven judge-style questions, end to end
python -m qa.demo

# the full batch run (all 130 payments, ~26 min on the free tier, resumable)
python -m eval.run_batch --fresh
python -m eval.evaluate
```

The free tier is rate-limited (~15 req/min) and occasionally returns transient
`503`s; the agent and Q&A loops self-throttle and retry.

### Regenerate the dataset (optional)

```bash
python -m data.generate_data          # payments.csv / invoices.csv / ground_truth.json
python -m data.generate_bad_batch     # the monitor's injected bad batch
```

The generators are seeded (`random.seed(42)`), so this reproduces the exact same
130 payments.

---

## Results

Full run, 130 payments, `gemini-3.5-flash-lite`, pre-flight on, 0 errors.
Numbers from [`eval/metrics.json`](eval/metrics.json); full breakdown in
[`docs/phase4-evaluation.md`](docs/phase4-evaluation.md).

| Metric | Result |
|---|---|
| **Accuracy** (match type **and** invoice set exactly right) | **129 / 130 (99.2%)** |
| **Match rate** — of the 120 solvable payments | **120 / 120 (100%)** |
| **Exception precision** — flagged exceptions that are real orphans | **9 / 9 (100%)** |
| **Solvable payments wrongly dumped to exceptions** | **0** |
| Throughput | ~5 payments/min (free-tier bound), 2.45 model calls/payment |

Per type: single_full 65/65 · combined 20/20 · partial 20/20 · fee_deducted
15/15 · orphan 9/10. The one miss — **PAY0123** — is a planted orphan whose
amount happens to land inside the gateway fee band of a real invoice; the
agent's reasoning is internally sound. It's a genuine data-design collision, kept
as a hard case.

**Live monitoring:** the injected bad batch (payments in a reference format the
tools can't parse) drives the windowed match rate from 100% down to ~30%; `LOW
MATCH RATE`, `EXCEPTION SPIKE` and `LOW CONFIDENCE` alerts fire mid-stream and
then clear as healthy payments resume. `python -m monitor.demo`.

**Settlement Q&A:** 7/7 judge-style questions answered, every answer grounded in
a query over the decision log. Transcript in [`qa/demo_output.txt`](qa/demo_output.txt).

---

## Notes on honesty

- All data is **synthetic**, generated locally with a known answer key. There is
  no live payment-gateway integration.
- The ground truth is written **before** the mess is layered on, and is read in
  exactly one place — `eval/evaluate.py` — for scoring only.
- The deterministic pre-flight scores 130/130 against truth on its own (the
  synthetic data is fully deterministic and the tools were built for exactly
  these mess types). The agent's value here is the reasoning narrative, the
  calibrated confidence, the audit trail, and generalisation beyond these mess
  types — not the raw accuracy number in isolation.
- The monitor measures **operational health, not accuracy** — live, there is no
  answer key, so it watches the signals the agent produces about itself.
