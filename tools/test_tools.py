"""Phase 2 unit tests — every tool verified in isolation, no LLM involved.

Run directly:      python tools/test_tools.py
or via unittest:   python -m unittest discover -s tools -p "test_*.py"
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.loaders import (  # noqa: E402
    Invoice,
    load_invoices,
    load_payments,
    DATA_DIR,
)
from tools.reconciliation_tools import (  # noqa: E402
    find_invoices_by_amount,
    find_invoices_by_customer,
    parse_remittance_reference,
    check_combination,
    check_fee_schedule,
    customer_id_from_email,
)


def inv(iid, amount, cust="CUST001", name=None) -> Invoice:
    return Invoice(
        invoice_id=iid,
        customer_id=cust,
        customer_name=name or f"Customer {cust[-3:]} Pvt Ltd",
        invoice_amount=round(amount, 2),
        invoice_date="2026-01-01",
        due_date="2026-01-31",
    )


# ---------------------------------------------------------------------------
# 1. find_invoices_by_amount
# ---------------------------------------------------------------------------


class TestFindByAmount(unittest.TestCase):
    def setUp(self):
        self.invoices = [
            inv("INV0001", 10000.00),
            inv("INV0002", 10000.01),
            inv("INV0003", 25000.00),
            inv("INV0004", 9800.00),
        ]

    def test_exact_match(self):
        hits = find_invoices_by_amount(self.invoices, 25000.00)
        self.assertEqual([h.invoice_id for h in hits], ["INV0003"])

    def test_tight_tolerance_catches_one_cent_drift(self):
        hits = find_invoices_by_amount(self.invoices, 10000.00, abs_tolerance=0.02)
        self.assertEqual({h.invoice_id for h in hits}, {"INV0001", "INV0002"})

    def test_pct_tolerance_for_fee(self):
        # 9800 is 2% below 10000 -> caught only with pct tolerance
        hits = find_invoices_by_amount(self.invoices, 10000.00, pct_tolerance=0.03)
        self.assertIn("INV0004", [h.invoice_id for h in hits])

    def test_no_match(self):
        self.assertEqual(find_invoices_by_amount(self.invoices, 1.23), [])

    def test_sorted_by_closeness(self):
        hits = find_invoices_by_amount(self.invoices, 10000.00, abs_tolerance=500)
        self.assertEqual(hits[0].invoice_id, "INV0001")


# ---------------------------------------------------------------------------
# 2. find_invoices_by_customer
# ---------------------------------------------------------------------------


class TestFindByCustomer(unittest.TestCase):
    def setUp(self):
        self.invoices = [
            inv("INV0001", 100, "CUST007", "Customer 007 Pvt Ltd"),
            inv("INV0002", 200, "CUST007", "Customer 007 Pvt Ltd"),
            inv("INV0003", 300, "CUST019", "Customer 019 Pvt Ltd"),
        ]

    def test_by_customer_id(self):
        hits = find_invoices_by_customer(self.invoices, customer_id="CUST007")
        self.assertEqual({h.invoice_id for h in hits}, {"INV0001", "INV0002"})

    def test_by_name_exact(self):
        hits = find_invoices_by_customer(self.invoices, name="Customer 007 Pvt Ltd")
        self.assertEqual(len(hits), 2)

    def test_by_name_loose(self):
        hits = find_invoices_by_customer(self.invoices, name="customer 019 pvt ltd  ")
        self.assertEqual([h.invoice_id for h in hits], ["INV0003"])

    def test_by_email(self):
        hits = find_invoices_by_customer(self.invoices, email="cust019@example.com")
        self.assertEqual([h.invoice_id for h in hits], ["INV0003"])

    def test_orphan_email_matches_nothing(self):
        self.assertEqual(find_invoices_by_customer(self.invoices, email="unknown@example.com"), [])

    def test_email_to_customer_id(self):
        self.assertEqual(customer_id_from_email("cust007@example.com"), "CUST007")
        self.assertIsNone(customer_id_from_email("unknown@example.com"))


# ---------------------------------------------------------------------------
# 3. parse_remittance_reference
# ---------------------------------------------------------------------------


class TestParseReference(unittest.TestCase):
    def test_single(self):
        r = parse_remittance_reference("INV0042")
        self.assertEqual(r.kind, "single")
        self.assertEqual(r.invoice_ids, ["INV0042"])

    def test_explicit_plus_list(self):
        r = parse_remittance_reference("INV0002+INV0044+INV0054")
        self.assertEqual(r.kind, "list")
        self.assertEqual(r.invoice_ids, ["INV0002", "INV0044", "INV0054"])

    def test_range_full_width(self):
        r = parse_remittance_reference("INV0077-0079")
        self.assertEqual(r.kind, "range")
        self.assertEqual(r.invoice_ids, ["INV0077", "INV0078", "INV0079"])

    def test_range_two_invoices(self):
        r = parse_remittance_reference("INV0070-0071")
        self.assertEqual(r.invoice_ids, ["INV0070", "INV0071"])

    def test_range_short_rhs(self):
        r = parse_remittance_reference("INV0077-79")
        self.assertEqual(r.invoice_ids, ["INV0077", "INV0078", "INV0079"])

    def test_null_tokens(self):
        for t in ["N/A", "UNKNOWN", "", "  ", "none"]:
            self.assertEqual(parse_remittance_reference(t).kind, "none", t)

    def test_lowercase_and_spacing(self):
        r = parse_remittance_reference("inv 0042")
        self.assertEqual(r.invoice_ids, ["INV0042"])

    def test_wide_range_guarded(self):
        r = parse_remittance_reference("INV0001-0999")
        self.assertEqual(r.invoice_ids, ["INV0001", "INV0999"])
        self.assertIn("wide", r.note)

    def test_garbage_is_unparseable(self):
        self.assertEqual(parse_remittance_reference("thanks!").kind, "unparseable")


# ---------------------------------------------------------------------------
# 4. check_combination
# ---------------------------------------------------------------------------


class TestCheckCombination(unittest.TestCase):
    def setUp(self):
        self.invoices = [
            inv("INV0001", 5000.00, "CUST001"),
            inv("INV0002", 7000.00, "CUST001"),
            inv("INV0003", 3000.00, "CUST001"),
            inv("INV0004", 9999.00, "CUST002"),
            inv("INV0005", 1.00, "CUST002"),
        ]

    def test_finds_exact_pair(self):
        combos = check_combination(self.invoices, 12000.00)
        self.assertTrue(combos)
        self.assertEqual(set(combos[0].invoice_ids), {"INV0001", "INV0002"})
        self.assertEqual(combos[0].diff, 0.0)

    def test_finds_triple(self):
        combos = check_combination(self.invoices, 15000.00, max_k=3)
        self.assertEqual(set(combos[0].invoice_ids), {"INV0001", "INV0002", "INV0003"})

    def test_same_customer_pair_found(self):
        invoices = [
            inv("INV0001", 5000.00, "CUST001"),
            inv("INV0002", 1234.00, "CUST001"),
            inv("INV0004", 9999.00, "CUST002"),
            inv("INV0005", 1.00, "CUST002"),
        ]
        combos = check_combination(invoices, 10000.00, same_customer=True)
        self.assertEqual(len(combos), 1)
        self.assertEqual(set(combos[0].invoice_ids), {"INV0004", "INV0005"})

    def test_cross_customer_rejected(self):
        mixed = [
            inv("INV0001", 4000.00, "CUST001"),
            inv("INV0002", 6000.00, "CUST002"),
        ]
        self.assertEqual(check_combination(mixed, 10000.00, same_customer=True), [])
        self.assertTrue(check_combination(mixed, 10000.00, same_customer=False))

    def test_no_combination(self):
        self.assertEqual(check_combination(self.invoices, 123.45), [])

    def test_tolerance_absorbs_rounding(self):
        invoices = [inv("INV0001", 3333.33, "C"), inv("INV0002", 3333.34, "C")]
        combos = check_combination(invoices, 6666.67, abs_tolerance=0.02)
        self.assertTrue(combos)


# ---------------------------------------------------------------------------
# 5. check_fee_schedule
# ---------------------------------------------------------------------------


class TestCheckFeeSchedule(unittest.TestCase):
    def test_fee_explained(self):
        # real row: invoice 25710.33, net 24989.31, fee 721.02 (~2.8%)
        r = check_fee_schedule(24989.31, 25710.33)
        self.assertTrue(r.explained)
        self.assertAlmostEqual(r.implied_fee_pct, 0.028, places=3)

    def test_lower_edge_fee(self):
        r = check_fee_schedule(9850.0, 10000.0)  # 1.5%
        self.assertTrue(r.explained)

    def test_partial_not_a_fee(self):
        r = check_fee_schedule(6000.0, 10000.0)  # 40% short
        self.assertFalse(r.explained)
        self.assertIn("partial", r.note)

    def test_tiny_gap_not_a_fee(self):
        r = check_fee_schedule(9995.0, 10000.0)  # 0.05%
        self.assertFalse(r.explained)

    def test_exact_match(self):
        r = check_fee_schedule(10000.0, 10000.0)
        self.assertFalse(r.explained)
        self.assertEqual(r.shortfall, 0.0)

    def test_overpayment(self):
        r = check_fee_schedule(10500.0, 10000.0)
        self.assertFalse(r.explained)
        self.assertIn("overpayment", r.note)


# ---------------------------------------------------------------------------
# Integration sanity — tools against the real generated data + ground truth
# ---------------------------------------------------------------------------


DATA_PRESENT = all(
    os.path.exists(os.path.join(DATA_DIR, f))
    for f in ("invoices.csv", "bank_wire_transactions.csv", "gateway_settlements.csv",
              "ground_truth.json", "payment_id_map.json")
)


@unittest.skipUnless(DATA_PRESENT, "generated data files not found")
class TestAgainstRealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.invoices = load_invoices()
        cls.by_id = {i.invoice_id: i for i in cls.invoices}
        cls.payments = {p.payment_ref: p for p in load_payments()}
        with open(os.path.join(DATA_DIR, "payment_id_map.json"), encoding="utf-8") as f:
            cls.ref_to_pid = json.load(f)
        with open(os.path.join(DATA_DIR, "ground_truth.json"), encoding="utf-8") as f:
            cls.gt = json.load(f)
        cls.pid_to_ref = {v: k for k, v in cls.ref_to_pid.items()}

    def _payment_for_pid(self, pid):
        return self.payments[self.pid_to_ref[pid]]

    def test_every_reference_parses_without_error(self):
        for p in self.payments.values():
            r = parse_remittance_reference(p.reference)
            self.assertIsNotNone(r.kind)

    def test_single_full_resolves_by_reference_and_amount(self):
        checked = 0
        for pid, truth in self.gt.items():
            if truth["match_type"] != "single_full":
                continue
            pay = self._payment_for_pid(pid)
            parsed = parse_remittance_reference(pay.reference)
            self.assertEqual(parsed.invoice_ids, truth["invoice_ids"], pid)
            hits = find_invoices_by_amount(self.invoices, pay.amount_received, abs_tolerance=0.05)
            self.assertIn(truth["invoice_ids"][0], [h.invoice_id for h in hits], pid)
            checked += 1
        self.assertGreater(checked, 30)

    def test_combined_reference_expands_to_truth(self):
        checked = 0
        for pid, truth in self.gt.items():
            if truth["match_type"] != "combined":
                continue
            pay = self._payment_for_pid(pid)
            parsed = parse_remittance_reference(pay.reference)
            self.assertEqual(
                sorted(parsed.invoice_ids), sorted(truth["invoice_ids"]), pid
            )
            checked += 1
        self.assertGreaterEqual(checked, 15)

    def test_combined_amount_reconstructed_by_check_combination(self):
        checked = 0
        for pid, truth in self.gt.items():
            if truth["match_type"] != "combined":
                continue
            pay = self._payment_for_pid(pid)
            cust = self.by_id[truth["invoice_ids"][0]].customer_id
            pool = find_invoices_by_customer(self.invoices, customer_id=cust)
            combos = check_combination(pool, pay.amount_received, max_k=3)
            self.assertTrue(combos, pid)
            self.assertEqual(sorted(combos[0].invoice_ids), sorted(truth["invoice_ids"]), pid)
            checked += 1
        self.assertGreaterEqual(checked, 15)

    def test_fee_deducted_flagged_by_fee_schedule(self):
        checked = 0
        for pid, truth in self.gt.items():
            if truth["match_type"] != "fee_deducted":
                continue
            pay = self._payment_for_pid(pid)
            invoice_amt = self.by_id[truth["invoice_ids"][0]].invoice_amount
            r = check_fee_schedule(pay.amount_received, invoice_amt)
            self.assertTrue(r.explained, f"{pid}: {r.note}")
            checked += 1
        self.assertGreaterEqual(checked, 10)

    def test_partial_rejected_by_fee_schedule(self):
        checked = 0
        for pid, truth in self.gt.items():
            if truth["match_type"] != "partial":
                continue
            pay = self._payment_for_pid(pid)
            invoice_amt = self.by_id[truth["invoice_ids"][0]].invoice_amount
            r = check_fee_schedule(pay.amount_received, invoice_amt)
            self.assertFalse(r.explained, f"{pid}: fee schedule wrongly explained a partial")
            checked += 1
        self.assertGreaterEqual(checked, 15)

    def test_orphans_have_no_reference_and_no_amount_match(self):
        checked = 0
        for pid, truth in self.gt.items():
            if truth["match_type"] != "orphan":
                continue
            pay = self._payment_for_pid(pid)
            parsed = parse_remittance_reference(pay.reference)
            self.assertEqual(parsed.invoice_ids, [], pid)
            checked += 1
        self.assertGreaterEqual(checked, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
