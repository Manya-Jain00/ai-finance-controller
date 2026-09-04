"""End-to-end proof: an AI-mapped, never-seen-before CSV flows into the
UNCHANGED reconciliation agent from Phase 3.

    python -m onboard.demo

`onboard/demo_foreign_bank.csv` is shaped nothing like this project's own data
— different column names (`txn_ref`, `posting_date`, `debit_amt`, `narration`,
`payer`), a bank nobody generated. This script:

  1. asks the model to map its columns onto Payment (propose_mapping),
  2. applies that mapping with plain Python (apply_mapping) to get real
     `tools.loaders.Payment` objects,
  3. hands each one to `agent.reconciler.Reconciler` — the exact same class
     that resolved the graded 130-payment batch, completely unmodified,
  4. prints what it decided.

Three of the four demo rows reference real, still-open invoices from
data/invoices.csv (INV0005, INV0007, INV0008); the fourth has no invoice
behind it at all, on purpose. Requires GEMINI_API_KEY.
"""

from __future__ import annotations

import csv
import json
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from agent.reconciler import Reconciler
from tools.loaders import load_invoices

from .mapper import apply_mapping, propose_mapping

_HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_CSV = os.path.join(_HERE, "demo_foreign_bank.csv")


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: set GEMINI_API_KEY (or put it in a .env file) first.", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    with open(DEMO_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    headers = list(rows[0].keys())

    print(f"'{os.path.basename(DEMO_CSV)}' — a file this project has never seen, "
          f"columns: {headers}\n")
    print("step 1 — asking the model to map it onto Payment...")
    mapping = propose_mapping(headers, rows, "payment")
    for g in mapping.guesses:
        print(f"    {g.target_field:<20} <- {g.source_column or '(not present)'}")
    if mapping.missing_required():
        print(f"  missing required fields: {mapping.missing_required()} — stopping.")
        return 1

    print("\nstep 2 — applying the confirmed mapping (plain Python, no LLM)...")
    payments = apply_mapping(rows, mapping, "payment", source_label="acme_bank_demo")
    print(f"  built {len(payments)} Payment objects, e.g. {payments[0]}\n")

    print("step 3 — handing them to the UNCHANGED Phase-3 agent...")
    recon = Reconciler(load_invoices())
    print(f"  model: {recon.model}\n")
    for p in payments:
        print("=" * 78)
        print(f"{p.payment_ref}  received={p.amount_received}  reference={p.reference!r}")
        rec = recon.reconcile(p, verbose=True)
        if rec.error:
            print(f"  ERROR: {rec.error}")
        else:
            r = rec.resolution
            print(f"  -> {r.match_type} {r.invoice_ids}  confidence={r.confidence:.2f}")
            print(f"     {r.reasoning}")
    print("=" * 78)
    print("\nEvery step above after 'step 1' is code that already existed for the "
          "graded 130-payment batch — nothing in agent/, tools/, eval/, monitor/, "
          "or qa/ was touched to make this work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
