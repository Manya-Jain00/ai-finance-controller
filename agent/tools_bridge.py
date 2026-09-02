"""Bridge between the LLM tool-use loop and the Phase 2 pure functions.

The Phase 2 tools operate on `Invoice` dataclasses and return dataclasses. The
agent should never handle those objects directly — it works with plain JSON. This
module:

  * declares the Anthropic tool schemas the model sees (`TOOL_SPECS`), and
  * dispatches a tool call by name to the underlying pure function, returning a
    JSON-serialisable dict (`ToolBridge.dispatch`).

No LLM SDK import here, and no ground-truth access — just a typed adapter.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tools.loaders import Invoice, index_by_invoice_id
from tools.reconciliation_tools import (
    check_combination,
    check_fee_schedule,
    find_invoices_by_amount,
    find_invoices_by_customer,
    parse_remittance_reference,
)

# Cap how many invoice rows a single lookup can dump into the context.
MAX_ROWS = 25

SUBMIT_TOOL_NAME = "submit_resolution"


def _inv_dict(inv: Invoice) -> dict:
    return {
        "invoice_id": inv.invoice_id,
        "customer_id": inv.customer_id,
        "customer_name": inv.customer_name,
        "invoice_amount": inv.invoice_amount,
        "invoice_date": inv.invoice_date,
        "due_date": inv.due_date,
    }


# ---------------------------------------------------------------------------
# Tool schemas shown to the model
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "parse_reference",
        "description": (
            "Decode a payment's remittance/payer reference string into concrete "
            "invoice ids. Handles single ids (INV0042), delimited lists "
            "(INV0002+INV0044), inclusive ranges (INV0077-0079 / INV0077-79) and "
            "null tokens (N/A, UNKNOWN). Start here for almost every payment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "The raw reference string."}
            },
            "required": ["reference"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_invoices",
        "description": (
            "Fetch full invoice records (amount, customer, dates) for a list of "
            "invoice ids. Use this to check the amounts behind a parsed reference "
            "before deciding — e.g. to confirm a single id matches the payment, or "
            "to sum a range/list yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Invoice ids like ['INV0041', 'INV0042'].",
                }
            },
            "required": ["invoice_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_invoices_by_amount",
        "description": (
            "Return open invoices whose amount is close to a given value, nearest "
            "first. Use tolerance_pct=0 for an exact match; widen to ~0.03 when a "
            "gateway fee may have been deducted, or higher when hunting a partial."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "tolerance_pct": {
                    "type": "number",
                    "description": "Fractional tolerance, e.g. 0.03 for 3%. Default 0.",
                },
            },
            "required": ["amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_invoices_by_customer",
        "description": (
            "Return every open invoice for one customer. Provide exactly one of "
            "customer_id (CUST019), name (loose match on 'Customer 019 Pvt Ltd'), "
            "or email (cust019@example.com). Useful to build a candidate pool for "
            "check_combination, or when the reference is unparseable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "name": {"type": "string"},
                "email": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "check_combination",
        "description": (
            "Find which subset of invoices sums to a target amount (combined "
            "payments cover 2–3 invoices from the same customer). Provide "
            "target_amount plus either customer_id (searches that customer's open "
            "invoices) or an explicit invoice_ids pool. Returns best-fit subsets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_amount": {"type": "number"},
                "customer_id": {"type": "string"},
                "invoice_ids": {"type": "array", "items": {"type": "string"}},
                "max_k": {
                    "type": "integer",
                    "description": "Largest subset size to try. Default 3.",
                },
            },
            "required": ["target_amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_fee_schedule",
        "description": (
            "Decide whether the gap between a received amount and an invoice amount "
            "is fully explained by a standard gateway processing fee (1.5%–3% of "
            "gross). Returns explained=true/false with the implied fee percentage. "
            "A larger gap means a genuine partial payment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount_received": {"type": "number"},
                "invoice_amount": {"type": "number"},
            },
            "required": ["amount_received", "invoice_amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": SUBMIT_TOOL_NAME,
        "description": (
            "Record the final resolution for this payment and end the task. Call "
            "this exactly once, when you are done. If you are about to submit an "
            "'exception' or a confidence below 0.6, you must have tried at least "
            "two materially different strategies first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match_type": {
                    "type": "string",
                    "enum": [
                        "single_full",
                        "combined",
                        "partial",
                        "fee_deducted",
                        "exception",
                    ],
                },
                "invoice_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Matched invoice ids. Empty for an exception.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0. Your calibrated certainty in this resolution.",
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Why this answer - cite the amounts and tool results that "
                        "led here. This is the audit trail; be concrete."
                    ),
                },
                "strategies_tried": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short labels for each distinct approach you tried.",
                },
                "exception_reason": {
                    "type": "string",
                    "description": (
                        "If match_type is 'exception', why it can't be resolved. "
                        "Use an empty string otherwise."
                    ),
                },
            },
            "required": [
                "match_type",
                "invoice_ids",
                "confidence",
                "reasoning",
                "strategies_tried",
            ],
        },
    },
]


class ToolBridge:
    """Holds the invoice ledger and dispatches tool calls against it."""

    def __init__(self, invoices: list[Invoice]):
        self.invoices = invoices
        self._by_id = index_by_invoice_id(invoices)

    # -- individual tools --------------------------------------------------

    def _parse_reference(self, reference: str) -> dict:
        return parse_remittance_reference(reference).to_dict()

    def _get_invoices(self, invoice_ids: list[str]) -> dict:
        found, missing = [], []
        for iid in invoice_ids:
            key = iid.strip().upper()
            inv = self._by_id.get(key)
            if inv:
                found.append(_inv_dict(inv))
            else:
                missing.append(iid)
        return {"found": found, "missing": missing}

    def _find_invoices_by_amount(self, amount: float, tolerance_pct: float = 0.0) -> dict:
        hits = find_invoices_by_amount(self.invoices, amount, pct_tolerance=tolerance_pct)
        return {
            "count": len(hits),
            "invoices": [_inv_dict(h) for h in hits[:MAX_ROWS]],
            "truncated": len(hits) > MAX_ROWS,
        }

    def _find_invoices_by_customer(
        self,
        customer_id: str | None = None,
        name: str | None = None,
        email: str | None = None,
    ) -> dict:
        hits = find_invoices_by_customer(
            self.invoices, customer_id=customer_id, name=name, email=email
        )
        return {
            "count": len(hits),
            "invoices": [_inv_dict(h) for h in hits[:MAX_ROWS]],
            "truncated": len(hits) > MAX_ROWS,
        }

    def _check_combination(
        self,
        target_amount: float,
        customer_id: str | None = None,
        invoice_ids: list[str] | None = None,
        max_k: int = 3,
    ) -> dict:
        if invoice_ids:
            pool = [self._by_id[i.strip().upper()] for i in invoice_ids if i.strip().upper() in self._by_id]
        elif customer_id:
            pool = find_invoices_by_customer(self.invoices, customer_id=customer_id)
        else:
            pool = self.invoices
        combos = check_combination(pool, target_amount, max_k=max_k)
        return {
            "pool_size": len(pool),
            "combinations": [c.to_dict() for c in combos[:10]],
        }

    def _check_fee_schedule(self, amount_received: float, invoice_amount: float) -> dict:
        return check_fee_schedule(amount_received, invoice_amount).to_dict()

    # -- dispatch --------------------------------------------------------

    _DISPATCH = {
        "parse_reference": "_parse_reference",
        "get_invoices": "_get_invoices",
        "find_invoices_by_amount": "_find_invoices_by_amount",
        "find_invoices_by_customer": "_find_invoices_by_customer",
        "check_combination": "_check_combination",
        "check_fee_schedule": "_check_fee_schedule",
    }

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> tuple[Any, bool]:
        """Run a tool. Returns (result, is_error)."""
        method_name = self._DISPATCH.get(name)
        if method_name is None:
            return {"error": f"unknown tool {name!r}"}, True
        try:
            result = getattr(self, method_name)(**tool_input)
            return result, False
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}, True
        except Exception as e:  # keep the loop alive; log the failure
            return {"error": f"{type(e).__name__}: {e}"}, True
