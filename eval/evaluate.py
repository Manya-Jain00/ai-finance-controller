"""Phase 4 — grade the decision log against the hidden ground truth.

    python -m eval.evaluate                       # reads agent/decision_log.jsonl
    python -m eval.evaluate --log path --json eval/metrics.json --quiet

This is the ONLY place ground_truth.json is opened for scoring. It compares each
agent decision to the answer key and reports the four Phase-4 metrics:

  * match rate        — how often the agent committed to a concrete match
  * accuracy          — how often that commitment was correct
  * throughput        — payments/min, model calls & tokens per payment
  * exception quality — did it flag the real orphans, and did it dump any
                        solvable payment into the exception bucket?

Wrong answers are listed with the agent's own reasoning so Phase 4 debugging is
"read the log", not "tweak the prompt blindly" (spec point 3).
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict

from agent.decision_log import DEFAULT_LOG_PATH, load_log
from tools.loaders import DATA_DIR

TYPES = ["single_full", "combined", "partial", "fee_deducted", "orphan"]
# the agent says "exception"; the ground truth says "orphan" — same thing
AGENT_TO_TRUTH = {"exception": "orphan"}


def _norm_ids(ids) -> tuple[str, ...]:
    return tuple(sorted(str(x).strip().upper() for x in (ids or [])))


class Row:
    """One payment: what the truth says, what the agent did, whether it's right."""

    def __init__(self, pid: str, truth: dict, rec: dict | None):
        self.pid = pid
        self.truth_type = truth["match_type"]
        self.truth_ids = _norm_ids(truth.get("invoice_ids"))
        self.notes = truth.get("notes", "")
        self.rec = rec

        self.ran = rec is not None
        self.error = rec.get("error") if rec else None
        res = rec.get("resolution") if rec else None
        self.resolved = bool(res) and not self.error

        if self.resolved:
            self.agent_type = AGENT_TO_TRUTH.get(res["match_type"], res["match_type"])
            self.agent_ids = _norm_ids(res.get("invoice_ids"))
            self.confidence = float(res.get("confidence", 0.0) or 0.0)
            self.reasoning = (res.get("reasoning") or "").strip()
        else:
            self.agent_type = None
            self.agent_ids = ()
            self.confidence = 0.0
            self.reasoning = self.error or "no decision recorded"

        self.type_ok = self.resolved and self.agent_type == self.truth_type
        self.ids_ok = self.resolved and self.agent_ids == self.truth_ids
        self.correct = self.type_ok and self.ids_ok

        self.is_concrete = self.resolved and self.agent_type != "orphan"
        self.truth_solvable = self.truth_type != "orphan"

    @property
    def preflight_suggestion(self) -> str:
        pf = (self.rec or {}).get("preflight") or {}
        mt = pf.get("suggested_match_type")
        return f"{mt} {pf.get('suggested_invoice_ids', [])}" if mt else "(inconclusive)"


def evaluate(log_path: str, gt_path: str) -> dict:
    log_rows = load_log(log_path)
    by_pid = {r.get("payment_id"): r for r in log_rows}
    with open(gt_path, encoding="utf-8") as f:
        gt = json.load(f)

    rows = [Row(pid, truth, by_pid.get(pid)) for pid, truth in sorted(gt.items())]
    total = len(rows)
    evaluated = [r for r in rows if r.resolved]

    # ---- headline metrics -------------------------------------------------
    n_correct = sum(r.correct for r in rows)
    n_concrete = sum(r.is_concrete for r in rows)
    n_ran = sum(r.ran for r in rows)
    n_errors = sum(bool(r.error) for r in rows)

    solvable = [r for r in rows if r.truth_solvable]
    solvable_matched = [r for r in solvable if r.is_concrete]

    metrics: dict = {
        "dataset": {"payments": total, "solvable": len(solvable),
                    "orphans": total - len(solvable)},
        "coverage": {
            "logged": n_ran, "missing": total - n_ran, "errors": n_errors,
            "pct_logged": round(100 * n_ran / total, 1),
        },
        "match_rate": {
            "committed_to_a_match": n_concrete,
            "pct_of_all": round(100 * n_concrete / total, 1),
            "pct_of_solvable": round(100 * len(solvable_matched) / len(solvable), 1),
        },
        "accuracy": {
            "correct": n_correct,
            "pct_of_all": round(100 * n_correct / total, 1),
            "pct_of_evaluated": round(100 * n_correct / len(evaluated), 1) if evaluated else 0.0,
            "type_only": round(100 * sum(r.type_ok for r in rows) / total, 1),
            "invoice_set_only": round(100 * sum(r.ids_ok for r in rows) / total, 1),
        },
    }

    # ---- per-type accuracy ---------------------------------------------
    per_type = {}
    for t in TYPES:
        grp = [r for r in rows if r.truth_type == t]
        per_type[t] = {
            "n": len(grp),
            "correct": sum(r.correct for r in grp),
            "pct": round(100 * sum(r.correct for r in grp) / len(grp), 1) if grp else 0.0,
        }
    metrics["accuracy_by_type"] = per_type

    # ---- confusion matrix (truth rows x agent cols) --------------------
    confusion = {t: Counter() for t in TYPES}
    for r in rows:
        confusion[r.truth_type][r.agent_type or ("error" if r.error else "missing")] += 1
    metrics["confusion"] = {t: dict(c) for t, c in confusion.items()}

    # ---- exception quality -------------------------------------------
    agent_exc = [r for r in rows if r.agent_type == "orphan"]
    correct_exc = [r for r in agent_exc if r.truth_type == "orphan"]
    gave_up_on_solvable = [r for r in agent_exc if r.truth_solvable]
    missed_orphans = [r for r in rows if r.truth_type == "orphan" and r.is_concrete]
    metrics["exception_quality"] = {
        "agent_raised": len(agent_exc),
        "correct": len(correct_exc),
        "precision": round(100 * len(correct_exc) / len(agent_exc), 1) if agent_exc else None,
        "orphan_recall": round(100 * len(correct_exc) / (total - len(solvable)), 1),
        "gave_up_on_solvable": {
            "count": len(gave_up_on_solvable),
            "pct_of_solvable": round(100 * len(gave_up_on_solvable) / len(solvable), 1),
            "payments": [r.pid for r in gave_up_on_solvable],
        },
        "missed_orphans": {
            "count": len(missed_orphans),
            "payments": [r.pid for r in missed_orphans],
        },
    }

    # ---- throughput --------------------------------------------------
    lat = [r.rec.get("latency_s", 0.0) for r in rows if r.ran]
    iters = [r.rec.get("iterations", 0) for r in rows if r.ran]
    ncalls = [len(r.rec.get("tool_calls", [])) for r in rows if r.ran]
    tin = sum(r.rec.get("usage", {}).get("input_tokens", 0) for r in rows if r.ran)
    tout = sum(r.rec.get("usage", {}).get("output_tokens", 0) for r in rows if r.ran)
    total_lat = sum(lat)
    metrics["throughput"] = {
        "payments_timed": len(lat),
        "total_agent_seconds": round(total_lat, 1),
        "payments_per_min": round(len(lat) / (total_lat / 60), 2) if total_lat else 0.0,
        "avg_latency_s": round(total_lat / len(lat), 2) if lat else 0.0,
        "avg_model_iterations": round(sum(iters) / len(iters), 2) if iters else 0.0,
        "avg_tool_calls": round(sum(ncalls) / len(ncalls), 2) if ncalls else 0.0,
        "forced_retries": sum(bool(r.rec.get("forced_retry")) for r in rows if r.ran),
        "tokens": {"input": tin, "output": tout,
                   "per_payment": (tin + tout) // len(lat) if lat else 0},
    }

    # ---- preflight contribution ------------------------------------
    pf_rows = [r for r in rows if r.ran and (r.rec.get("preflight") or {}).get("suggested_match_type")]
    pf_agree = sum(
        1 for r in pf_rows
        if AGENT_TO_TRUTH.get((r.rec["preflight"]["suggested_match_type"]),
                              r.rec["preflight"]["suggested_match_type"]) == r.agent_type
    )
    pf_right = sum(
        1 for r in pf_rows
        if AGENT_TO_TRUTH.get((r.rec["preflight"]["suggested_match_type"]),
                              r.rec["preflight"]["suggested_match_type"]) == r.truth_type
        and _norm_ids(r.rec["preflight"]["suggested_invoice_ids"]) == r.truth_ids
    )
    metrics["preflight"] = {
        "made_a_suggestion": len(pf_rows),
        "agent_followed": pf_agree,
        "suggestion_correct_vs_truth": pf_right,
    }

    # ---- the wrong answers, for debugging ---------------------------
    wrong = [r for r in rows if not r.correct]
    metrics["wrong_answers"] = [
        {
            "payment_id": r.pid,
            "truth": f"{r.truth_type} {list(r.truth_ids)}",
            "agent": (f"{r.agent_type} {list(r.agent_ids)} @ {r.confidence:.2f}"
                      if r.resolved else f"[{r.reasoning[:60]}]"),
            "preflight": r.preflight_suggestion,
            "truth_note": r.notes,
            "agent_reasoning": r.reasoning,
        }
        for r in wrong
    ]
    return {"metrics": metrics, "rows": rows}


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 28) -> str:
    filled = round(pct / 100 * width)
    return "#" * filled + "-" * (width - filled)


def print_report(m: dict) -> None:
    d, cov = m["dataset"], m["coverage"]
    print("=" * 72)
    print(f"  PHASE 4 EVALUATION   ({d['payments']} payments: "
          f"{d['solvable']} solvable, {d['orphans']} planted orphans)")
    print("=" * 72)

    if cov["missing"] or cov["errors"]:
        print(f"\n  coverage: {cov['logged']}/{d['payments']} logged "
              f"({cov['missing']} missing, {cov['errors']} errored)")

    mr, acc = m["match_rate"], m["accuracy"]
    print(f"\n  MATCH RATE   {mr['pct_of_all']:>5.1f}%  [{_bar(mr['pct_of_all'])}]  "
          f"{mr['committed_to_a_match']}/{d['payments']} committed to a match")
    print(f"               {mr['pct_of_solvable']:>5.1f}%  of the {d['solvable']} solvable payments")
    print(f"\n  ACCURACY     {acc['pct_of_all']:>5.1f}%  [{_bar(acc['pct_of_all'])}]  "
          f"{acc['correct']}/{d['payments']} exactly correct (type + invoices)")
    print(f"               type label alone {acc['type_only']:.1f}%   "
          f"invoice set alone {acc['invoice_set_only']:.1f}%")

    print("\n  accuracy by ground-truth type:")
    for t, s in m["accuracy_by_type"].items():
        print(f"    {t:<13} {s['correct']:>2}/{s['n']:<2}  {s['pct']:>5.1f}%  [{_bar(s['pct'], 20)}]")

    print("\n  confusion (truth \\ agent):")
    cols = TYPES + ["error", "missing"]
    present = [c for c in cols if any(m["confusion"][t].get(c) for t in TYPES)]
    print("    " + " " * 13 + "".join(f"{c[:9]:>10}" for c in present))
    for t in TYPES:
        cells = "".join(f"{m['confusion'][t].get(c, ''):>10}" for c in present)
        print(f"    {t:<13}{cells}")

    eq = m["exception_quality"]
    print("\n  EXCEPTION QUALITY")
    print(f"    agent raised {eq['agent_raised']} exception(s); {eq['correct']} were real orphans"
          + (f"  (precision {eq['precision']:.1f}%)" if eq["precision"] is not None else ""))
    print(f"    orphan recall: {eq['orphan_recall']:.1f}%  "
          f"({eq['correct']}/{d['orphans']} planted orphans caught)")
    g = eq["gave_up_on_solvable"]
    print(f"    gave up on solvable payments: {g['count']}  ({g['pct_of_solvable']:.1f}% of solvable)"
          + (f"  -> {g['payments']}" if g["payments"] else ""))
    if eq["missed_orphans"]["count"]:
        print(f"    orphans wrongly matched: {eq['missed_orphans']['payments']}")

    tp = m["throughput"]
    print("\n  THROUGHPUT")
    print(f"    {tp['payments_per_min']:.2f} payments/min agent time "
          f"({tp['avg_latency_s']:.1f}s each, {tp['total_agent_seconds'] / 60:.1f} min total)")
    print(f"    {tp['avg_model_iterations']:.2f} model calls/payment, "
          f"{tp['avg_tool_calls']:.2f} tool calls/payment, {tp['forced_retries']} forced retries")
    print(f"    tokens: {tp['tokens']['input']:,} in / {tp['tokens']['output']:,} out "
          f"(~{tp['tokens']['per_payment']:,}/payment)")

    pf = m["preflight"]
    if pf["made_a_suggestion"]:
        print(f"\n  PREFLIGHT   suggested on {pf['made_a_suggestion']} payments; "
              f"agent followed {pf['agent_followed']}; "
              f"{pf['suggestion_correct_vs_truth']} of those suggestions matched truth")

    if m["wrong_answers"]:
        print("\n" + "-" * 72)
        print(f"  WRONG ANSWERS ({len(m['wrong_answers'])}) -- read the reasoning before touching the prompt")
        print("-" * 72)
        for w in m["wrong_answers"]:
            print(f"\n  {w['payment_id']}   truth: {w['truth']}   ({w['truth_note']})")
            print(f"           agent: {w['agent']}")
            print(f"        preflight: {w['preflight']}")
            print(f"        reasoning: {w['agent_reasoning'][:300]}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=None)
    ap.add_argument("--gt", default=os.path.join(DATA_DIR, "ground_truth.json"))
    ap.add_argument("--json", default=os.path.join(os.path.dirname(__file__), "metrics.json"))
    ap.add_argument("--quiet", action="store_true", help="write metrics.json only")
    args = ap.parse_args(argv)

    # The live log is git-ignored; fall back to the committed 130-record snapshot
    # (under qa/) so the grade reproduces on a fresh clone with no batch re-run.
    snapshot = os.path.join(os.path.dirname(os.path.dirname(__file__)), "qa", "batch_decision_log.jsonl")
    log = args.log or (DEFAULT_LOG_PATH if os.path.exists(DEFAULT_LOG_PATH) else snapshot)

    if not os.path.exists(log):
        print(f"no decision log at {log} -- run `python -m eval.run_batch` first.")
        return 2
    args.log = log

    result = evaluate(args.log, args.gt)
    m = result["metrics"]
    if not args.quiet:
        print_report(m)

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    print(f"metrics -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
