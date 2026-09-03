"""Offline tests for the settlement Q&A layer — no API calls.

    python -m unittest qa.test_qa

Covers the deterministic query layer (`DecisionStore` / `QueryBridge`) against a
hand-built log fixture and the real committed snapshot, plus the `SettlementQA`
loop driven by a scripted fake Gemini client.
"""

from __future__ import annotations

import json
import os
import types as pytypes
import unittest

from .assistant import SettlementQA
from .query_tools import QueryBridge
from .store import (
    DecisionStore,
    SNAPSHOT_LOG_PATH,
    cited_payment_ids,
    compact,
    confidence_band,
    resolve_match_type,
)


def _rec(pid, mt, amount, conf, **kw):
    res = None if mt is None else {
        "match_type": mt,
        "invoice_ids": kw.get("invoice_ids", []),
        "confidence": conf,
        "reasoning": kw.get("reasoning", f"{pid} resolved as {mt}"),
        "strategies_tried": kw.get("strategies_tried", ["parse_reference"]),
        "exception_reason": kw.get("exception_reason"),
    }
    return {
        "payment_id": pid,
        "payment_ref": kw.get("payment_ref", pid.replace("PAY", "TXN")),
        "source": kw.get("source", "gateway"),
        "date": kw.get("date", "2026-03-01"),
        "amount_received": amount,
        "gross_amount": kw.get("gross_amount", amount),
        "fee": kw.get("fee", 0.0),
        "reference": kw.get("reference", "INV0001"),
        "counterparty": kw.get("counterparty", "cust001@example.com"),
        "resolution": res,
        "preflight": kw.get("preflight"),
        "tool_calls": [],
        "iterations": 2,
        "forced_retry": kw.get("forced_retry", False),
        "model": "test-model",
        "error": kw.get("error"),
    }


FIXTURE = [
    _rec("PAY0001", "single_full", 1000.0, 0.97, invoice_ids=["INV0001"], source="gateway"),
    _rec("PAY0002", "combined", 150000.0, 0.95, invoice_ids=["INV0002", "INV0003"], source="bank"),
    _rec("PAY0003", "combined", 8000.0, 0.9, invoice_ids=["INV0004", "INV0005"]),
    _rec("PAY0004", "fee_deducted", 24667.15, 0.92, invoice_ids=["INV0010"], fee=550.51,
         gross_amount=25217.66),
    _rec("PAY0005", "fee_deducted", 9800.0, 0.88, invoice_ids=["INV0011"], fee=200.0),
    _rec("PAY0006", "partial", 4000.0, 0.85, invoice_ids=["INV0012"]),
    _rec("PAY0007", "exception", 35224.52, 0.90, reference="N/A",
         counterparty="Unknown Sender Ltd", exception_reason="no reference, unknown sender"),
    _rec("PAY0008", "single_full", 500.0, 0.55, forced_retry=True,
         preflight={"suggested_match_type": "exception"}),
    _rec("PAY0009", "fee_deducted", 27043.96, 0.80, invoice_ids=["INV0038"],
         preflight={"suggested_match_type": "exception"}),
]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore([dict(r) for r in FIXTURE])

    def test_get_accepts_many_identifier_forms(self):
        for ident in ("PAY0004", "pay4", "#4", "4", "TXN0004"):
            self.assertEqual(self.store.get(ident)["payment_id"], "PAY0004", ident)
        self.assertIsNone(self.store.get("PAY9999"))

    def test_search_by_match_type_and_amount(self):
        hits = self.store.search(match_type="combined", min_amount=100000)
        self.assertEqual([h["payment_id"] for h in hits], ["PAY0002"])

    def test_match_type_synonyms(self):
        self.assertEqual(resolve_match_type("orphan"), "exception")
        self.assertEqual(resolve_match_type("fee deducted"), "fee_deducted")
        hits = self.store.search(match_type="unmatched")
        self.assertEqual([h["payment_id"] for h in hits], ["PAY0007"])

    def test_search_source_and_confidence(self):
        hits = self.store.search(source="bank")
        self.assertEqual({h["payment_id"] for h in hits}, {"PAY0002"})
        low = self.store.search(max_confidence=0.6)
        self.assertEqual([h["payment_id"] for h in low], ["PAY0008"])

    def test_search_forced_retry_and_preflight_disagreement(self):
        self.assertEqual([h["payment_id"] for h in self.store.search(forced_retry=True)], ["PAY0008"])
        dis = self.store.search(preflight_disagreed=True)
        # PAY0008 preflight said exception, resolved single_full; PAY0009 said exception, resolved fee_deducted
        self.assertEqual({h["payment_id"] for h in dis}, {"PAY0008", "PAY0009"})

    def test_by_invoice(self):
        self.assertEqual([h["payment_id"] for h in self.store.by_invoice("INV0003")], ["PAY0002"])

    def test_summary_numbers(self):
        s = self.store.summary()
        self.assertEqual(s["payments"], 9)
        self.assertEqual(s["by_match_type"]["fee_deducted"], 3)
        self.assertEqual(s["exception_count"], 1)
        self.assertEqual(s["matched_count"], 8)
        self.assertEqual(s["forced_retry_count"], 1)
        self.assertEqual(s["preflight_disagreement_count"], 2)
        # matched value excludes the one exception
        expected = round(sum(r["amount_received"] for r in FIXTURE) - 35224.52, 2)
        self.assertEqual(s["total_matched_value"], expected)

    def test_group_by(self):
        self.assertEqual(self.store.group_by("match_type")["fee_deducted"], 3)
        self.assertEqual(self.store.group_by("source")["gateway"], 8)
        self.assertEqual(self.store.group_by("confidence_band")["low"], 1)
        self.assertEqual(self.store.group_by("preflight_agreement")["overridden"], 2)
        with self.assertRaises(ValueError):
            self.store.group_by("nonsense")

    def test_compact_and_confidence_band(self):
        self.assertEqual(confidence_band(0.95), "high")
        self.assertEqual(confidence_band(0.7), "medium")
        self.assertEqual(confidence_band(0.4), "low")
        c = compact(FIXTURE[0])
        self.assertEqual(set(c), {"payment_id", "date", "source", "amount_received",
                                  "reference", "counterparty", "match_type",
                                  "invoice_ids", "confidence", "forced_retry"})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = QueryBridge(DecisionStore([dict(r) for r in FIXTURE]))

    def test_get_payment_tool(self):
        out, err = self.bridge.dispatch("get_payment", {"identifier": "7"})
        self.assertFalse(err)
        self.assertTrue(out["found"])
        self.assertEqual(out["payment"]["match_type"], "exception")
        self.assertIn("reasoning", out["payment"])

    def test_get_payment_missing(self):
        out, err = self.bridge.dispatch("get_payment", {"identifier": "PAY9999"})
        self.assertFalse(err)
        self.assertFalse(out["found"])

    def test_search_tool_sorts_and_limits(self):
        out, err = self.bridge.dispatch("search_payments", {"match_type": "fee_deducted",
                                                            "sort_by": "amount_received",
                                                            "descending": True, "limit": 2})
        self.assertFalse(err)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["returned"], 2)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["payments"][0]["payment_id"], "PAY0009")  # 27043.96 is largest

    def test_group_by_tool_bad_field_is_error(self):
        out, err = self.bridge.dispatch("group_by", {"field": "banana"})
        self.assertTrue(err)
        self.assertIn("banana", out["error"])

    def test_unknown_tool(self):
        out, err = self.bridge.dispatch("frobnicate", {})
        self.assertTrue(err)


class SnapshotTests(unittest.TestCase):
    """The committed snapshot must stay loadable and sane."""

    @unittest.skipUnless(os.path.exists(SNAPSHOT_LOG_PATH), "no committed snapshot")
    def test_snapshot_loads(self):
        store = DecisionStore.load(SNAPSHOT_LOG_PATH)
        self.assertGreaterEqual(len(store), 100)
        s = store.summary()
        self.assertEqual(s["errors"], 0)
        self.assertGreater(s["matched_count"], 0)
        self.assertIsNotNone(store.get("PAY0001"))


# --- fake Gemini SDK objects (same shape as agent/test_agent.py) --------

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
        prompt_token_count=80, candidates_token_count=25, thoughts_token_count=0
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


class AssistantLoopTests(unittest.TestCase):
    def setUp(self):
        self.store = DecisionStore([dict(r) for r in FIXTURE])

    def _qa(self, script):
        return SettlementQA(self.store, client=FakeClient(script), model="fake",
                            min_call_interval=0)

    def test_tool_call_then_grounded_answer(self):
        script = [
            _response([_part_call("get_payment", {"identifier": "PAY0007"})]),
            _response([_part_text("PAY0007 was logged as an exception at confidence 0.90 "
                                  "because it had no reference and an unknown sender.")]),
        ]
        qa = self._qa(script)
        ans = qa.ask("why didn't PAY0007 match?")
        self.assertIsNone(ans.error)
        self.assertEqual(ans.payment_ids, ["PAY0007"])
        self.assertEqual([tc["tool"] for tc in ans.tool_calls], ["get_payment"])
        self.assertTrue(ans.tool_calls[0]["output"]["found"])
        self.assertEqual(ans.iterations, 2)
        self.assertEqual(ans.usage["input_tokens"], 160)

    def test_multiple_queries_before_answering(self):
        script = [
            _response([_part_call("summarize_batch", {})]),
            _response([_part_call("group_by", {"field": "match_type"})]),
            _response([_part_text("9 payments: 8 matched, 1 exception (PAY0007).")]),
        ]
        ans = self._qa(script).ask("give me the batch breakdown")
        self.assertIsNone(ans.error)
        self.assertEqual([tc["tool"] for tc in ans.tool_calls], ["summarize_batch", "group_by"])

    def test_nudge_when_model_stalls(self):
        script = [
            _response([]),  # nothing -> nudge
            _response([_part_text("There are 3 fee_deducted payments.")]),
        ]
        ans = self._qa(script).ask("how many fee deducted?")
        self.assertIsNone(ans.error)
        self.assertEqual(ans.answer, "There are 3 fee_deducted payments.")

    def test_api_error_is_captured_not_raised(self):
        from google.genai import errors as genai_errors

        class Boom:
            class models:
                @staticmethod
                def generate_content(**kw):
                    # 400 is non-retryable, so this returns immediately
                    raise genai_errors.APIError(400, {"error": {"message": "bad request"}})

        qa = SettlementQA(self.store, client=Boom(), model="fake", min_call_interval=0)
        ans = qa.ask("anything?")
        self.assertIsNotNone(ans.error)
        self.assertEqual(ans.answer, "")

    def test_transient_server_error_is_retried(self):
        from google.genai import errors as genai_errors

        ok = _response([_part_text("3 fee_deducted payments.")])

        class Flaky:
            def __init__(self):
                self.n = 0

            class _M:
                pass

            @property
            def models(self):
                m = self._M()
                m.generate_content = self._gen
                return m

            def _gen(self, **kw):
                self.n += 1
                if self.n == 1:
                    raise genai_errors.APIError(503, {"error": {"message": "overloaded"}})
                return ok

        import qa.assistant as mod
        orig = mod.time.sleep
        mod.time.sleep = lambda *_: None  # don't actually back off in the test
        try:
            qa = SettlementQA(self.store, client=Flaky(), model="fake", min_call_interval=0)
            ans = qa.ask("how many fee deducted?")
        finally:
            mod.time.sleep = orig
        self.assertIsNone(ans.error)
        self.assertEqual(ans.answer, "3 fee_deducted payments.")

    def test_remember_carries_context(self):
        script = [
            _response([_part_call("get_payment", {"identifier": "PAY0004"})]),
            _response([_part_text("PAY0004 had a fee of Rs 550.51 deducted.")]),
            _response([_part_text("Yes, that is within the 1.5-3% band (2.18%).")]),
        ]
        qa = self._qa(script)
        qa.ask("what fee was taken on PAY0004?", remember=True)
        ans2 = qa.ask("is that a normal fee?", remember=True)
        self.assertIsNone(ans2.error)
        # second turn's contents should include the first Q and A
        last_call = qa.bridge.store  # sanity: bridge still wired
        self.assertIsNotNone(last_call)
        self.assertGreaterEqual(len(qa.history), 4)


class CitationTests(unittest.TestCase):
    def test_cited_payment_ids_dedupes_and_uppercases(self):
        self.assertEqual(
            cited_payment_ids("PAY0121 and pay0121 plus PAY0044"),
            ["PAY0121", "PAY0044"],
        )
        self.assertEqual(cited_payment_ids("no ids here"), [])


if __name__ == "__main__":
    unittest.main()
