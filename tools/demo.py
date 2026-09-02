"""Phase 2 demo — every tool exercised directly, no agent, no LLM.

    python tools/demo.py

Picks one real payment of each mess type from the generated data and shows
the tool call that cracks it. This is the "called directly from a script"
checkpoint before Phase 3 wires anything to an LLM.
"""

from __future__ import annotations

import json
import os

from tools.loaders import DATA_DIR, load_invoices, load_payments
from tools.reconciliation_tools import (
    check_combination,
    check_fee_schedule,
    find_invoices_by_amount,
    find_invoices_by_customer,
    parse_remittance_reference,
)


def main() -> None:
    invoices = load_invoices()
    by_id = {i.invoice_id: i for i in invoices}
    payments = {p.payment_ref: p for p in load_payments()}

    with open(os.path.join(DATA_DIR, "payment_id_map.json"), encoding="utf-8") as f:
        ref_to_pid = json.load(f)
    with open(os.path.join(DATA_DIR, "ground_truth.json"), encoding="utf-8") as f:
        gt = json.load(f)
    pid_to_ref = {v: k for k, v in ref_to_pid.items()}

    def first_pid(match_type: str) -> str:
        return next(p for p, t in gt.items() if t["match_type"] == match_type)

    def show(title: str, pid: str) -> None:
        pay = payments[pid_to_ref[pid]]
        print(f"\n{'='*70}\n{title}  ({pid} via {pay.source} {pay.payment_ref})")
        print(f"  reference={pay.reference!r}  received={pay.amount_received}  "
              f"counterparty={pay.counterparty_name or pay.counterparty_email}")
        print(f"  ground truth -> {gt[pid]['match_type']}: {gt[pid]['invoice_ids']}")
        return pay

    # --- single_full: parse + amount lookup --------------------------------
    pid = first_pid("single_full")
    pay = show("SINGLE FULL  ->  parse_remittance_reference + find_invoices_by_amount", pid)
    print("  parse_remittance_reference:", parse_remittance_reference(pay.reference).to_dict())
    hits = find_invoices_by_amount(invoices, pay.amount_received, abs_tolerance=0.05)
    print("  find_invoices_by_amount:", [h.invoice_id for h in hits])

    # --- combined: parse (range/list) + check_combination ------------------
    pid = first_pid("combined")
    pay = show("COMBINED  ->  parse_remittance_reference + check_combination", pid)
    parsed = parse_remittance_reference(pay.reference)
    print("  parse_remittance_reference:", parsed.to_dict())
    cust = by_id[gt[pid]["invoice_ids"][0]].customer_id
    pool = find_invoices_by_customer(invoices, customer_id=cust)
    combos = check_combination(pool, pay.amount_received, max_k=3)
    print("  check_combination:", combos[0].to_dict() if combos else None)

    # --- partial: fee schedule says NO -----------------------------------
    pid = first_pid("partial")
    pay = show("PARTIAL  ->  check_fee_schedule (expected: not explained)", pid)
    inv_amt = by_id[gt[pid]["invoice_ids"][0]].invoice_amount
    print(f"  invoice amount {inv_amt}")
    print("  check_fee_schedule:", check_fee_schedule(pay.amount_received, inv_amt).to_dict())

    # --- fee_deducted: fee schedule says YES ------------------------------
    pid = first_pid("fee_deducted")
    pay = show("FEE DEDUCTED  ->  check_fee_schedule (expected: explained)", pid)
    inv_amt = by_id[gt[pid]["invoice_ids"][0]].invoice_amount
    print(f"  invoice amount {inv_amt}")
    print("  check_fee_schedule:", check_fee_schedule(pay.amount_received, inv_amt).to_dict())

    # --- orphan: nothing resolves --------------------------------------
    pid = first_pid("orphan")
    pay = show("ORPHAN  ->  every tool comes up empty", pid)
    print("  parse_remittance_reference:", parse_remittance_reference(pay.reference).to_dict())
    print("  find_invoices_by_amount:",
          [h.invoice_id for h in find_invoices_by_amount(invoices, pay.amount_received, abs_tolerance=0.05)])
    if pay.counterparty_name:
        print("  find_invoices_by_customer(name):",
              [h.invoice_id for h in find_invoices_by_customer(invoices, name=pay.counterparty_name)])
    print(f"\n{'='*70}\nAll five tools exercised directly. Phase 2 complete.")


if __name__ == "__main__":
    main()
