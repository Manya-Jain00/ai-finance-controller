"""Offline tests for the deterministic pre-flight — no API calls.

Runs `build_preflight` against the real generated data and checks that it
reaches the right suggestion for one payment of each mess type. Ground truth is
used only to pick the payments and assert the expected answer.

    python -m unittest agent.test_preflight
"""

from __future__ import annotations

import json
import os
import unittest

from tools.loaders import DATA_DIR, load_invoices, load_payments

from .preflight import build_preflight
from .tools_bridge import ToolBridge


class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.invoices = load_invoices()
        cls.bridge = ToolBridge(cls.invoices)
        payments = {p.payment_ref: p for p in load_payments()}
        with open(os.path.join(DATA_DIR, "payment_id_map.json"), encoding="utf-8") as f:
            ref_to_pid = json.load(f)
        pid_to_ref = {v: k for k, v in ref_to_pid.items()}
        with open(os.path.join(DATA_DIR, "ground_truth.json"), encoding="utf-8") as f:
            cls.gt = json.load(f)
        cls.by_pid = {pid: payments[pid_to_ref[pid]] for pid in ref_to_pid.values()}

    def _pf(self, pid: str):
        return build_preflight(self.by_pid[pid], self.bridge)

    def _first(self, match_type: str) -> str:
        return next(p for p, v in sorted(self.gt.items()) if v["match_type"] == match_type)

    def test_single_full_is_resolved_confidently(self):
        pid = self._first("single_full")
        pf = self._pf(pid)
        self.assertEqual(pf.suggested_match_type, "single_full")
        self.assertEqual(pf.suggested_invoice_ids, self.gt[pid]["invoice_ids"])
        self.assertTrue(pf.is_confident)

    def test_combined_sum_is_recognised(self):
        pid = self._first("combined")
        pf = self._pf(pid)
        self.assertEqual(pf.suggested_match_type, "combined")
        self.assertEqual(sorted(pf.suggested_invoice_ids), sorted(self.gt[pid]["invoice_ids"]))

    def test_fee_deducted_distinguished_from_partial(self):
        pid = self._first("fee_deducted")
        pf = self._pf(pid)
        self.assertEqual(pf.suggested_match_type, "fee_deducted")
        self.assertEqual(pf.suggested_invoice_ids, self.gt[pid]["invoice_ids"])

    def test_partial_is_not_called_a_full_match(self):
        pid = self._first("partial")
        pf = self._pf(pid)
        self.assertEqual(pf.suggested_match_type, "partial")
        self.assertEqual(pf.suggested_invoice_ids, self.gt[pid]["invoice_ids"])
        self.assertLess(pf.suggested_confidence, 0.9)

    def test_orphan_gets_an_exception_suggestion_after_real_strategies(self):
        pid = self._first("orphan")
        pf = self._pf(pid)
        self.assertEqual(pf.suggested_match_type, "exception")
        self.assertEqual(pf.suggested_invoice_ids, [])
        # the fallback searches must have actually run, so the agent loop won't
        # pointlessly force a "second strategy"
        self.assertGreaterEqual(
            len({s for s in pf.strategies_run if s != "parse_reference"}), 2
        )

    def test_every_payment_produces_serialisable_preflight(self):
        for pid, pay in self.by_pid.items():
            pf = build_preflight(pay, self.bridge)
            json.dumps(pf.to_dict(), default=str)  # must not raise
            self.assertIn("parse_reference", pf.strategies_run)


if __name__ == "__main__":
    unittest.main()
