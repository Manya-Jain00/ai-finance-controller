import csv
import json
import random
from datetime import date, timedelta

random.seed(42)

N_CUSTOMERS = 40
N_DECOY_INVOICES = 20

COUNTS = {
    "single_full": 65,
    "combined": 20,
    "partial": 20,
    "fee_deducted": 15,
    "orphan": 10,
}

BASE_DATE = date(2026, 1, 1)


def make_customers(n):
    customers = []
    for i in range(1, n + 1):
        customers.append({
            "customer_id": f"CUST{i:03d}",
            "customer_name": f"Customer {i:03d} Pvt Ltd",
        })
    return customers


def make_invoice(invoice_id, customer):
    invoice_date = BASE_DATE + timedelta(days=random.randint(0, 150))
    amount = round(random.uniform(5000, 95000), 2)
    return {
        "invoice_id": f"INV{invoice_id:04d}",
        "customer_id": customer["customer_id"],
        "customer_name": customer["customer_name"],
        "invoice_amount": amount,
        "invoice_date": invoice_date.isoformat(),
        "due_date": (invoice_date + timedelta(days=30)).isoformat(),
    }


def build_invoice_pool(customers, total_needed):
    invoices = []
    invoice_id = 1
    while len(invoices) < total_needed:
        customer = random.choice(customers)
        invoices.append(make_invoice(invoice_id, customer))
        invoice_id += 1
    return invoices


def payment_date_for(invoice):
    inv_date = date.fromisoformat(invoice["invoice_date"])
    return inv_date + timedelta(days=random.randint(2, 4))


def build_ground_truth_and_payments(invoices):
    # keep sequential order so we can carve out contiguous invoice_id blocks
    # for "consecutive" combined payments before shuffling the rest
    sequential_pool = invoices[:]

    payments = []
    ground_truth = {}
    bank_seq = 1
    gw_seq = 1

    def new_payment_id():
        return f"PAY{len(ground_truth) + 1:04d}"

    n_consecutive_combined = COUNTS["combined"] // 2
    consecutive_groups = []
    for i in range(n_consecutive_combined):
        n = random.choice([2, 3])
        start = random.randint(0, len(sequential_pool) - n)
        group = sequential_pool[start:start + n]
        if any(inv in [g for grp in consecutive_groups for g in grp] for inv in group):
            continue  # skip on overlap, rare with this pool size
        consecutive_groups.append(group)
        for inv in group:
            sequential_pool.remove(inv)

    pool = sequential_pool
    random.shuffle(pool)

    def take(n):
        chunk = pool[:n]
        del pool[:n]
        return chunk

    # single_full
    for _ in range(COUNTS["single_full"]):
        inv = take(1)[0]
        pay_date = payment_date_for(inv)
        source = random.choice(["bank", "gateway"])
        pid = new_payment_id()
        if source == "bank":
            payments.append({
                "source": "bank",
                "transaction_id": f"TXN{bank_seq:04d}",
                "value_date": pay_date.isoformat(),
                "amount": inv["invoice_amount"],
                "remittance_info": inv["invoice_id"],
                "sender_name": inv["customer_name"],
                "_payment_id": pid,
            })
            bank_seq += 1
        else:
            payments.append({
                "source": "gateway",
                "settlement_id": f"STL{gw_seq:04d}",
                "txn_date": pay_date.isoformat(),
                "gross_amount": inv["invoice_amount"],
                "fee": 0.0,
                "net_amount": inv["invoice_amount"],
                "payer_reference": inv["invoice_id"],
                "payer_email": f"{inv['customer_id'].lower()}@example.com",
                "_payment_id": pid,
            })
            gw_seq += 1
        ground_truth[pid] = {
            "match_type": "single_full",
            "invoice_ids": [inv["invoice_id"]],
            "notes": "Exact single invoice match.",
        }

    # combined (2-3 invoices, same customer, shorthand reference)
    for combo_idx in range(COUNTS["combined"]):
        if combo_idx < len(consecutive_groups):
            group = consecutive_groups[combo_idx]
        else:
            n = random.choice([2, 3])
            group = take(n)
        # force same customer for realism (a customer paying several invoices at once)
        customer_id = group[0]["customer_id"]
        customer_name = group[0]["customer_name"]
        for inv in group[1:]:
            inv["customer_id"] = customer_id
            inv["customer_name"] = customer_name
        total = round(sum(i["invoice_amount"] for i in group), 2)
        pay_date = payment_date_for(group[-1])
        ids_sorted = sorted(group, key=lambda i: i["invoice_id"])
        first_num = int(ids_sorted[0]["invoice_id"][3:])
        last_num = int(ids_sorted[-1]["invoice_id"][3:])
        if last_num - first_num == len(group) - 1:
            shorthand = f"INV{first_num:04d}-{last_num:04d}"
        else:
            shorthand = "+".join(i["invoice_id"] for i in ids_sorted)
        pid = new_payment_id()
        source = random.choice(["bank", "gateway"])
        if source == "bank":
            payments.append({
                "source": "bank",
                "transaction_id": f"TXN{bank_seq:04d}",
                "value_date": pay_date.isoformat(),
                "amount": total,
                "remittance_info": shorthand,
                "sender_name": group[0]["customer_name"],
                "_payment_id": pid,
            })
            bank_seq += 1
        else:
            payments.append({
                "source": "gateway",
                "settlement_id": f"STL{gw_seq:04d}",
                "txn_date": pay_date.isoformat(),
                "gross_amount": total,
                "fee": 0.0,
                "net_amount": total,
                "payer_reference": shorthand,
                "payer_email": f"{group[0]['customer_id'].lower()}@example.com",
                "_payment_id": pid,
            })
            gw_seq += 1
        ground_truth[pid] = {
            "match_type": "combined",
            "invoice_ids": [i["invoice_id"] for i in group],
            "notes": f"Combined payment covering {len(group)} invoices, reference '{shorthand}'.",
        }

    # partial (payment is a fraction of the invoice)
    for _ in range(COUNTS["partial"]):
        inv = take(1)[0]
        fraction = round(random.uniform(0.4, 0.8), 2)
        paid_amount = round(inv["invoice_amount"] * fraction, 2)
        pay_date = payment_date_for(inv)
        pid = new_payment_id()
        source = random.choice(["bank", "gateway"])
        if source == "bank":
            payments.append({
                "source": "bank",
                "transaction_id": f"TXN{bank_seq:04d}",
                "value_date": pay_date.isoformat(),
                "amount": paid_amount,
                "remittance_info": inv["invoice_id"],
                "sender_name": inv["customer_name"],
                "_payment_id": pid,
            })
            bank_seq += 1
        else:
            payments.append({
                "source": "gateway",
                "settlement_id": f"STL{gw_seq:04d}",
                "txn_date": pay_date.isoformat(),
                "gross_amount": paid_amount,
                "fee": 0.0,
                "net_amount": paid_amount,
                "payer_reference": inv["invoice_id"],
                "payer_email": f"{inv['customer_id'].lower()}@example.com",
                "_payment_id": pid,
            })
            gw_seq += 1
        ground_truth[pid] = {
            "match_type": "partial",
            "invoice_ids": [inv["invoice_id"]],
            "notes": f"Partial payment: {paid_amount} of {inv['invoice_amount']} ({fraction*100:.0f}%).",
        }

    # fee_deducted (gateway only)
    for _ in range(COUNTS["fee_deducted"]):
        inv = take(1)[0]
        fee = round(inv["invoice_amount"] * random.uniform(0.015, 0.03), 2)
        net = round(inv["invoice_amount"] - fee, 2)
        pay_date = payment_date_for(inv)
        pid = new_payment_id()
        payments.append({
            "source": "gateway",
            "settlement_id": f"STL{gw_seq:04d}",
            "txn_date": pay_date.isoformat(),
            "gross_amount": inv["invoice_amount"],
            "fee": fee,
            "net_amount": net,
            "payer_reference": inv["invoice_id"],
            "payer_email": f"{inv['customer_id'].lower()}@example.com",
            "_payment_id": pid,
        })
        gw_seq += 1
        ground_truth[pid] = {
            "match_type": "fee_deducted",
            "invoice_ids": [inv["invoice_id"]],
            "notes": f"Net amount {net} = invoice {inv['invoice_amount']} minus fee {fee}.",
        }

    # orphan (no valid counterpart at all)
    for _ in range(COUNTS["orphan"]):
        pay_date = BASE_DATE + timedelta(days=random.randint(0, 150))
        amount = round(random.uniform(3000, 40000), 2)
        pid = new_payment_id()
        source = random.choice(["bank", "gateway"])
        fake_customer = random.choice(["Unknown Sender Ltd", "Refund Co", "Unregistered Payer"])
        if source == "bank":
            payments.append({
                "source": "bank",
                "transaction_id": f"TXN{bank_seq:04d}",
                "value_date": pay_date.isoformat(),
                "amount": amount,
                "remittance_info": "N/A",
                "sender_name": fake_customer,
                "_payment_id": pid,
            })
            bank_seq += 1
        else:
            payments.append({
                "source": "gateway",
                "settlement_id": f"STL{gw_seq:04d}",
                "txn_date": pay_date.isoformat(),
                "gross_amount": amount,
                "fee": 0.0,
                "net_amount": amount,
                "payer_reference": "UNKNOWN",
                "payer_email": "unknown@example.com",
                "_payment_id": pid,
            })
            gw_seq += 1
        ground_truth[pid] = {
            "match_type": "orphan",
            "invoice_ids": [],
            "notes": "No corresponding invoice exists; planted as a true exception.",
        }

    random.shuffle(payments)
    return payments, ground_truth


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})


def main():
    customers = make_customers(N_CUSTOMERS)
    total_invoices_needed = (
        COUNTS["single_full"]
        + COUNTS["combined"] * 2  # approx, real count computed below
        + COUNTS["partial"]
        + COUNTS["fee_deducted"]
        + N_DECOY_INVOICES
    )
    # combined uses 2 or 3 per group; overshoot the pool a bit to be safe
    total_invoices_needed += COUNTS["combined"]
    invoices = build_invoice_pool(customers, total_invoices_needed)

    payments, ground_truth = build_ground_truth_and_payments(invoices)

    consumed_ids = {inv_id for gt in ground_truth.values() for inv_id in gt["invoice_ids"]}
    all_invoices = invoices  # includes decoys (never referenced) automatically

    write_csv(
        "data/invoices.csv",
        all_invoices,
        ["invoice_id", "customer_id", "customer_name", "invoice_amount", "invoice_date", "due_date"],
    )

    bank_rows = [p for p in payments if p["source"] == "bank"]
    gateway_rows = [p for p in payments if p["source"] == "gateway"]

    write_csv(
        "data/bank_wire_transactions.csv",
        bank_rows,
        ["transaction_id", "value_date", "amount", "remittance_info", "sender_name"],
    )
    write_csv(
        "data/gateway_settlements.csv",
        gateway_rows,
        ["settlement_id", "txn_date", "gross_amount", "fee", "net_amount", "payer_reference", "payer_email"],
    )

    payment_id_map = {}
    for p in bank_rows:
        payment_id_map[p["transaction_id"]] = p["_payment_id"]
    for p in gateway_rows:
        payment_id_map[p["settlement_id"]] = p["_payment_id"]

    with open("data/payment_id_map.json", "w", encoding="utf-8") as f:
        json.dump(payment_id_map, f, indent=2)

    with open("data/ground_truth.json", "w", encoding="utf-8") as f:
        json.dump(ground_truth, f, indent=2)

    print(f"Invoices generated: {len(all_invoices)} (decoys/unpaid: {len(all_invoices) - len(consumed_ids)})")
    print(f"Bank transactions: {len(bank_rows)}")
    print(f"Gateway settlements: {len(gateway_rows)}")
    print(f"Total payments: {len(payments)}")
    print(f"Ground truth entries: {len(ground_truth)}")


if __name__ == "__main__":
    main()
