"""Offline tests for the evaluator — synthetic log + ground truth, no API.

    python -m unittest eval.test_evaluate
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from eval.evaluate import evaluate


def _rec(pid, match_type=None, ids=None, *, error=None, confidence=0.9,
         latency=2.0, iterations=2, tool_calls=1, preflight=None):
    res = None
    if match_type is not None:
        res = {"match_type": match_type, "invoice_ids": ids or [],
               "confidence": confidence, "reasoning": "because",
               "strategies_tried": [], "exception_reason": None}
    return {
        "payment_id": pid, "payment_ref": pid, "source": "bank", "date": "2026-02-01",
        "amount_received": 100.0, "gross_amount": 100.0, "fee": 0.0, "reference": "x",
        "counterparty": None, "resolution": res, "preflight": preflight,
        "tool_calls": [{"tool": "parse_reference", "input": {}, "output": {}, "is_error": False}]
                       * tool_calls,
        "iterations": iterations, "forced_retry": False, "model": "fake",
        "usage": {"input_tokens": 500, "output_tokens": 50}, "latency_s": latency,
        "timestamp": "2026-02-01T00:00:00+00:00", "error": error,
    }


GT = {
    "PAY001": {"match_type": "single_full", "invoice_ids": ["INV1"], "notes": ""},
    "PAY002": {"match_type": "combined", "invoice_ids": ["INV2", "INV3"], "notes": ""},
    "PAY003": {"match_type": "partial", "invoice_ids": ["INV4"], "notes": ""},
    "PAY004": {"match_type": "orphan", "invoice_ids": [], "notes": "planted"},
    "PAY005": {"match_type": "single_full", "invoice_ids": ["INV5"], "notes": ""},
    "PAY006": {"match_type": "orphan", "invoice_ids": [], "notes": "planted"},
    "PAY007": {"match_type": "fee_deducted", "invoice_ids": ["INV7"], "notes": ""},
}

LOG = [
    _rec("PAY001", "single_full", ["INV1"]),
    _rec("PAY002", "combined", ["INV3", "INV2"]),                     # order-independent
    _rec("PAY003", "single_full", ["INV4"]),                          # right invoice, wrong type
    _rec("PAY004", "exception", [], confidence=0.8),                  # exception <-> orphan
    _rec("PAY005", "exception", [], confidence=0.7),                  # gave up on a solvable one
    _rec("PAY006", "single_full", ["INV6"]),                          # matched a real orphan
    _rec("PAY007", None, error="APIError: boom"),                     # errored, no decision
]


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.log = os.path.join(self.d, "log.jsonl")
        self.gt = os.path.join(self.d, "gt.json")
        with open(self.log, "w", encoding="utf-8") as f:
            for r in LOG:
                f.write(json.dumps(r) + "\n")
        with open(self.gt, "w", encoding="utf-8") as f:
            json.dump(GT, f)
        self.m = evaluate(self.log, self.gt)["metrics"]

    def test_dataset_split(self):
        self.assertEqual(self.m["dataset"], {"payments": 7, "solvable": 5, "orphans": 2})

    def test_accuracy(self):
        acc = self.m["accuracy"]
        self.assertEqual(acc["correct"], 3)                 # PAY001, 002, 004
        self.assertAlmostEqual(acc["pct_of_all"], 42.9, places=1)
        self.assertAlmostEqual(acc["invoice_set_only"], 57.1, places=1)  # +PAY003

    def test_match_rate(self):
        mr = self.m["match_rate"]
        self.assertEqual(mr["committed_to_a_match"], 4)     # 001,002,003,006
        self.assertAlmostEqual(mr["pct_of_solvable"], 60.0, places=1)  # 001,002,003 of 5

    def test_coverage_counts_the_error(self):
        self.assertEqual(self.m["coverage"]["errors"], 1)
        self.assertEqual(self.m["coverage"]["missing"], 0)

    def test_exception_quality(self):
        eq = self.m["exception_quality"]
        self.assertEqual(eq["agent_raised"], 2)
        self.assertEqual(eq["correct"], 1)                  # PAY004
        self.assertEqual(eq["precision"], 50.0)
        self.assertEqual(eq["orphan_recall"], 50.0)
        self.assertEqual(eq["gave_up_on_solvable"]["payments"], ["PAY005"])
        self.assertEqual(eq["missed_orphans"]["payments"], ["PAY006"])

    def test_wrong_answers_listed_with_reasoning(self):
        wrong = {w["payment_id"] for w in self.m["wrong_answers"]}
        self.assertEqual(wrong, {"PAY003", "PAY005", "PAY006", "PAY007"})

    def test_throughput_present(self):
        tp = self.m["throughput"]
        self.assertEqual(tp["payments_timed"], 7)
        self.assertGreater(tp["payments_per_min"], 0)
        self.assertEqual(tp["tokens"]["input"], 3500)


if __name__ == "__main__":
    unittest.main()
