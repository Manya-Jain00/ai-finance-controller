"""The live monitor driver (Phase 5).

`LiveMonitor` ties the sliding-window tracker to the alert manager and renders a
running dashboard line for every payment, plus a banner whenever an alert fires
or clears. It works two ways:

  * **during a batch run** — `eval.run_batch --monitor` passes each finished
    `DecisionRecord` to `LiveMonitor.feed()` the moment it is logged, so the
    dashboard scrolls in real time alongside the run.

  * **replaying a saved log** — `python -m monitor.live agent/decision_log.jsonl`
    streams an existing decision log through the same code. This is how the
    Phase 5 demo shows the alert firing without spending API calls.

No ground truth is read here.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Iterable

from .alerts import AlertEvent, AlertManager, AlertRule
from .tracker import DEFAULT_MIN_SAMPLES, DEFAULT_WINDOW, SlidingWindowTracker, WindowStats


def _spark(values: list[float], lo: float = 0.0, hi: float = 1.0) -> str:
    ramp = " .:-=+*#%@"  # 10 levels, ASCII (Windows-console safe)
    out = []
    for v in values:
        t = 0.0 if hi == lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
        out.append(ramp[round(t * (len(ramp) - 1))])
    return "".join(out)


class LiveMonitor:
    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        rules: list[AlertRule] | None = None,
        total: int | None = None,
        stream=None,
        quiet: bool = False,
    ):
        self.tracker = SlidingWindowTracker(window=window, min_samples=min_samples)
        self.alerts = AlertManager(rules=list(rules) if rules else None) if rules else AlertManager()
        self.total = total
        self.out = stream or sys.stdout
        self.quiet = quiet
        self._printed_header = False

    # ------------------------------------------------------------------

    def feed(self, record: Any) -> tuple[WindowStats, list[AlertEvent]]:
        stats = self.tracker.observe(record)
        events = self.alerts.update(stats)
        if not self.quiet:
            self._render(record, stats, events)
        return stats, events

    def feed_all(self, records: Iterable[Any]) -> None:
        for r in records:
            self.feed(r)

    def on_record_hook(self) -> Callable[[Any], None]:
        """A one-arg callback for `eval.run_batch` to call per finished payment."""
        return lambda rec: self.feed(rec)

    # ------------------------------------------------------------------

    def _render(self, record: Any, stats: WindowStats, events: list[AlertEvent]) -> None:
        d = record.to_dict() if hasattr(record, "to_dict") else record
        if not self._printed_header:
            self._banner()
            self._printed_header = True

        res = d.get("resolution") or {}
        err = d.get("error")
        pid = d.get("payment_id") or d.get("payment_ref") or "?"
        src = (d.get("source") or "")[:7]
        if err:
            verdict = f"ERROR  {str(err)[:24]}"
        else:
            verdict = f"{res.get('match_type', '?'):<12} {res.get('confidence', 0):.2f}"

        n = stats.total_seen
        prog = f"{n:>3}/{self.total}" if self.total else f"{n:>4}"
        warm = " " if stats.warm else "~"  # ~ = window still warming up
        active = len(self.alerts.active)
        flag = f"  <<{active} ALERT{'S' if active != 1 else ''}>>" if active else ""

        print(f"[{prog}]{warm}{pid:<8} {src:<7} {verdict:<20} | {stats.summary()}{flag}",
              file=self.out, flush=True)

        for ev in events:
            self._alert_banner(ev)

    def _banner(self) -> None:
        w = self.tracker.window
        print("=" * 100, file=self.out)
        print(f"  LIVE RECONCILIATION MONITOR   sliding window = {w} payments   "
              f"(alerts arm after {self.tracker.min_samples})", file=self.out)
        print("=" * 100, file=self.out)
        print("  columns:  [n]  payment  source  verdict conf | "
              "window: match% conf exc% retry% pf-disagree% effort", file=self.out)
        print("-" * 100, file=self.out)

    def _alert_banner(self, ev: AlertEvent) -> None:
        bar = "!" * 100 if ev.state == "FIRED" else "." * 100
        print(bar, file=self.out)
        print(f"  {ev.line()}", file=self.out)
        if ev.state == "FIRED":
            print(f"  why: {ev.rule.why}", file=self.out)
        print(bar, file=self.out, flush=True)

    # ------------------------------------------------------------------

    def summary(self) -> dict:
        hist = self.tracker.history
        rep = self.alerts.report()
        rep["payments_seen"] = self.tracker.total_seen
        rep["match_rate_trace"] = [round(s.match_rate, 3) for s in hist]
        return rep

    def print_summary(self) -> None:
        rep = self.summary()
        hist = self.tracker.history
        print("\n" + "=" * 100, file=self.out)
        print("  MONITOR SUMMARY", file=self.out)
        print("=" * 100, file=self.out)
        if hist:
            trace = [s.match_rate for s in hist]
            print(f"  window match-rate trace ({len(trace)} ticks):", file=self.out)
            print("    " + _spark(trace), file=self.out)
            lo = min(hist, key=lambda s: s.match_rate)
            print(f"    lowest: {lo.match_rate:.0%} at record {lo.total_seen}", file=self.out)
        fires = [e for e in self.alerts.events if e.state == "FIRED"]
        if fires:
            print(f"\n  {len(fires)} alert(s) fired during the run:", file=self.out)
            for e in self.alerts.events:
                print(f"    {e.line()}", file=self.out)
        else:
            print("\n  no alerts fired — the run stayed healthy.", file=self.out)
        if self.alerts.active:
            print(f"\n  STILL FIRING at end of run: "
                  f"{', '.join(r.label for r in self.alerts.firing)}", file=self.out)
        print(file=self.out)


# ---------------------------------------------------------------------------
# CLI — replay a saved decision log
# ---------------------------------------------------------------------------

def _load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main(argv: list[str] | None = None) -> int:
    from agent.decision_log import DEFAULT_LOG_PATH

    ap = argparse.ArgumentParser(description="Replay a decision log through the live monitor.")
    ap.add_argument("log", nargs="?", default=DEFAULT_LOG_PATH)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    ap.add_argument("--json", help="write the monitor summary to this path")
    args = ap.parse_args(argv)

    try:  # Windows consoles default to cp1252; keep the dashboard printable
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        rows = _load_jsonl(args.log)
    except FileNotFoundError:
        print(f"no decision log at {args.log}", file=sys.stderr)
        return 2

    mon = LiveMonitor(window=args.window, min_samples=args.min_samples, total=len(rows))
    mon.feed_all(rows)
    mon.print_summary()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(mon.summary(), f, indent=2)
        print(f"summary -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
