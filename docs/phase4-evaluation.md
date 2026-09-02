# Phase 4 — Full Batch Run + Evaluation

Run the agent across the whole dataset and grade it against the hidden ground
truth. Real numbers, not estimates.

## How it works

```
data/*.csv ──► eval/run_batch.py ──► agent/decision_log.jsonl ──► eval/evaluate.py ──► eval/metrics.json
                    │                                                   ▲
                    └── agent/preflight.py (deterministic)              │
                                                          data/ground_truth.json (only opened here)
```

- **`agent/preflight.py`** — before the LLM sees a payment, a deterministic pass
  runs the same Phase 2 tools in plain Python: parse the reference, pull the
  candidate invoices, test them for an exact match / an exact sum / an
  invoice-minus-fee gap, and fall back to a customer + amount + combination
  search when the reference is no help. The result (raw tool outputs + a
  *suggestion*) is handed to the agent in its first prompt. The agent still
  decides and still calls `submit_resolution`; the pre-flight just does the
  legwork so a clean payment resolves in one model call instead of four.
- **`eval/run_batch.py`** — runs all 130 payments, appending one `DecisionRecord`
  per payment to the log *the moment it finishes*. A run killed by a rate limit
  or Ctrl-C loses nothing and resumes on the next invocation. Never opens the
  ground truth.
- **`eval/evaluate.py`** — the only place `ground_truth.json` is read for
  scoring. Maps the agent's `exception` to the ground truth's `orphan`, compares
  `(match_type, invoice_ids)` per payment, and computes the four metrics below.
  Lists every wrong answer with the agent's own reasoning.

Reproduce:

```bash
python -m eval.run_batch --fresh      # ~26 min on the free tier
python -m eval.evaluate               # prints the report, writes eval/metrics.json
```

## Results — full run, 130 payments, `gemini-3.5-flash-lite`

Pre-flight enabled. 0 errors, 100% of payments logged. Numbers from
[`eval/metrics.json`](../eval/metrics.json).

### 1. Match rate

| | |
|---|---|
| Committed to a concrete match | **121 / 130 (93.1%)** |
| Of the 120 *solvable* payments | **120 / 120 (100%)** |

The 9 it did not match are the 9 orphans it correctly flagged as exceptions.

### 2. Accuracy

| | |
|---|---|
| Exactly correct (match type **and** invoice set) | **129 / 130 (99.2%)** |

| Ground-truth type | Correct |
|---|---|
| single_full | 65 / 65 |
| combined | 20 / 20 |
| partial | 20 / 20 |
| fee_deducted | 15 / 15 |
| orphan | 9 / 10 |

Confusion (truth → agent): the only off-diagonal cell is one `orphan` → `fee_deducted`.

### 3. Throughput

| | |
|---|---|
| Agent time | 12.1 s / payment (26.2 min total for 130) |
| Rate | ~5 payments / min (free-tier throttle-bound, not compute-bound) |
| Model calls | **2.45 / payment** (with pre-flight) vs **3.89 / payment** unaided |
| Tool calls | 2.45 / payment |
| Forced retries (2-strategy rule) | 3 |
| Tokens | 929k in / 24k out (~7.3k / payment) |

### 4. Exception quality

| | |
|---|---|
| Exceptions raised | 9 |
| ...that were real orphans | 9 → **precision 100%** |
| Planted orphans caught | 9 / 10 → **recall 90%** |
| Solvable payments dumped into the exception bucket | **0** |

It did not give up on a single solvable payment.

## The one wrong answer — PAY0123

- **Truth:** orphan (a planted payment with no matching invoice).
- **Agent:** `fee_deducted` → INV0038, confidence 0.80.
- **Pre-flight said:** exception. This is the 1 payment of 130 where the agent
  overrode the pre-flight suggestion.
- **Agent's reasoning:** the reference was unknown and the counterparty was
  `unknown@example.com`, but an amount search found INV0038 (₹27,627.27), and the
  received ₹27,043.96 is 2.11% short — squarely inside the 1.5–3% gateway fee
  band, so `check_fee_schedule` returned `explained=true`.

This is a genuine data-design collision: a planted orphan whose amount happens to
land within fee tolerance of a real open invoice. The agent's chain of reasoning
is internally sound; the system prompt's "amount proximity alone is not a match"
rule was outweighed by a passing fee check. Candidate fixes (not yet applied, per
"don't tweak the prompt blindly"): require a reference **or** customer link
before accepting `fee_deducted`, or have the generator reject planted orphans
that collide with an invoice inside the fee band.

## Unaided baseline (agent without pre-flight)

A `--no-preflight` sample was started to measure the agent standalone. It
completed 9 payments — **9 / 9 correct** (6 single_full, 3 combined), at 3.89
model calls / payment — before stalling on a multi-hour free-tier rate-limit
backoff. Takeaway so far: the agent reaches the same answers on its own; the
pre-flight makes it ~37% cheaper in model calls, not more accurate. A full
unaided run is pending a quota reset.

## Files

| Path | What |
|---|---|
| `agent/preflight.py` | deterministic pre-analysis |
| `eval/run_batch.py` | batch runner (resumable) |
| `eval/evaluate.py` | grader → `eval/metrics.json` |
| `agent/decision_log.jsonl` | the audit trail (130 records, git-ignored, regenerable) |
| `eval/metrics.json` | the graded numbers (committed) |
| `agent/test_preflight.py`, `eval/test_evaluate.py` | offline tests |
