"""Deterministic pre-flight analysis (Phase 4 optimisation).

Before the agent ever sees a payment we run the cheap, deterministic checks in
plain Python — parse the reference, pull the candidate invoices, test them for an
exact match / an exact sum / an invoice-minus-fee match, and fall back to a
customer/amount/combination search when the reference is no help. The result is
handed to the agent in its first prompt, so a straightforward payment can be
resolved in a single model call instead of four or five.

There is no LLM here and no ground-truth access — this uses exactly the same
tools the agent has (via `ToolBridge`), just driven deterministically. The agent
is still the one that decides and calls `submit_resolution`; the pre-flight only
does the legwork and states a *suggestion*.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from tools.loaders import Payment
from tools.reconciliation_tools import MONEY_TOL, customer_id_from_email

from .tools_bridge import ToolBridge

# A received amount below this fraction of an invoice is "substantially short" —
# consistent with the generator's 40%-85% partial band and the system prompt.
PARTIAL_CEILING = 0.92


def _close(a: float, b: float, tol: float = MONEY_TOL) -> bool:
    return abs(round(a * 100) - round(b * 100)) <= round(tol * 100)


@dataclass
class Preflight:
    """What the deterministic pass found. Serialised straight into the log."""

    reference_parse: dict = field(default_factory=dict)
    candidate_invoices: list[dict] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)          # human-readable findings
    tool_results: dict[str, Any] = field(default_factory=dict)  # raw, for the audit trail
    strategies_run: list[str] = field(default_factory=list)
    suggested_match_type: Optional[str] = None
    suggested_invoice_ids: list[str] = field(default_factory=list)
    suggested_confidence: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_confident(self) -> bool:
        """True when the deterministic pass alone is enough to resolve."""
        return self.suggested_match_type is not None and self.suggested_confidence >= 0.85


def build_preflight(payment: Payment, bridge: ToolBridge) -> Preflight:
    pf = Preflight()

    # --- 1. the reference -------------------------------------------------
    parsed, _ = bridge.dispatch("parse_reference", {"reference": payment.reference})
    pf.reference_parse = parsed
    pf.tool_results["parse_reference"] = parsed
    pf.strategies_run.append("parse_reference")
    ref_ids: list[str] = list(parsed.get("invoice_ids") or [])
    kind = parsed.get("kind", "none")

    ref_invoices: list[dict] = []
    if ref_ids:
        got, _ = bridge.dispatch("get_invoices", {"invoice_ids": ref_ids})
        pf.tool_results["get_invoices"] = got
        ref_invoices = got.get("found", [])
        pf.candidate_invoices = ref_invoices
        if got.get("missing"):
            pf.checks.append(f"reference ids not in ledger: {got['missing']}")

    recv = payment.amount_received

    # --- 2. reference-driven verdicts ----------------------------------
    if kind == "single" and len(ref_invoices) == 1:
        inv = ref_invoices[0]
        amt = inv["invoice_amount"]
        if _close(recv, amt):
            pf.checks.append(f"received {recv} == invoice {inv['invoice_id']} ({amt})")
            _suggest(pf, "single_full", [inv["invoice_id"]], 0.97,
                     f"reference resolves to {inv['invoice_id']} and the amount matches to the cent")
        else:
            fee, _ = bridge.dispatch(
                "check_fee_schedule",
                {"amount_received": recv, "invoice_amount": amt},
            )
            pf.tool_results["check_fee_schedule"] = fee
            pf.strategies_run.append("check_fee_schedule")
            if fee.get("explained"):
                pf.checks.append(
                    f"shortfall vs {inv['invoice_id']} is {fee['note']}")
                _suggest(pf, "fee_deducted", [inv["invoice_id"]], 0.92,
                         f"reference resolves to {inv['invoice_id']}; the "
                         f"{fee.get('implied_fee_pct')} shortfall is within the gateway fee band")
            elif amt > 0 and recv < amt * PARTIAL_CEILING:
                pct = round(recv / amt * 100)
                pf.checks.append(
                    f"received {recv} is ~{pct}% of {inv['invoice_id']} ({amt}) — "
                    f"reference-identified but substantially short")
                _suggest(pf, "partial", [inv["invoice_id"]], 0.85,
                         f"reference resolves to {inv['invoice_id']}; received is ~{pct}% of it "
                         f"and the fee schedule does not explain the gap")
            else:
                pf.checks.append(
                    f"received {recv} is close to but not equal to {inv['invoice_id']} "
                    f"({amt}) and not a fee shortfall — inconclusive")

    elif kind in ("range", "list") and len(ref_invoices) >= 2:
        total = round(sum(i["invoice_amount"] for i in ref_invoices), 2)
        cust_ids = {i["customer_id"] for i in ref_invoices}
        if _close(recv, total) and len(cust_ids) == 1:
            ids = [i["invoice_id"] for i in ref_invoices]
            pf.checks.append(f"invoices {ids} sum to {total} == received {recv}, same customer")
            _suggest(pf, "combined", ids, 0.95,
                     f"reference lists {len(ids)} invoices for one customer that sum exactly to the payment")
        else:
            pf.checks.append(
                f"reference lists {len(ref_invoices)} invoices summing to {total} "
                f"(received {recv}); not an exact combined match")

    # --- 3. fallback: customer / amount / combination search ------------
    if not pf.is_confident:
        cust_id = customer_id_from_email(payment.counterparty_email)
        by_cust = None
        if cust_id:
            by_cust, _ = bridge.dispatch("find_invoices_by_customer", {"customer_id": cust_id})
        elif payment.counterparty_name:
            by_cust, _ = bridge.dispatch("find_invoices_by_customer", {"name": payment.counterparty_name})
        if by_cust is not None:
            pf.tool_results["find_invoices_by_customer"] = by_cust
            pf.strategies_run.append("find_invoices_by_customer")
            pf.checks.append(
                f"customer lookup ({cust_id or payment.counterparty_name}) -> "
                f"{by_cust.get('count', 0)} open invoice(s)")

        exact, _ = bridge.dispatch("find_invoices_by_amount", {"amount": recv, "tolerance_pct": 0})
        pf.tool_results["find_invoices_by_amount"] = exact
        pf.strategies_run.append("find_invoices_by_amount")
        exact_hits = exact.get("invoices", [])
        pf.checks.append(f"exact amount search -> {exact.get('count', 0)} invoice(s)")

        combo_res = None
        if cust_id:
            combo_res, _ = bridge.dispatch(
                "check_combination", {"target_amount": recv, "customer_id": cust_id})
            pf.tool_results["check_combination"] = combo_res
            pf.strategies_run.append("check_combination")
            combos = combo_res.get("combinations", [])
            if combos:
                pf.checks.append(f"combination search -> {combos[0]}")

        if pf.suggested_match_type is None:
            if len(exact_hits) == 1 and kind in ("none", "unparseable"):
                inv = exact_hits[0]
                _suggest(pf, "single_full", [inv["invoice_id"]], 0.6,
                         f"no usable reference, but exactly one invoice ({inv['invoice_id']}) "
                         f"matches the amount to the cent — verify the customer before trusting this")
            elif not exact_hits and not (combo_res and combo_res.get("combinations")):
                _suggest(pf, "exception", [], 0.7,
                         "reference is missing/unparseable, no customer match, no invoice at the "
                         "exact amount, and no valid combination — looks like a true orphan")

    if pf.suggested_match_type is None:
        pf.rationale = "deterministic pass inconclusive — the agent should investigate with its tools"

    return pf


def _suggest(pf: Preflight, match_type: str, ids: list[str], conf: float, why: str) -> None:
    pf.suggested_match_type = match_type
    pf.suggested_invoice_ids = ids
    pf.suggested_confidence = conf
    pf.rationale = why
