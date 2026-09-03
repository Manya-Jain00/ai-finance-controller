"""Bridge between the Q&A LLM loop and the read-only `DecisionStore` (Phase 6).

Mirrors `agent/tools_bridge.py`: declares the tool schemas the model sees
(`TOOL_SPECS`) and dispatches a call by name to a `DecisionStore` method,
returning plain JSON. No LLM SDK import, no ground-truth access.
"""

from __future__ import annotations

from typing import Any

from .store import DecisionStore, compact, detailed

# Cap how many rows one search can dump into the model context.
MAX_ROWS = 40

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_payment",
        "description": (
            "Fetch the full decision record for ONE payment: the agent's match "
            "type, matched invoice ids, confidence, full reasoning narrative, the "
            "exception reason (if any), which tools it ran, and what the "
            "deterministic pre-flight had suggested. Accepts 'PAY0047', '47' or "
            "'#47', or a source ref like 'TXN0010'. Use this for any question "
            "about a specific payment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Payment id or reference."}
            },
            "required": ["identifier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_payments",
        "description": (
            "Return decision records matching filters (all optional, AND-combined). "
            "Amounts are in rupees. Results are compact; call get_payment for the "
            "full reasoning of a specific hit. Use sort_by/descending/limit to "
            "shape the list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match_type": {
                    "type": "string",
                    "description": (
                        "single_full | combined | partial | fee_deducted | exception. "
                        "Free-text synonyms like 'orphan', 'unmatched' or 'fee deducted' "
                        "are accepted."
                    ),
                },
                "min_amount": {"type": "number", "description": "amount_received >= this."},
                "max_amount": {"type": "number", "description": "amount_received <= this."},
                "min_confidence": {"type": "number"},
                "max_confidence": {"type": "number"},
                "source": {"type": "string", "description": "'bank' or 'gateway'."},
                "start_date": {"type": "string", "description": "ISO date, inclusive lower bound."},
                "end_date": {"type": "string", "description": "ISO date, inclusive upper bound."},
                "forced_retry": {
                    "type": "boolean",
                    "description": "true = the loop made the agent try a second strategy.",
                },
                "preflight_disagreed": {
                    "type": "boolean",
                    "description": "true = the agent's answer differs from the pre-flight suggestion.",
                },
                "had_error": {"type": "boolean", "description": "true = the loop errored on this payment."},
                "invoice_id": {"type": "string", "description": "Payments matched to this invoice id."},
                "reference_contains": {"type": "string", "description": "Substring of the raw payment reference."},
                "counterparty_contains": {"type": "string", "description": "Substring of the counterparty name/email."},
                "reasoning_contains": {"type": "string", "description": "Substring of the agent's reasoning text."},
                "sort_by": {
                    "type": "string",
                    "description": "amount_received (default) | confidence | date | payment_id.",
                },
                "descending": {"type": "boolean", "description": "Default true."},
                "limit": {"type": "integer", "description": f"Max rows (default {MAX_ROWS})."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "summarize_batch",
        "description": (
            "Batch-wide totals from the log: payment count, counts per match type, "
            "matched vs exception counts, total amount received, total matched "
            "value, mean confidence, forced-retry count, pre-flight-disagreement "
            "count, date range and model(s) used. No arguments."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "group_by",
        "description": (
            "Count payments grouped by one field. field is one of: match_type, "
            "source, model, confidence_band, preflight_agreement, forced_retry, "
            "month. Returns {group: count} sorted by count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"field": {"type": "string"}},
            "required": ["field"],
            "additionalProperties": False,
        },
    },
]

_SORT_KEYS = {
    "amount_received": lambda r: r.get("amount_received") or 0.0,
    "amount": lambda r: r.get("amount_received") or 0.0,
    "confidence": lambda r: (r.get("resolution") or {}).get("confidence") or 0.0,
    "date": lambda r: r.get("date") or "",
    "payment_id": lambda r: r.get("payment_id") or "",
}


class QueryBridge:
    """Holds a `DecisionStore` and dispatches tool calls against it."""

    def __init__(self, store: DecisionStore):
        self.store = store

    # -- individual tools ------------------------------------------

    def _get_payment(self, identifier: str) -> dict:
        rec = self.store.get(identifier)
        if rec is None:
            return {"found": False, "identifier": identifier,
                    "note": "no payment with that id/ref in the decision log"}
        return {"found": True, "payment": detailed(rec)}

    def _search_payments(
        self,
        sort_by: str = "amount_received",
        descending: bool = True,
        limit: int = MAX_ROWS,
        **filters: Any,
    ) -> dict:
        hits = self.store.search(**filters)
        keyfn = _SORT_KEYS.get((sort_by or "amount_received").lower(), _SORT_KEYS["amount_received"])
        hits = sorted(hits, key=keyfn, reverse=bool(descending))
        limit = min(int(limit or MAX_ROWS), MAX_ROWS)
        return {
            "count": len(hits),
            "returned": min(len(hits), limit),
            "truncated": len(hits) > limit,
            "payments": [compact(r) for r in hits[:limit]],
        }

    def _summarize_batch(self) -> dict:
        return self.store.summary()

    def _group_by(self, field: str) -> dict:
        return {"field": field, "counts": self.store.group_by(field)}

    # -- dispatch ------------------------------------------------

    _DISPATCH = {
        "get_payment": "_get_payment",
        "search_payments": "_search_payments",
        "summarize_batch": "_summarize_batch",
        "group_by": "_group_by",
    }

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> tuple[Any, bool]:
        """Run a query tool. Returns (result, is_error)."""
        method = self._DISPATCH.get(name)
        if method is None:
            return {"error": f"unknown tool {name!r}"}, True
        try:
            return getattr(self, method)(**(tool_input or {})), False
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}, True
        except ValueError as e:
            return {"error": str(e)}, True
        except Exception as e:  # keep the loop alive
            return {"error": f"{type(e).__name__}: {e}"}, True
