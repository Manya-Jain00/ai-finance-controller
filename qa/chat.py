"""Interactive settlement Q&A — ask the decision log questions in plain English.

    python -m qa.chat                      # REPL over agent/decision_log.jsonl
    python -m qa.chat --log path/to.jsonl
    python -m qa.chat "why didn't PAY0121 match?"   # one-shot, then exit
    python -m qa.chat --show-tools         # also print each tool call

Requires GEMINI_API_KEY in the environment (or a .env file). Never reads the
ground truth — answers come only from the decision log.
"""

from __future__ import annotations

import argparse
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import os

from .assistant import SettlementQA
from .store import DecisionStore, default_log_path


def _print_answer(ans, *, show_tools: bool) -> None:
    if show_tools and ans.tool_calls:
        for tc in ans.tool_calls:
            print(f"  · {tc['tool']}({_fmt_args(tc['input'])})")
    if ans.error:
        print(f"  [error] {ans.error}")
        return
    print(ans.answer)
    tail = []
    if ans.payment_ids:
        tail.append("cites " + ", ".join(ans.payment_ids))
    tail.append(f"{len(ans.tool_calls)} log queries")
    tail.append(f"{ans.latency_s:.1f}s")
    print(f"  \033[2m({'; '.join(tail)})\033[0m")


def _fmt_args(d: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in d.items())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question", nargs="*", help="a single question; omit for a REPL")
    ap.add_argument("--log", default=None, help="decision log path")
    ap.add_argument("--show-tools", action="store_true", help="print each log query")
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
    try:
        store = DecisionStore.load(path)
    except FileNotFoundError:
        print(f"no decision log at {path}", file=sys.stderr)
        return 2

    qa = SettlementQA(store, model=args.model)
    s = store.summary()
    print(f"loaded {len(store)} decisions from {os.path.relpath(path)}  "
          f"({s['matched_count']} matched, {s['exception_count']} exceptions)   model: {qa.model}")

    if args.question:
        ans = qa.ask(" ".join(args.question), verbose=args.show_tools)
        print()
        _print_answer(ans, show_tools=False)
        return 0 if not ans.error else 1

    print("ask a question, or 'quit'. context carries across turns.\n")
    while True:
        try:
            q = input("\033[1m? \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not q:
            continue
        if q.lower() in ("quit", "exit", "q"):
            return 0
        ans = qa.ask(q, verbose=args.show_tools, remember=True)
        _print_answer(ans, show_tools=False)
        print()


if __name__ == "__main__":
    raise SystemExit(main())
