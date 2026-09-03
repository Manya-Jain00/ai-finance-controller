# Phase 5 — Live Monitoring Layer

Prove the system can catch its **own** degradation while a run is in progress —
not just report a score at the end.

## The idea

Phase 4 grades the agent offline against `ground_truth.json`. That is useless in
production: real payments arrive without an answer key. Phase 5 watches the
signals the agent produces about *itself* — did it commit to a match, how
confident was it, did the deterministic pre-flight still agree, did the
2-strategy rule have to fire — over a sliding window, and raises an alert when
those degrade. **No ground truth is read anywhere in `monitor/`.**

```
                per finished DecisionRecord
 eval/run_batch.py ───────────────► LiveMonitor
 monitor/run_bad_batch.py           ├─ SlidingWindowTracker  (last N records → WindowStats)
 monitor/demo.py (replay)           └─ AlertManager          (WindowStats → FIRE / CLEAR events)
                                            │
                                       dashboard line per payment + alert banners
```

| File | Role |
|---|---|
| `monitor/tracker.py` | `SlidingWindowTracker` — keeps the last `N` (default 20) records, emits a `WindowStats` after each: match rate, mean confidence, exception rate, error rate, forced-retry rate, pre-flight-disagreement rate, effort. Pure, no I/O. |
| `monitor/alerts.py` | Six `AlertRule`s with **hysteresis** (trip threshold ≠ clear threshold, so a metric on the line doesn't flap). `AlertManager` turns each `WindowStats` into FIRE / CLEAR events. |
| `monitor/live.py` | `LiveMonitor` — glues tracker + alerts to a scrolling text dashboard. Runs live during a batch (`eval/run_batch.py --monitor`) or replays a saved log (`python -m monitor.live`). |
| `data/generate_bad_batch.py` | Builds the deliberately-injected bad batch (below). |
| `monitor/run_bad_batch.py` | Runs the real agent over the bad batch, live-monitored → `monitor/bad_batch_log.jsonl`. |
| `monitor/demo.py` | Splices the healthy Phase 4 log + the bad batch, ordered by date, and replays it. The Phase 5 deliverable. |
| `monitor/test_monitor.py` | 12 offline tests (window maths, warm-up gating, hysteresis, fire/clear, healthy data raises nothing). |

## The alert rules

Calibrated against the healthy Phase 4 run (window match rate ~0.85–1.0, mean
confidence ~0.94, exception rate ~0.07, pre-flight disagreement ~0.008):

| Rule | Trips when (over last N) | Clears at | Meaning |
|---|---|---|---|
| **LOW MATCH RATE** | match rate < 70% | ≥ 80% | agent stopped committing to matches — headline signal |
| LOW CONFIDENCE | mean confidence < 0.78 | ≥ 0.85 | resolving, but unsure |
| EXCEPTION SPIKE | exception rate > 35% | ≤ 20% | burst of payments dumped to the exception bucket |
| PRE-FLIGHT DISAGREEMENT | agent overrides pre-flight > 35% | ≤ 15% | the cheap deterministic path stopped working |
| RETRY SPIKE | forced-retry rate > 30% | ≤ 12% | first attempts are weak |
| ERROR SPIKE | loop error rate > 15% | ≤ 5% | API / loop failures |

An alert only arms once the window holds at least `min_samples` (default 10)
records, so start-of-run noise can't trip it.

## The injected bad batch

`data/bad_batch/` — 15 payments (6 single, 5 combined, 4 partial) from customers
who "migrated to a new ERP at quarter end". The new system emits references in a
purchase-order format the Phase 2 parser cannot decode
(`PO#4824 / DT#20260612 / VND#NimbusRetail`, `20260614//5852//5853`) and pays
from a new email domain and trading name, so the customer lookup misses too. The
PO numbers are the ERP's own — they don't map to our `INV` ids.

Every one of these still corresponds to a real open invoice (or a real
2-invoice sum): **a human could reconcile all 15 by hand.** The agent can't —
every tool it has whiffs — so it correctly refuses to guess and returns
`exception` on all 15. That is the degradation the monitor has to catch.

The bad batch is dated late-Q1, so when the combined stream is ordered by
payment date it lands mid-run, and the monitor shows the alerts both **firing**
and then **clearing** as the healthy Apr–Jun payments resume.

## Result — `python -m monitor.demo`

Healthy log (130) + bad batch (15), ordered by date, replayed through the
monitor. Full transcript in [`../monitor/demo_output.txt`](../monitor/demo_output.txt),
summary in [`../monitor/demo_summary.json`](../monitor/demo_summary.json).

```
[ 69/145] PAY0088  gateway partial      0.85  | match 100%  conf 0.93  exc   0% ...
[ 70/145] PAY0904  gateway exception    0.90  | match  95%  conf 0.94  exc   5% ...   <- bad batch starts
...
[ 80/145] PAY0909  gateway exception    0.80  | match  65%  conf 0.83  exc  35% ...   <<1 ALERT>>
  !!! ALERT FIRED  LOW MATCH RATE  [match_rate over last 20 = 65% (< 70% trip)]  @ record 80
[ 81/145] PAY0910  bank    exception    0.85  | match  60%  conf 0.82  exc  40% ...   <<2 ALERTS>>
  !!! ALERT FIRED  EXCEPTION SPIKE  [exception_rate over last 20 = 40% (> 35% trip)]  @ record 81
[ 88/145] PAY0902  bank    exception    0.30  | match  35%  conf 0.76  exc  65% ...   <<3 ALERTS>>
  !!! ALERT FIRED  LOW CONFIDENCE  [mean_confidence over last 20 = 0.76 (< 0.78 trip)]  @ record 88
...                                             match bottoms at 30%
[100/145] PAY0083  bank    combined     0.95  | match  55%  conf 0.85  exc  45% ...
  ... ALERT CLEARED  LOW CONFIDENCE  [mean_confidence over last 20 = 0.85 (>= 0.85 clear)]  @ record 100
[108/145] ...                                    match 80%
  ... ALERT CLEARED  LOW MATCH RATE   @ record 108
  ... ALERT CLEARED  EXCEPTION SPIKE  @ record 108
```

`python -m monitor.run_bad_batch` (the bad batch alone, window 10) trips **four**
alerts — LOW MATCH RATE, LOW CONFIDENCE, EXCEPTION SPIKE and PRE-FLIGHT
DISAGREEMENT — by record 5.

## Reproduce

```bash
python -m data.generate_bad_batch          # writes data/bad_batch/ (committed)
python -m monitor.run_bad_batch --fresh     # ~2 min on the free tier; needs GEMINI_API_KEY
python -m monitor.demo                      # replay + assert alert fired & cleared
python -m monitor.demo --offline            # same, but synthesises the bad batch (no API)
python -m monitor.live agent/decision_log.jsonl   # replay any decision log
python -m eval.run_batch --monitor          # live monitor during the real Phase 4 batch
python -m unittest monitor.test_monitor
```

## Scope / honesty notes

- The monitor measures **operational health, not accuracy** — it cannot know a
  match is *correct*, only that the agent made one confidently and the
  pre-flight agreed. That is the right thing to measure live.
- The bad batch is genuinely solvable by a human; the agent's all-`exception`
  response is the *correct* conservative call given its tools, and is exactly
  what the monitor is designed to surface (a segment the automation can no
  longer handle → route to a human).
- `monitor/bad_batch_log.jsonl` is committed so the demo runs without an API
  key; regenerate it with `run_bad_batch --fresh`.
