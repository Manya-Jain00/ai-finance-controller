"""Phase 6 deliverable — a scripted run of judge-style questions.

    python -m qa.demo                # runs the questions against Gemini
    python -m qa.demo --json out.json

Asks the settlement Q&A layer a handful of questions a judge might ask live and
prints each answer with the log queries that produced it. Writes a transcript to
qa/demo_output.txt and a machine summary to qa/demo_summary.json.

Requires GEMINI_API_KEY (or a .env file). Reads only the decision log.
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

from .assistant import SettlementQA
from .store import DecisionStore, default_log_path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Questions phrased the way a finance reviewer would ask them out loud.
QUESTIONS = [
    "Why didn't payment PAY0121 match?",
    "Show me every combined payment above Rs 100,000.",
    "How many payments did we reconcile, and what's the total value matched?",
    "Which payments had a gateway fee deducted, and what was the largest fee taken?",
    "Were there any payments where the agent overrode its own pre-flight analysis? What happened?",
    "Give me the breakdown of resolutions by match type.",
    "List the lowest-confidence matches and say why the agent was unsure.",
]


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    ap.add_argument("--json", default=os.path.join(_THIS_DIR, "demo_summary.json"))
    ap.add_argument("--transcript", default=os.path.join(_THIS_DIR, "demo_output.txt"))
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)

    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: set GEMINI_API_KEY (or put it in a .env file) first.", file=sys.stderr)
        return 2

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    path = args.log or default_log_path()
    store = DecisionStore.load(path)
    qa = SettlementQA(store, model=args.model)

    transcript = open(args.transcript, "w", encoding="utf-8")
    out = _Tee(sys.stdout, transcript)

    s = store.summary()
    print("=" * 88, file=out)
    print("  SETTLEMENT Q&A — judge-style questions against the decision log", file=out)
    print(f"  log: {os.path.relpath(path)}   {len(store)} decisions   "
          f"{s['matched_count']} matched / {s['exception_count']} exceptions   model: {qa.model}", file=out)
    print("=" * 88, file=out)

    results = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n[{i}] {q}\n" + "-" * 88, file=out)
        ans = qa.ask(q)
        # the free tier hands out 503s under load; give a failed question two more goes
        for _ in range(2):
            if not ans.error:
                break
            print(f"    (retrying after: {str(ans.error)[:60]})", file=out)
            time.sleep(20)
            ans = qa.ask(q)
        for tc in ans.tool_calls:
            args_s = ", ".join(f"{k}={v!r}" for k, v in tc["input"].items())
            print(f"    · {tc['tool']}({args_s})", file=out)
        print(file=out)
        if ans.error:
            print(f"    [ERROR] {ans.error}", file=out)
        else:
            print(_indent(ans.answer), file=out)
        meta = f"    -- {len(ans.tool_calls)} log queries"
        if ans.payment_ids:
            meta += f"; cites {', '.join(ans.payment_ids)}"
        meta += f"; {ans.latency_s:.1f}s"
        print("\n" + meta, file=out)
        results.append({
            "question": q,
            "answer": ans.answer,
            "error": ans.error,
            "tool_calls": [{"tool": tc["tool"], "input": tc["input"]} for tc in ans.tool_calls],
            "cited_payment_ids": ans.payment_ids,
            "latency_s": ans.latency_s,
        })

    answered = sum(1 for r in results if not r["error"])
    grounded = sum(1 for r in results if r["tool_calls"])
    print("\n" + "=" * 88, file=out)
    print(f"  {answered}/{len(results)} answered; {grounded}/{len(results)} hit the log at least once", file=out)
    print("=" * 88, file=out)
    transcript.close()

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump({
            "log": os.path.relpath(path),
            "model": qa.model,
            "answered": answered,
            "total": len(results),
            "results": results,
        }, f, indent=2)
    print(f"\ntranscript -> {os.path.relpath(args.transcript)}   summary -> {os.path.relpath(args.json)}")
    return 0 if answered == len(results) else 1


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
