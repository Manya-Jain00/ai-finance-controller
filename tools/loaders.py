"""Data loaders for the reconciliation tools.

These turn the raw CSV files (two mismatched-schema payment sources plus the
open-invoice ledger) into normalised Python objects the tools operate on.

No LLM is involved here. Everything is plain, deterministic parsing so the
tools in `reconciliation_tools.py` can be unit-tested in isolation.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, asdict
from typing import Iterable

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", "data"))

INVOICES_CSV = os.path.join(DATA_DIR, "invoices.csv")
BANK_CSV = os.path.join(DATA_DIR, "bank_wire_transactions.csv")
GATEWAY_CSV = os.path.join(DATA_DIR, "gateway_settlements.csv")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    customer_id: str
    customer_name: str
    invoice_amount: float
    invoice_date: str
    due_date: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Payment:
    """A single incoming payment, normalised across both source formats.

    `amount_received` is the money that actually landed in the account:
    the bank wire amount, or the gateway *net* amount (after fee).
    `gross_amount` is what the payer sent before any gateway fee
    (equal to `amount_received` for bank wires).
    """

    payment_ref: str            # transaction_id (bank) or settlement_id (gateway)
    source: str                 # "bank" | "gateway"
    date: str                   # ISO date the money moved
    amount_received: float
    gross_amount: float
    fee: float
    reference: str              # remittance_info / payer_reference, verbatim
    counterparty_name: str | None   # bank sender_name
    counterparty_email: str | None  # gateway payer_email

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_invoices(path: str = INVOICES_CSV) -> list[Invoice]:
    out: list[Invoice] = []
    for r in _rows(path):
        out.append(
            Invoice(
                invoice_id=r["invoice_id"].strip(),
                customer_id=r["customer_id"].strip(),
                customer_name=r["customer_name"].strip(),
                invoice_amount=round(float(r["invoice_amount"]), 2),
                invoice_date=r["invoice_date"].strip(),
                due_date=r["due_date"].strip(),
            )
        )
    return out


def load_bank_transactions(path: str = BANK_CSV) -> list[Payment]:
    out: list[Payment] = []
    for r in _rows(path):
        amount = round(float(r["amount"]), 2)
        out.append(
            Payment(
                payment_ref=r["transaction_id"].strip(),
                source="bank",
                date=r["value_date"].strip(),
                amount_received=amount,
                gross_amount=amount,
                fee=0.0,
                reference=r["remittance_info"].strip(),
                counterparty_name=(r.get("sender_name") or "").strip() or None,
                counterparty_email=None,
            )
        )
    return out


def load_gateway_settlements(path: str = GATEWAY_CSV) -> list[Payment]:
    out: list[Payment] = []
    for r in _rows(path):
        out.append(
            Payment(
                payment_ref=r["settlement_id"].strip(),
                source="gateway",
                date=r["txn_date"].strip(),
                amount_received=round(float(r["net_amount"]), 2),
                gross_amount=round(float(r["gross_amount"]), 2),
                fee=round(float(r["fee"]), 2),
                reference=r["payer_reference"].strip(),
                counterparty_name=None,
                counterparty_email=(r.get("payer_email") or "").strip() or None,
            )
        )
    return out


def load_payments(
    bank_path: str = BANK_CSV, gateway_path: str = GATEWAY_CSV
) -> list[Payment]:
    """All payments from both sources, in one normalised list."""
    return load_bank_transactions(bank_path) + load_gateway_settlements(gateway_path)


def index_by_invoice_id(invoices: Iterable[Invoice]) -> dict[str, Invoice]:
    return {inv.invoice_id: inv for inv in invoices}
