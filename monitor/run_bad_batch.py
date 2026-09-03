"""Phase 5 — run the agent over the injected bad batch, live-monitored.

    python -m monitor.run_bad_batch          # all 15 bad-batch payments
    python -m monitor.run_bad_batch --limit 3

Same agent, same tools, same pre-flight as the Phase 4 batch run — only the
input changes (``data/bad_batch/``). Each finished `DecisionRecord` is appended
to ``monitor/bad_batch_log.jsonl`` and fed straight into a `LiveMonitor`, so the
dashboard scrolls as the run goes and the match-rate collapse is visible in
real time.

The agent still never sees any ground truth. Requires GEMINI_API_KEY.
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

from agent.decision_log import append_record, load_log
from agent.preflight import build_preflight
from agent.reconciler import Reconciler
from agent.tools_bridge import ToolBridge
from tools.loaders import (
    load_bank_transactions,
    load_gateway_settlements,
    load_invoices,
)

from .live import LiveMonitor

_HERE = os.path.dirname(os.path.abspath(__file__))
BAD_DIR = os.path.normpath(os.path.join(_HERE, "..", "data", "bad_batch"))
BAD_LOG = os.path.join(_HERE, "bad_batch_log.jsonl")


def load_bad_payments():
    bank = load_bank_transactions(os.path.join(BAD_DIR, "bank_wire_transactions.csv"))
    gw = load_gateway_settlements(os.path.join(BAD_DIR, "gateway_settlements.csv"))
    with open(os.path.join(BAD_DIR, "payment_id_map.json"), encoding="utf-8") as f:
        ref_to_pid = json.load(f)
    return bank + gw, ref_to_pid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=BAD_LOG)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--window", type=int, default=10)  # small batch -> small window
    ap.add_argument("--min-samples", type=int, default=5)
    args = ap.parse_args(argv)

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("ERROR: set GEMINI_API_KEY (or put it in a .env file) first.", file=sys.stderr)
        return 2

    if args.fresh and os.path.exists(args.log):
        os.remove(args.log)

    invoices = load_invoices()  # the bad batch references the real ledger
    payments, ref_to_pid = load_bad_payments()
    by_pid = {ref_to_pid.get(p.payment_ref, p.payment_ref): p for p in payments}

    done = {r.get("payment_id") for r in load_log(args.log)} if os.path.exists(args.log) else set()
    queue = [pid for pid in sorted(by_pid) if pid not in done]
    if args.limit:
        queue = queue[: args.limit]
    if not queue:
        print("nothing to do — every bad-batch payment is already logged.")
        return 0

    bridge = ToolBridge(invoices)
    recon = Reconciler(invoices, payment_id_map=ref_to_pid)
    mon = LiveMonitor(window=args.window, min_samples=args.min_samples, total=len(queue))
    print(f"model: {recon.model}   bad-batch payments: {len(queue)}\n")

    for pid in queue:
        pay = by_pid[pid]
        pf = build_preflight(pay, bridge)
        rec = recon.reconcile(pay, preflight=pf)
        append_record(rec, args.log)
        mon.feed(rec)

    mon.print_summary()
    print(f"\ndecision log -> {args.log}")
    print("next: python -m monitor.demo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
