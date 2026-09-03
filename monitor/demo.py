"""Phase 5 demo — the live monitor catching a bad batch mid-run.

Splices the healthy Phase 4 decision log together with the injected bad batch
(`data/bad_batch/`, resolved by the agent into `monitor/bad_batch_log.jsonl`),
orders everything by payment date the way it would actually have arrived, and
replays the stream through `LiveMonitor`.

The bad batch is dated late-Q1, so it lands in the middle of the stream: you see
the window match rate collapse, one or more alerts FIRE, and then CLEAR again as
the healthy Apr-Jun payments resume.

    python -m monitor.demo                 # uses monitor/bad_batch_log.jsonl
    python -m monitor.demo --offline       # synthesise the bad-batch records
                                           #   (deterministic, no API) if the
                                           #   real run isn't available

Writes monitor/demo_output.txt and monitor/demo_summary.json. Exit code is 0
only if an alert both fired during the bad segment and cleared afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from agent.decision_log import DEFAULT_LOG_PATH, load_log

from .live import LiveMonitor
from .run_bad_batch import BAD_DIR, BAD_LOG

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_TXT = os.path.join(_HERE, "demo_output.txt")
OUT_JSON = os.path.join(_HERE, "demo_summary.json")


def _synth_bad_records() -> list[dict]:
    """Deterministic stand-in for the agent on the bad batch (no API).

    Runs the real pre-flight; when its reference and customer tools all whiff
    the agent would be left guessing, so we mirror that: follow a confident
    pre-flight, otherwise raise a low-confidence exception.
    """
    from agent.preflight import build_preflight
    from agent.tools_bridge import ToolBridge
    from tools.loaders import (
        load_bank_transactions,
        load_gateway_settlements,
        load_invoices,
    )

    invoices = load_invoices()
    bridge = ToolBridge(invoices)
    bank = load_bank_transactions(os.path.join(BAD_DIR, "bank_wire_transactions.csv"))
    gw = load_gateway_settlements(os.path.join(BAD_DIR, "gateway_settlements.csv"))
    with open(os.path.join(BAD_DIR, "payment_id_map.json"), encoding="utf-8") as f:
        ref_to_pid = json.load(f)

    out = []
    for pay in bank + gw:
        pf = build_preflight(pay, bridge)
        pid = ref_to_pid.get(pay.payment_ref, pay.payment_ref)
        st = pf.suggested_match_type
        if st and st != "exception" and pf.suggested_confidence >= 0.85:
            mt, ids, conf = st, pf.suggested_invoice_ids, pf.suggested_confidence
            reason = f"followed pre-flight: {pf.rationale}"
        elif st and st != "exception":
            mt, ids, conf = st, pf.suggested_invoice_ids, 0.55
            reason = "weak amount-only lead; reference unparseable, payer unidentified"
        else:
            mt, ids, conf = "exception", [], 0.70
            reason = ("reference is in an unrecognised PO format, payer could not be "
                      "identified, and no invoice/combination matched the amount")
        out.append({
            "payment_id": pid, "payment_ref": pay.payment_ref, "source": pay.source,
            "date": pay.date, "amount_received": pay.amount_received,
            "reference": pay.reference, "counterparty": pay.counterparty_name,
            "resolution": {"match_type": mt, "invoice_ids": ids, "confidence": conf,
                           "reasoning": reason},
            "preflight": pf.to_dict(),
            "tool_calls": [{"tool": t, "input": {}, "output": "..."} for t in pf.strategies_run],
            "iterations": len(pf.strategies_run), "forced_retry": mt == "exception",
            "model": "synthetic(preflight)", "error": None,
        })
    return out


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--healthy-log", default=DEFAULT_LOG_PATH)
    ap.add_argument("--bad-log", default=BAD_LOG)
    ap.add_argument("--offline", action="store_true",
                    help="synthesise the bad-batch records instead of reading bad-log")
    ap.add_argument("--window", type=int, default=20)
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not os.path.exists(args.healthy_log):
        print(f"no healthy decision log at {args.healthy_log} — run Phase 4 first.", file=sys.stderr)
        return 2

    healthy = load_log(args.healthy_log)
    for r in healthy:
        r["_segment"] = "healthy"

    if args.offline or not os.path.exists(args.bad_log):
        if not args.offline:
            print(f"(no {args.bad_log} — synthesising the bad batch offline)\n")
        bad = _synth_bad_records()
    else:
        bad = load_log(args.bad_log)
    for r in bad:
        r["_segment"] = "bad_batch"

    stream = sorted(healthy + bad, key=lambda r: (r.get("date", ""), r.get("payment_id", "")))

    with open(OUT_TXT, "w", encoding="utf-8") as fh:
        mon = LiveMonitor(window=args.window, total=len(stream), stream=_Tee(sys.stdout, fh))
        mon.feed_all(stream)
        mon.print_summary()

    # ---- did the monitor actually do its job? ---------------------------
    bad_span = [i for i, r in enumerate(stream, 1) if r["_segment"] == "bad_batch"]
    lo, hi = min(bad_span), max(bad_span)
    fires = [e for e in mon.alerts.events if e.state == "FIRED"]
    clears = [e for e in mon.alerts.events if e.state == "CLEARED"]
    fired_in_bad = [e for e in fires if lo <= e.at_record <= hi + args.window]
    cleared_after = [e for e in clears if e.at_record > hi]

    summary = mon.summary()
    summary["bad_batch_records"] = f"{lo}..{hi}"
    summary["fired_during_bad_batch"] = [e.rule.key for e in fired_in_bad]
    summary["cleared_after_bad_batch"] = [e.rule.key for e in cleared_after]
    summary["bad_batch_source"] = "synthetic" if (args.offline or not os.path.exists(args.bad_log)) else args.bad_log
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=" * 100)
    print(f"  bad batch occupied stream records {lo}-{hi}")
    print(f"  alerts fired during it:      {[e.rule.label for e in fired_in_bad] or 'NONE'}")
    print(f"  alerts cleared afterwards:   {[e.rule.label for e in cleared_after] or 'NONE'}")
    print(f"  transcript -> {OUT_TXT}")
    print(f"  summary    -> {OUT_JSON}")
    print("=" * 100)

    ok = bool(fired_in_bad) and bool(cleared_after)
    if not ok:
        print("\nDEMO CHECK FAILED: expected an alert to fire during the bad batch and clear after.",
              file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
