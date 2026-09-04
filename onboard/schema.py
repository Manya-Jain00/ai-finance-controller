"""The canonical target schema the mapper maps onto.

These are exactly the fields `tools.loaders.Payment` / `Invoice` already use —
deliberately not a new schema, so a mapped source slots into the existing
pipeline with no further translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TargetField:
    name: str
    description: str
    required: bool


# order matters only for display
PAYMENT_FIELDS: tuple[TargetField, ...] = (
    TargetField("payment_ref", "A unique id for this row - a transaction or settlement id.", True),
    TargetField("date", "The date the money moved, any common date format.", True),
    TargetField("amount_received", "The amount that actually landed in the account.", True),
    TargetField("reference", "The free-text remittance note / payer reference - whatever the "
                              "payer or bank wrote to say what this payment is for.", True),
    TargetField("gross_amount", "The amount before any fee was deducted. Leave unmapped if the "
                                 "same as amount_received (no separate fee column).", False),
    TargetField("fee", "A fee/charge deducted by a gateway, if there is one.", False),
    TargetField("counterparty_name", "The name of the person or company that sent the money.", False),
    TargetField("counterparty_email", "The payer's email address, if present.", False),
)

INVOICE_FIELDS: tuple[TargetField, ...] = (
    TargetField("invoice_id", "A unique id for the invoice.", True),
    TargetField("customer_name", "The customer's display name.", True),
    TargetField("invoice_amount", "The invoice's total amount.", True),
    TargetField("customer_id", "A unique id for the customer. Leave unmapped if the file has no "
                                "separate customer id - it will be derived from the name.", False),
    TargetField("invoice_date", "The date the invoice was raised.", False),
    TargetField("due_date", "The date the invoice is due.", False),
)

SCHEMAS: dict[str, tuple[TargetField, ...]] = {"payment": PAYMENT_FIELDS, "invoice": INVOICE_FIELDS}


def fields_for(kind: str) -> tuple[TargetField, ...]:
    try:
        return SCHEMAS[kind]
    except KeyError:
        raise ValueError(f"kind must be 'payment' or 'invoice', got {kind!r}") from None


def required_fields(kind: str) -> list[str]:
    return [f.name for f in fields_for(kind) if f.required]


@dataclass
class FieldGuess:
    target_field: str
    source_column: Optional[str]   # None = nothing in the file maps to it
    confidence: str = "medium"     # "high" | "medium" | "low"


@dataclass
class ColumnMapping:
    """The AI's proposal for one file: target field -> source column (or None)."""

    kind: str
    headers: list[str]
    guesses: list[FieldGuess] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> dict[str, Optional[str]]:
        return {g.target_field: g.source_column for g in self.guesses}

    def missing_required(self) -> list[str]:
        mapped = self.as_dict()
        return [f for f in required_fields(self.kind) if not mapped.get(f)]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "headers": self.headers,
            "guesses": [g.__dict__ for g in self.guesses],
            "notes": self.notes,
        }
