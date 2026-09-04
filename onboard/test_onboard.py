"""Offline tests for the bring-your-own-data mapper — no real API calls.

    python -m unittest onboard.test_onboard

`apply_mapping` is tested directly (pure Python, no LLM). `propose_mapping` is
driven by a scripted fake Gemini client, the same pattern used in
agent/test_agent.py and qa/test_qa.py.
"""

from __future__ import annotations

import types as pytypes
import unittest

from tools.loaders import Invoice, Payment

from .mapper import MappingError, apply_mapping, propose_mapping
from .schema import ColumnMapping, FieldGuess, required_fields


# --- fake Gemini SDK objects (same shape as agent/test_agent.py) --------

def _part_call(name, args):
    return pytypes.SimpleNamespace(
        text=None, function_call=pytypes.SimpleNamespace(name=name, args=args)
    )


def _response(parts):
    content = pytypes.SimpleNamespace(role="model", parts=parts)
    cand = pytypes.SimpleNamespace(content=content)
    return pytypes.SimpleNamespace(candidates=[cand], usage_metadata=None)


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


# --- the "foreign bank" example used throughout the docs ----------------

FOREIGN_HEADERS = ["txn_ref", "posting_date", "debit_amt", "narration", "payer"]
FOREIGN_ROWS = [
    {"txn_ref": "TX9981", "posting_date": "2026-07-01", "debit_amt": "50481.98",
     "narration": "Settlement for INV0005", "payer": "Customer 014 Pvt Ltd"},
    {"txn_ref": "TX9982", "posting_date": "2026-07-02", "debit_amt": "24839.66",
     "narration": "Payment ref INV0007", "payer": "Customer 035 Pvt Ltd"},
]

FOREIGN_GUESSES = {
    "guesses": [
        {"target_field": "payment_ref", "source_column": "txn_ref", "confidence": "high"},
        {"target_field": "date", "source_column": "posting_date", "confidence": "high"},
        {"target_field": "amount_received", "source_column": "debit_amt", "confidence": "high"},
        {"target_field": "reference", "source_column": "narration", "confidence": "high"},
        {"target_field": "gross_amount", "source_column": "", "confidence": "medium"},
        {"target_field": "fee", "source_column": "", "confidence": "medium"},
        {"target_field": "counterparty_name", "source_column": "payer", "confidence": "high"},
        {"target_field": "counterparty_email", "source_column": "", "confidence": "high"},
    ],
    "notes": "",
}


class ProposeMappingTests(unittest.TestCase):
    def test_happy_path_builds_column_mapping(self):
        script = [_response([_part_call("submit_mapping", FOREIGN_GUESSES)])]
        client = FakeClient(script)
        mapping = propose_mapping(FOREIGN_HEADERS, FOREIGN_ROWS, "payment", client=client, model="fake")

        self.assertIsInstance(mapping, ColumnMapping)
        d = mapping.as_dict()
        self.assertEqual(d["payment_ref"], "txn_ref")
        self.assertEqual(d["amount_received"], "debit_amt")
        self.assertEqual(d["reference"], "narration")
        # empty string from the model becomes None, not the literal ""
        self.assertIsNone(d["gross_amount"])
        self.assertEqual(mapping.missing_required(), [])
        # only headers + a capped sample were sent, never the whole file
        sent = client.models.calls[0]["contents"][0].parts[0].text
        self.assertIn("txn_ref", sent)
        self.assertIn("Settlement for INV0005", sent)

    def test_missing_required_field_is_reported(self):
        guesses = {"guesses": [
            {"target_field": "payment_ref", "source_column": "txn_ref", "confidence": "high"},
            {"target_field": "date", "source_column": "", "confidence": "low"},
            {"target_field": "amount_received", "source_column": "debit_amt", "confidence": "high"},
            {"target_field": "reference", "source_column": "", "confidence": "low"},
        ], "notes": "couldn't find a date or reference column"}
        script = [_response([_part_call("submit_mapping", guesses)])]
        mapping = propose_mapping(FOREIGN_HEADERS, FOREIGN_ROWS, "payment",
                                  client=FakeClient(script), model="fake")
        self.assertEqual(sorted(mapping.missing_required()), ["date", "reference"])

    def test_no_tool_call_raises(self):
        text_only = pytypes.SimpleNamespace(
            text="I'm not sure.", function_call=None)
        script = [_response([text_only])]
        with self.assertRaises(RuntimeError):
            propose_mapping(FOREIGN_HEADERS, FOREIGN_ROWS, "payment",
                            client=FakeClient(script), model="fake")


class ApplyMappingTests(unittest.TestCase):
    def _mapping(self) -> ColumnMapping:
        return ColumnMapping(
            kind="payment",
            headers=FOREIGN_HEADERS,
            guesses=[FieldGuess(g["target_field"], g["source_column"] or None, g["confidence"])
                    for g in FOREIGN_GUESSES["guesses"]],
        )

    def test_builds_real_payment_objects(self):
        payments = apply_mapping(FOREIGN_ROWS, self._mapping(), "payment", source_label="acme_bank")
        self.assertEqual(len(payments), 2)
        p = payments[0]
        self.assertIsInstance(p, Payment)
        self.assertEqual(p.payment_ref, "TX9981")
        self.assertEqual(p.source, "acme_bank")
        self.assertEqual(p.amount_received, 50481.98)
        self.assertEqual(p.gross_amount, 50481.98)  # falls back to amount_received
        self.assertEqual(p.fee, 0.0)
        self.assertEqual(p.reference, "Settlement for INV0005")
        self.assertEqual(p.counterparty_name, "Customer 014 Pvt Ltd")
        self.assertIsNone(p.counterparty_email)

    def test_missing_required_field_raises_before_touching_rows(self):
        bad = ColumnMapping(kind="payment", headers=FOREIGN_HEADERS, guesses=[
            FieldGuess("payment_ref", "txn_ref", "high"),
            FieldGuess("amount_received", "debit_amt", "high"),
            # date and reference left unmapped
        ])
        with self.assertRaises(MappingError) as ctx:
            apply_mapping(FOREIGN_ROWS, bad, "payment")
        msg = str(ctx.exception)
        self.assertIn("date", msg)
        self.assertIn("reference", msg)

    def test_invoice_kind_derives_customer_id_when_unmapped(self):
        rows = [{"id": "INV9001", "name": "Nimbus Retail LLP", "amt": "1000.00"}]
        mapping = ColumnMapping(kind="invoice", headers=["id", "name", "amt"], guesses=[
            FieldGuess("invoice_id", "id", "high"),
            FieldGuess("customer_name", "name", "high"),
            FieldGuess("invoice_amount", "amt", "high"),
        ])
        invoices = apply_mapping(rows, mapping, "invoice")
        self.assertEqual(len(invoices), 1)
        inv = invoices[0]
        self.assertIsInstance(inv, Invoice)
        self.assertEqual(inv.invoice_id, "INV9001")
        self.assertEqual(inv.invoice_amount, 1000.00)
        self.assertTrue(inv.customer_id)  # derived, not blank
        self.assertNotIn(" ", inv.customer_id)

    def test_amounts_with_thousands_separators_parse(self):
        rows = [{"txn_ref": "TX1", "posting_date": "2026-01-01", "debit_amt": "1,234.50",
                 "narration": "x", "payer": "y"}]
        payments = apply_mapping(rows, self._mapping(), "payment")
        self.assertEqual(payments[0].amount_received, 1234.50)


class SchemaTests(unittest.TestCase):
    def test_every_field_catalog_has_required_entries(self):
        self.assertIn("payment_ref", required_fields("payment"))
        self.assertIn("invoice_id", required_fields("invoice"))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            required_fields("ledger")


if __name__ == "__main__":
    unittest.main()
