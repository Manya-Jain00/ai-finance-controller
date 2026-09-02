"""Phase 3 checkpoint — run the agent on a few hand-picked payments.

    python -m agent.run_handpicked            # one payment of each mess type
    python -m agent.run_handpicked --n 2      # two of each
    python -m agent.run_handpicked PAY0066    # specific payment id(s)

Ground truth is used ONLY here, to pick representative payments and to print the
expected answer beside the agent's for a human eyeball check. The agent itself
never receives it. Writes the decision log to agent/decision_log.jsonl.

Requires GEMINI_API_KEY in the environment (or a .env file — python-dotenv is
loaded if present).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from tools.loaders import DATA_DIR, load_invoices, load_payments

from .decision_log import DEFAULT_LOG_PATH, write_log
from .reconciler import Reconciler
from .schema import MATCH_TYPES

MESS_ORDER = ["single_full", "combined", "partial", "fee_deducted", "orphan"]


def _load_side_data():
    with open(os.path.join(DATA_DIR, "payment_id_map.json"), encoding="utf-8") as f:
        ref_to_pid = json.load(f)
    with open(os.path.join(DATA_DIR, "ground_truth.json"), encoding="utf-8") as f:
        gt = json.load(f)
    return ref_to_pid, gt


def _pick(gt: dict, pid_to_ref: dict, n: int) -> list[str]:
    picks: list[str] = []
    for mess in MESS_ORDER:
        matches = [p for p, v in sorted(gt.items()) if v["match_type"] == mess]
        picks.extend(matches[:n])
    return picks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("payment_ids", nargs="*", help="specific PAY#### ids to run")
    ap.add_argument("--n", type=int, default=1, help="payments per mess type (default 1)")
    ap.add_argument("--log", default=DEFAULT_LOG_PATH)
    args = ap.parse_args(argv)

    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: set GEMINI_API_KEY (or put it in a .env file) first.", file=sys.stderr)
        return 2

    invoices = load_invoices()
    payments = load_payments()
    ref_to_pid, gt = _load_side_data()
    pid_to_ref = {v: k for k, v in ref_to_pid.items()}
    by_ref = {p.payment_ref: p for p in payments}

    target_pids = args.payment_ids or _pick(gt, pid_to_ref, args.n)

    recon = Reconciler(invoices, payment_id_map=ref_to_pid)
    print(f"model: {recon.model}\n")

    records = []
    for pid in target_pids:
        ref = pid_to_ref.get(pid)
        if ref is None or ref not in by_ref:
            print(f"?? {pid}: not found, skipping")
            continue
        payment = by_ref[ref]
        truth = gt.get(pid, {})
        print("=" * 78)
        print(f"{pid}  ({payment.source} {ref})   reference={payment.reference!r}  "
              f"received={payment.amount_received}")
        print(f"  expected: {truth.get('match_type')} {truth.get('invoice_ids')}  "
              f"— {truth.get('notes','')}")
        print("  agent:")
    

        rec = recon.reconcile(payment, verbose=True)
        records.append(rec)

        if rec.error:
            print(f"  ERROR: {rec.error}")
        else:
            r = rec.resolution
            ok = (
                r.match_type == truth.get("match_type")
                or (r.match_type == "exception" and truth.get("match_type") == "orphan")
            ) and sorted(r.invoice_ids) == sorted(truth.get("invoice_ids", []))
            mark = "OK " if ok else "XX "
            print(f"  {mark}-> {r.match_type} {r.invoice_ids}  conf={r.confidence:.2f}  "
                  f"iters={rec.iterations}  forced_retry={rec.forced_retry}")
            print(f"     strategies: {r.strategies_tried}")
            print(f"     reasoning: {r.reasoning}")
        print()

    path = write_log(records, args.log)
    resolved = sum(1 for r in records if r.resolved)
    print("=" * 78)
    print(f"{resolved}/{len(records)} resolved without error. Decision log -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
