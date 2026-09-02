"""Offline tests for the agent loop — no API calls.

A scripted fake Gemini client drives `Reconciler.reconcile` through the paths
that matter: a clean happy-path resolution, and the forced-second-strategy rule
(spec point 2). Tool dispatch runs for real against the generated data.

    python -m unittest agent.test_agent
"""

from __future__ import annotations

import json
import types as pytypes
import unittest

from google.genai import errors as genai_errors

from tools.loaders import load_invoices, load_payments

from .reconciler import Reconciler
from .tools_bridge import SUBMIT_TOOL_NAME


# --- fake Gemini SDK objects --------------------------------------------

def _part_text(t):
    return pytypes.SimpleNamespace(text=t, function_call=None)


def _part_call(name, args):
    return pytypes.SimpleNamespace(
        text=None, function_call=pytypes.SimpleNamespace(name=name, args=args)
    )


def _response(parts):
    content = pytypes.SimpleNamespace(role="model", parts=parts)
    cand = pytypes.SimpleNamespace(content=content)
    usage = pytypes.SimpleNamespace(
        prompt_token_count=100, candidates_token_count=20, thoughts_token_count=0
    )
    return pytypes.SimpleNamespace(candidates=[cand], usage_metadata=usage)


class _FakeModels:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.models = _FakeModels(script)


class AgentLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.invoices = load_invoices()
        cls.payments = {p.payment_ref: p for p in load_payments()}

    def _reconciler(self, script):
        return Reconciler(
            self.invoices,
            client=FakeClient(script),
            model="fake-model",
            payment_id_map={"TXN0010": "PAY0016"},
            min_call_interval=0,
        )

    def test_happy_path_single_full(self):
        pay = self.payments["TXN0010"]  # reference INV0019, exact single
        script = [
            _response([_part_call("parse_reference", {"reference": pay.reference})]),
            _response([_part_call("get_invoices", {"invoice_ids": ["INV0019"]})]),
            _response([_part_call(SUBMIT_TOOL_NAME, {
                "match_type": "single_full",
                "invoice_ids": ["INV0019"],
                "confidence": 0.97,
                "reasoning": "reference parsed to INV0019; amount matches exactly",
                "strategies_tried": ["reference parse", "amount check"],
                "exception_reason": "",
            })]),
        ]
        rec = self._reconciler(script).reconcile(pay)
        self.assertIsNone(rec.error)
        self.assertTrue(rec.resolved)
        self.assertEqual(rec.payment_id, "PAY0016")
        self.assertEqual(rec.resolution.match_type, "single_full")
        self.assertEqual(rec.resolution.invoice_ids, ["INV0019"])
        self.assertFalse(rec.forced_retry)
        self.assertEqual(rec.iterations, 3)
        self.assertEqual([tc.tool for tc in rec.tool_calls],
                         ["parse_reference", "get_invoices", SUBMIT_TOOL_NAME])
        self.assertEqual(rec.usage["input_tokens"], 300)

    def test_low_confidence_exception_is_forced_to_retry(self):
        pay = self.payments["TXN0010"]
        submit_exc = _part_call(SUBMIT_TOOL_NAME, {
            "match_type": "exception",
            "invoice_ids": [],
            "confidence": 0.2,
            "reasoning": "gave up early",
            "strategies_tried": ["reference parse"],
            "exception_reason": "could not find anything",
        })
        submit_ok = _part_call(SUBMIT_TOOL_NAME, {
            "match_type": "single_full",
            "invoice_ids": ["INV0019"],
            "confidence": 0.9,
            "reasoning": "after amount search, INV0019 matches",
            "strategies_tried": ["reference parse", "amount search"],
            "exception_reason": "",
        })
        script = [
            _response([_part_call("parse_reference", {"reference": "N/A"})]),
            _response([submit_exc]),                               # rejected here
            _response([_part_call("find_invoices_by_amount",
                                  {"amount": pay.amount_received})]),
            _response([submit_ok]),                                # accepted
        ]
        rec = self._reconciler(script).reconcile(pay)
        self.assertIsNone(rec.error)
        self.assertTrue(rec.forced_retry)
        self.assertEqual(rec.resolution.match_type, "single_full")
        rejected = [tc for tc in rec.tool_calls
                    if tc.tool == SUBMIT_TOOL_NAME and tc.is_error]
        self.assertEqual(len(rejected), 1)

    def test_exception_accepted_after_two_strategies(self):
        pay = self.payments["TXN0010"]
        script = [
            _response([_part_call("parse_reference", {"reference": "GARBAGE"})]),
            _response([_part_call("find_invoices_by_amount", {"amount": 999999.0})]),
            _response([_part_call(SUBMIT_TOOL_NAME, {
                "match_type": "exception",
                "invoice_ids": [],
                "confidence": 0.55,
                "reasoning": "reference unparseable and no invoice near the amount",
                "strategies_tried": ["reference parse", "amount search"],
                "exception_reason": "no matching invoice exists",
            })]),
        ]
        rec = self._reconciler(script).reconcile(pay)
        self.assertIsNone(rec.error)
        self.assertFalse(rec.forced_retry)
        self.assertEqual(rec.resolution.match_type, "exception")
        self.assertEqual(rec.resolution.exception_reason, "no matching invoice exists")

    def test_api_error_is_captured_not_raised(self):
        class Boom:
            class models:
                @staticmethod
                def generate_content(**kw):
                    raise genai_errors.APIError(503, {"error": {"message": "overloaded"}})
        recon = Reconciler(self.invoices, client=Boom(), model="fake", payment_id_map={})
        rec = recon.reconcile(self.payments["TXN0010"])
        self.assertIsNotNone(rec.error)
        self.assertFalse(rec.resolved)

    def test_decision_record_round_trips_through_jsonl(self):
        from .decision_log import _record_from_dict
        pay = self.payments["TXN0010"]
        script = [
            _response([_part_call(SUBMIT_TOOL_NAME, {
                "match_type": "single_full",
                "invoice_ids": ["INV0019"],
                "confidence": 0.95,
                "reasoning": "x",
                "strategies_tried": ["ref"],
                "exception_reason": "",
            })]),
        ]
        rec = self._reconciler(script).reconcile(pay)
        row = json.loads(json.dumps(rec.to_dict(), default=str))
        back = _record_from_dict(row)
        self.assertEqual(back.resolution.invoice_ids, ["INV0019"])
        self.assertEqual(back.payment_id, "PAY0016")


if __name__ == "__main__":
    unittest.main()
