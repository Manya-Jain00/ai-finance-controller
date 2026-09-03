"""Phase 5 — the deliberately-injected "bad batch".

A block of payments from customers who migrated to a new ERP mid-quarter. The
new system emits remittance references in a purchase-order format the Phase 2
parser was never built for (``PO#4471 / DT#20260615 / VND#ZR``) and pays from a
new email domain / trading name, so `customer_id_from_email` and the name match
both miss too. The PO document numbers are the ERP's own — they do **not**
correspond to our INV ids — so the reference is genuinely no help.

Every payment here still maps to a real open invoice (or a real 2-invoice sum):
a human doing the reconciliation by hand could resolve all of them. The point of
Phase 5 is that the agent *can't* — its reference and customer tools all whiff —
so it starts returning exceptions and low-confidence guesses, and the live
monitor should catch the match rate collapsing while the batch is still running.

Outputs (kept apart from the main dataset, all git-committed so the demo is
reproducible without re-running this):

    data/bad_batch/bank_wire_transactions.csv
    data/bad_batch/gateway_settlements.csv
    data/bad_batch/payment_id_map.json
    data/bad_batch/ground_truth.json      # only the Phase 4-style grader may read this

`data/invoices.csv` is NOT modified — the bad batch references invoices that are
already in the ledger but left unpaid by the main generator.
"""

from __future__ import annotations

import csv
import json
import os
import random
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "bad_batch")

SEED = 43
N_SINGLE = 6      # exact amount, one real invoice
N_COMBINED = 5    # exact sum of two real invoices, same customer
N_PARTIAL = 4     # 45-70% of one real invoice
BASE_ID = 900     # PAY0900+ so ids never collide with the main dataset


def _load_free_invoices() -> list[dict]:
    with open(os.path.join(_HERE, "ground_truth.json"), encoding="utf-8") as f:
        consumed = {i for v in json.load(f).values() for i in v["invoice_ids"]}
    with open(os.path.join(_HERE, "invoices.csv"), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    free = [r for r in rows if r["invoice_id"] not in consumed]
    for r in free:
        r["invoice_amount"] = round(float(r["invoice_amount"]), 2)
    return free


def _new_ref(rng: random.Random, date: str) -> str:
    """A PO-style reference the parser cannot decode (verified in tests)."""
    po = rng.randint(1000, 9999)
    vnd = rng.choice(["ZR", "KX", "NimbusRetail", "Orion", "TT"])
    d = date.replace("-", "")
    if rng.random() < 0.5:
        return f"PO#{po} / DT#{d} / VND#{vnd}"
    return f"{d}//{po}//{po + 1}"


def _new_email(rng: random.Random, cust_id: str) -> str:
    dom = rng.choice(["nimbusretail.co", "orion-group.example", "zrcorp.example", "kxtrading.example"])
    return f"{rng.choice(['ap', 'finance', 'accounts', 'remittance'])}@{dom}"


def _new_sender(rng: random.Random) -> str:
    return rng.choice(["Nimbus Retail LLP", "Orion Group", "ZR Corp", "KX Trading Co", "Kanix Enterprises"])


def build():
    rng = random.Random(SEED)
    free = _load_free_invoices()
    rng.shuffle(free)
    by_cust: dict[str, list[dict]] = defaultdict(list)
    for r in free:
        by_cust[r["customer_id"]].append(r)

    pool = list(free)
    pairs: list[list[dict]] = []
    for cid, invs in by_cust.items():
        while len(invs) >= 2 and len(pairs) < N_COMBINED:
            a, b = invs.pop(), invs.pop()
            pairs.append([a, b])
            pool.remove(a)
            pool.remove(b)

    def take_one() -> dict:
        return pool.pop()

    bank_rows, gw_rows = [], []
    id_map, gt = {}, {}
    bank_seq = gw_seq = 1
    pid_n = BASE_ID

    def emit(pid, group, amount, kind, note):
        nonlocal bank_seq, gw_seq
        inv0 = group[0]
        # late Q1 / early Q2: the "new ERP went live at quarter end" window. Sorted
        # by date this lands mid-stream in the batch, so the monitor demo shows the
        # alert both firing and clearing as the healthy Apr-Jun payments resume.
        date = rng.choice([
            "2026-03-27", "2026-03-28", "2026-03-29", "2026-03-30", "2026-03-31",
            "2026-04-01", "2026-04-02", "2026-04-03",
        ])
        ref = _new_ref(rng, date)
        source = rng.choice(["bank", "gateway"])
        if source == "bank":
            bank_rows.append({
                "transaction_id": f"BADTXN{bank_seq:03d}",
                "value_date": date,
                "amount": amount,
                "remittance_info": ref,
                "sender_name": _new_sender(rng),
            })
            id_map[f"BADTXN{bank_seq:03d}"] = pid
            bank_seq += 1
        else:
            gw_rows.append({
                "settlement_id": f"BADSTL{gw_seq:03d}",
                "txn_date": date,
                "gross_amount": amount,
                "fee": 0.0,
                "net_amount": amount,
                "payer_reference": ref,
                "payer_email": _new_email(rng, inv0["customer_id"]),
            })
            id_map[f"BADSTL{gw_seq:03d}"] = pid
            gw_seq += 1
        gt[pid] = {"match_type": kind,
                   "invoice_ids": [i["invoice_id"] for i in group],
                   "notes": note}

    for _ in range(N_SINGLE):
        inv = take_one()
        pid_n += 1
        emit(f"PAY{pid_n:04d}", [inv], inv["invoice_amount"], "single_full",
             "new-ERP reference; exact single-invoice match a human could make")

    for group in pairs:
        pid_n += 1
        total = round(sum(i["invoice_amount"] for i in group), 2)
        emit(f"PAY{pid_n:04d}", group, total, "combined",
             f"new-ERP reference; exact sum of {len(group)} invoices, same customer")

    for _ in range(N_PARTIAL):
        inv = take_one()
        pid_n += 1
        frac = round(rng.uniform(0.45, 0.70), 2)
        emit(f"PAY{pid_n:04d}", [inv], round(inv["invoice_amount"] * frac, 2), "partial",
             f"new-ERP reference; partial payment ({frac:.0%} of one invoice)")

    all_rows = [("bank", r) for r in bank_rows] + [("gateway", r) for r in gw_rows]
    rng.shuffle(all_rows)  # realistic interleaved arrival order

    os.makedirs(OUT_DIR, exist_ok=True)
    _write_csv(os.path.join(OUT_DIR, "bank_wire_transactions.csv"),
               [r for s, r in all_rows if s == "bank"],
               ["transaction_id", "value_date", "amount", "remittance_info", "sender_name"])
    _write_csv(os.path.join(OUT_DIR, "gateway_settlements.csv"),
               [r for s, r in all_rows if s == "gateway"],
               ["settlement_id", "txn_date", "gross_amount", "fee", "net_amount",
                "payer_reference", "payer_email"])
    with open(os.path.join(OUT_DIR, "payment_id_map.json"), "w", encoding="utf-8") as f:
        json.dump(id_map, f, indent=2)
    with open(os.path.join(OUT_DIR, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(gt, f, indent=2)

    print(f"bad batch -> {OUT_DIR}")
    print(f"  {len(bank_rows)} bank + {len(gw_rows)} gateway = {len(gt)} payments")
    for k in ("single_full", "combined", "partial"):
        print(f"  {k:<12} {sum(1 for v in gt.values() if v['match_type'] == k)}")


def _write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in fieldnames})


if __name__ == "__main__":
    build()
