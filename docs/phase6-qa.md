# Phase 6 — Settlement Q&A Layer

Give a human a natural-language way to interrogate the audit trail **without
re-running the agent** and without ever touching the ground truth.

## The idea

After a batch run the decision log (`agent/decision_log.jsonl`) holds one record
per payment: what it resolved to, the confidence, the full reasoning narrative,
which tools ran, and what the pre-flight had suggested. That is everything a
finance reviewer would want to ask about — "why didn't payment #121 match?",
"show every combined payment above ₹100,000", "what was the biggest fee we ate?".

Phase 6 is a thin conversational layer over that log: a question goes in, a
Gemini loop queries the log through four read-only tools, and an answer grounded
in those query results comes back — with the payment ids it cited and the exact
queries it ran.

```
 "why didn't PAY0121 match?"
          │
          ▼
   SettlementQA  ── Gemini tool-use loop (qa/assistant.py)
          │           tools: get_payment · search_payments · summarize_batch · group_by
          ▼
    QueryBridge ──► DecisionStore   (qa/store.py — pure, read-only, no ground truth)
          │              ▲
          │              └── agent/decision_log.jsonl  (or the committed snapshot)
          ▼
   grounded answer + cited PAY#### ids + the queries it ran
```

| File | Role |
|---|---|
| `qa/store.py` | `DecisionStore` — loads the JSONL log and answers plain queries: `get` (accepts `PAY0047` / `47` / `#47` / `TXN0010`), `search` (14 AND-combined filters), `summary`, `group_by`, `by_invoice`. Synonym map so "orphan"/"unmatched" → `exception`. No LLM, no ground truth. |
| `qa/query_tools.py` | `QueryBridge` + the 4 tool schemas the model sees. Compact projections keep result rows small. |
| `qa/prompts.py` | System prompt — the record shape, the match-type vocabulary, "cite ids and numbers, say so if the log doesn't have it". |
| `qa/assistant.py` | `SettlementQA.ask()` — the manual Gemini loop (same backend / rate-limit handling as the agent). Returns an `Answer` with the prose, the cited `PAY####` ids, and every tool call. `remember=True` carries context across turns. |
| `qa/chat.py` | `python -m qa.chat` — REPL, or one-shot `python -m qa.chat "..."`. `--show-tools` prints each query. |
| `qa/demo.py` | The deliverable — 7 judge-style questions, transcript to `qa/demo_output.txt`, summary to `qa/demo_summary.json`. |
| `qa/test_qa.py` | 21 offline tests: the query layer against a fixture + the committed snapshot, and the loop against a scripted fake client. |
| `qa/batch_decision_log.jsonl` | Committed snapshot of the 130-record Phase 4 run, so the demo and a fresh clone work without an API key or a re-run. The live `agent/decision_log.jsonl` is used automatically when present. |

## The query tools

| Tool | For | Returns |
|---|---|---|
| `get_payment(identifier)` | one specific payment | full record: match type, invoice ids, confidence, **full reasoning**, exception reason, tools run, pre-flight suggestion |
| `search_payments(...)` | filtered lists | compact rows; filters: match_type (+ synonyms), min/max amount, min/max confidence, source, date range, forced_retry, preflight_disagreed, had_error, invoice_id, reference/counterparty/reasoning substring; sort_by / descending / limit |
| `summarize_batch()` | batch-wide totals | counts per match type, matched vs exception, total received, total matched value, mean confidence, forced-retry count, pre-flight-disagreement count, date range, models |
| `group_by(field)` | breakdowns | `{group: count}` for match_type / source / model / confidence_band / preflight_agreement / forced_retry / month |

## Result — `python -m qa.demo`

Seven questions phrased the way a finance reviewer would ask them out loud, run
against the committed 130-decision log. **7/7 answered, every answer grounded in
at least one log query.** Full transcript in
[`../qa/demo_output.txt`](../qa/demo_output.txt), machine summary in
[`../qa/demo_summary.json`](../qa/demo_summary.json).

| # | Question | Queries it ran | Answer (abridged) |
|---|---|---|---|
| 1 | Why didn't payment PAY0121 match? | `get_payment(PAY0121)` | exception @ 0.90 — reference `"N/A"`, "Unknown Sender Ltd", no amount/customer match, fee reconciliation failed (one invoice off 0.57%, one an overpayment). |
| 2 | Every combined payment above ₹100,000 | `search_payments(match_type=combined, min_amount=100000)` | 12 payments, listed with amount, date, source, invoice ids — from PAY0067 (₹1,89,532.60) down to PAY0068 (₹1,02,759.78). |
| 3 | How many reconciled, total value matched? | `summarize_batch()` | 130 processed, 121 matched / 9 exceptions, **₹66,02,965.47** matched of ₹68,04,475.48 received. |
| 4 | Which payments had a fee deducted, biggest fee? | `search_payments(fee_deducted)` + 16 × `get_payment` | 16 payments; largest fee **₹1,734.51 on PAY0113** (2.69% of ₹64,513.70); full list with gross/received/fee. |
| 5 | Any payment where the agent overrode its pre-flight? | `search_payments(preflight_disagreed=true)` + `get_payment` | 1 — **PAY0123**: pre-flight said `exception`, agent resolved `fee_deducted` → INV0038 @ 0.80 after an amount search + a 2.11% fee-band check. |
| 6 | Breakdown by match type | `group_by(match_type)` | single_full 65 · combined 20 · partial 20 · fee_deducted 16 · exception 9. |
| 7 | Lowest-confidence matches, and why | `search_payments(sort_by=confidence)` + 3 × `get_payment` | PAY0122 @ 0.70 (no ref, 4 amount-only candidates → exception), then PAY0123 / PAY0129 @ 0.80, each with the agent's stated reason. |

The two questions that fan out to many `get_payment` calls (4, 7) do so because
the model wants every row's full reasoning; a tighter prompt could batch them,
but the answers are correct as-is.

> The free-tier Gemini endpoint returns transient `503 UNAVAILABLE` under load.
> `SettlementQA._generate` retries 5xx (and 429) with backoff, and `qa/demo.py`
> re-asks a failed question twice more — questions 5 and 7 above each recovered
> that way (hence their longer wall-clock time).

## Reproduce

```bash
python -m qa.chat                                  # REPL over the decision log
python -m qa.chat "show every combined payment above 100000" --show-tools
python -m qa.demo                                  # the 7 judge questions
python -m unittest qa.test_qa                       # 21 offline tests
```

## Scope / honesty notes

- The Q&A layer reads the **decision log only**. It never re-invokes the agent
  and never opens `ground_truth.json` — so it can report what the agent decided
  and how sure it was, not whether that decision was objectively correct. That
  is the right boundary: it is a window onto the audit trail, not a second
  grader.
- Every answer carries the tool calls that produced it (`--show-tools`, or the
  `tool_calls` field on the `Answer`), so a claim in the prose can always be
  traced to a query over the log.
- Uses the same free-tier Gemini backend as the agent; a question costs 2–4
  model calls. Model via `FC_QA_MODEL` (falls back to `FC_AGENT_MODEL`).
