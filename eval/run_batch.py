"""Phase 4 — run the agent across the whole dataset.

    python -m eval.run_batch                 # all payments; resume if interrupted
    python -m eval.run_batch --fresh          # start a new decision log first
    python -m eval.run_batch --limit 10       # first 10 only (smoke test)
    python -m eval.run_batch --only PAY0066,PAY0121
    python -m eval.run_batch --no-preflight   # skip the deterministic pre-analysis

Every payment produces one DecisionRecord, appended to the decision log the
moment it finishes — so a run killed by a rate limit or Ctrl-C loses nothing and
simply resumes on the next invocation (already-logged payments are skipped unless
--fresh or --redo).

The agent never sees ground_truth.json. This script does not open it either;
grading happens separately in `eval.evaluate`.

Requires GEMINI_API_KEY in the environment or a .env file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from agent.decision_log import DEFAULT_LOG_PATH, append_record, load_log
from agent.preflight import build_preflight
from agent.reconciler import Reconciler
from agent.tools_bridge import ToolBridge
from tools.loaders import DATA_DIR, load_invoices, load_payments


def _already_done(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    return {row.get("payment_id") for row in load_log(path)}


def _fmt_res(rec) -> str:
    if rec.error:
        return f"ERROR: {rec.error}"
    r = rec.resolution
    return (f"{r.match_type:<12} {r.invoice_ids}  conf={r.confidence:.2f}  "
            f"iters={rec.iterations}  {rec.latency_s:.1f}s"
            + ("  [forced-retry]" if rec.forced_retry else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG_PATH)
    ap.add_argument("--fresh", action="store_true", help="truncate the log before running")
    ap.add_argument("--redo", action="store_true", help="re-run payments already in the log")
    ap.add_argument("--limit", type=int, help="process at most N payments")
    ap.add_argument("--only", help="comma-separated PAY#### ids to run")
    ap.add_argument("--no-preflight", dest="preflight", action="store_false")
    ap.add_argument("--monitor", action="store_true",
                    help="run the Phase 5 sliding-window monitor alongside the batch")
    args = ap.parse_args(argv)

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("ERROR: set GEMINI_API_KEY (or put it in a .env file) first.", file=sys.stderr)
        return 2

    if args.fresh and os.path.exists(args.log):
        os.remove(args.log)

    invoices = load_invoices()
    payments = load_payments()
    with open(os.path.join(DATA_DIR, "payment_id_map.json"), encoding="utf-8") as f:
        ref_to_pid = json.load(f)
    by_pid = {ref_to_pid.get(p.payment_ref, p.payment_ref): p for p in payments}

    if args.only:
        wanted = [s.strip().upper() for s in args.only.split(",") if s.strip()]
    else:
        wanted = sorted(by_pid)

    done = set() if args.redo else _already_done(args.log)
    queue = [pid for pid in wanted if pid not in done]
    skipped = len(wanted) - len(queue)
    if args.limit:
        queue = queue[: args.limit]

    if not queue:
        print("nothing to do — every requested payment is already in the log.")
        return 0

    bridge = ToolBridge(invoices)
    recon = Reconciler(invoices, payment_id_map=ref_to_pid)
    print(f"model: {recon.model}   preflight: {args.preflight}   "
          f"payments: {len(queue)} (of {len(wanted)}; {skipped} already logged)\n")

    monitor = None
    if args.monitor:
        from monitor.live import LiveMonitor

        # quiet: the batch already prints a line per payment; we only want the
        # monitor's window stats and its alert banners layered on top.
        monitor = LiveMonitor(total=len(queue), quiet=True)

    started = time.monotonic()
    ok = err = 0
    tok_in = tok_out = 0
    try:
        for i, pid in enumerate(queue, 1):
            pay = by_pid[pid]
            pf = build_preflight(pay, bridge) if args.preflight else None
            hint = f"  pre:{pf.suggested_match_type or '?'}" if pf else ""
            print(f"[{i:>3}/{len(queue)}] {pid} {pay.source:<7} ref={pay.reference!r:<22}{hint}", flush=True)

            rec = recon.reconcile(pay, preflight=pf)
            append_record(rec, args.log)

            print(f"          -> {_fmt_res(rec)}", flush=True)
            if monitor is not None:
                stats, events = monitor.feed(rec)
                print(f"          .. window({stats.window_n}): {stats.summary()}", flush=True)
                for ev in events:
                    print(f"          {ev.line()}", flush=True)
            if rec.error:
                err += 1
            else:
                ok += 1
            tok_in += rec.usage.get("input_tokens", 0)
            tok_out += rec.usage.get("output_tokens", 0)
    except KeyboardInterrupt:
        print(f"\n\ninterrupted after {ok + err} payment(s). "
              f"Re-run `python -m eval.run_batch` to resume.", file=sys.stderr)
        return 130

    elapsed = time.monotonic() - started
    n = ok + err
    print("\n" + "=" * 70)
    print(f"done: {ok} resolved, {err} error(s) in {elapsed / 60:.1f} min "
          f"({n / (elapsed / 60):.1f} payments/min)")
    print(f"tokens: {tok_in:,} in / {tok_out:,} out"
          + (f"  (~{(tok_in + tok_out) // n:,}/payment)" if n else ""))
    print(f"decision log -> {args.log}")
    if monitor is not None:
        monitor.print_summary()
    print("next: python -m eval.evaluate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
