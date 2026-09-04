"""Smoke tests for the dashboard generator.

    python -m unittest docs.test_dashboard

No API, no network — just checks that the three committed inputs still have the
shape `build_dashboard` expects and that the page renders with every number
filled in.
"""

from __future__ import annotations

import re
import unittest

from . import build_dashboard as bd


class BuildDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = bd.build_data()

    def test_headline_numbers(self):
        h = self.data["headline"]
        self.assertEqual(h["payments"], 130)
        self.assertEqual(h["correct"], 129)
        self.assertEqual(h["exceptions"], 9)
        self.assertEqual(h["gave_up_on_solvable"], 0)
        self.assertTrue(h["money_matched"].startswith("₹"))

    def test_outcomes_sum_to_dataset(self):
        outs = self.data["outcomes"]
        self.assertEqual(len(outs), 5)
        self.assertEqual(sum(o["count"] for o in outs), 130)
        for o in outs:
            self.assertLessEqual(o["correct"], o["of"])

    def test_confidence_bands_cover_every_resolved_payment(self):
        self.assertEqual(sum(c["count"] for c in self.data["confidence"]), 130)

    def test_monitor_block(self):
        m = self.data["monitor"]
        self.assertEqual(len(m["trace"]), 145)
        self.assertEqual(len(m["alerts"]), 3)
        self.assertTrue(all("{" not in a["detail"] for a in m["alerts"]))
        self.assertEqual(m["bad_batch"], [70, 91])

    def test_exceptions_and_wrong_answer(self):
        self.assertEqual(len(self.data["exceptions"]), 9)
        self.assertEqual(self.data["wrong"]["id"], "PAY0123")


class RenderTests(unittest.TestCase):
    def test_page_has_no_unfilled_placeholders(self):
        import json

        html = bd.TEMPLATE.replace("__DATA__", json.dumps(bd.build_data(), ensure_ascii=False))
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("{d}", html)
        self.assertNotIn("{n}", html)
        # the data blob parses back
        m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
        self.assertIsNotNone(m)
        json.loads(m.group(1))
        self.assertEqual(html.count("<script"), html.count("</script>"))


if __name__ == "__main__":
    unittest.main()
