"""Offline tests for the Phase 5 monitor — no API, no ground truth.

    python -m unittest monitor.test_monitor
"""

from __future__ import annotations

import unittest

from .alerts import DEFAULT_RULES, AlertManager, AlertRule
from .tracker import Observation, SlidingWindowTracker, WindowStats


def rec(pid="P", match_type="single_full", confidence=0.95, error=None,
        forced_retry=False, preflight_type=None, tool_calls=2, iterations=2):
    d = {
        "payment_id": pid,
        "source": "gateway",
        "forced_retry": forced_retry,
        "iterations": iterations,
        "tool_calls": [{"tool": "x", "input": {}, "output": "y"} for _ in range(tool_calls)],
        "error": error,
    }
    if error is None:
        d["resolution"] = {"match_type": match_type, "invoice_ids": [], "confidence": confidence}
    if preflight_type is not None:
        d["preflight"] = {"suggested_match_type": preflight_type}
    return d


class TestObservation(unittest.TestCase):
    def test_reads_a_healthy_match(self):
        o = Observation.from_record(rec(match_type="combined", confidence=0.9))
        self.assertTrue(o.resolved and o.is_match)
        self.assertFalse(o.is_exception or o.is_error)
        self.assertEqual(o.confidence, 0.9)

    def test_exception_is_not_a_match(self):
        o = Observation.from_record(rec(match_type="exception", confidence=0.8))
        self.assertTrue(o.is_exception)
        self.assertFalse(o.is_match)

    def test_error_record(self):
        o = Observation.from_record(rec(error="APIError: boom"))
        self.assertTrue(o.is_error)
        self.assertFalse(o.resolved or o.is_match)
        self.assertEqual(o.confidence, 0.0)

    def test_preflight_disagreement(self):
        agree = Observation.from_record(rec(match_type="partial", preflight_type="partial"))
        dis = Observation.from_record(rec(match_type="single_full", preflight_type="exception"))
        self.assertFalse(agree.preflight_disagreed)
        self.assertTrue(dis.preflight_disagreed)


class TestTracker(unittest.TestCase):
    def test_window_evicts(self):
        t = SlidingWindowTracker(window=5, min_samples=3)
        for i in range(10):
            s = t.observe(rec(pid=f"P{i}"))
        self.assertEqual(s.window_n, 5)
        self.assertEqual(s.total_seen, 10)

    def test_match_rate_and_warmup(self):
        t = SlidingWindowTracker(window=10, min_samples=5)
        s = t.observe(rec())
        self.assertFalse(s.warm)
        for _ in range(4):
            s = t.observe(rec())
        self.assertTrue(s.warm)
        self.assertEqual(s.match_rate, 1.0)

    def test_mean_confidence_ignores_errors(self):
        t = SlidingWindowTracker(window=10, min_samples=1)
        t.observe(rec(confidence=0.8))
        t.observe(rec(error="boom"))
        s = t.observe(rec(confidence=1.0))
        self.assertAlmostEqual(s.mean_confidence, 0.9)      # (0.8 + 1.0) / 2
        self.assertAlmostEqual(s.match_rate, 2 / 3)         # error counts against it
        self.assertAlmostEqual(s.error_rate, 1 / 3)


class TestAlerts(unittest.TestCase):
    def _feed(self, mgr, tracker, records):
        events = []
        for r in records:
            events += mgr.update(tracker.observe(r))
        return events

    def test_low_match_rate_fires_and_clears(self):
        t = SlidingWindowTracker(window=10, min_samples=5)
        m = AlertManager()
        # 10 healthy -> no alert
        self._feed(m, t, [rec() for _ in range(10)])
        self.assertEqual(m.firing, [])
        # 10 exceptions -> match rate over window collapses to 0
        ev = self._feed(m, t, [rec(match_type="exception", confidence=0.7) for _ in range(10)])
        keys = {e.rule.key for e in ev if e.state == "FIRED"}
        self.assertIn("match_rate", keys)
        self.assertIn("exception_rate", keys)
        # 10 healthy again -> clears
        ev = self._feed(m, t, [rec() for _ in range(10)])
        self.assertIn("match_rate", {e.rule.key for e in ev if e.state == "CLEARED"})
        self.assertEqual(m.firing, [])

    def test_no_alert_before_min_samples(self):
        t = SlidingWindowTracker(window=20, min_samples=10)
        m = AlertManager()
        ev = self._feed(m, t, [rec(match_type="exception", confidence=0.5) for _ in range(4)])
        self.assertEqual(ev, [])           # window not warm yet
        self.assertEqual(m.firing, [])

    def test_hysteresis_no_flapping(self):
        # sit exactly between trip (0.70) and clear (0.80): 3/4 matches = 0.75
        t = SlidingWindowTracker(window=4, min_samples=4)
        m = AlertManager()
        self._feed(m, t, [rec(match_type="exception", confidence=0.5) for _ in range(4)])
        self.assertIn("match_rate", [r.key for r in m.firing])
        # now hold at 0.75 for a while — should NOT clear (needs >= 0.80)
        ev = self._feed(m, t, [rec(), rec(), rec(), rec(match_type="exception", confidence=0.5),
                               rec(), rec(), rec(), rec(match_type="exception", confidence=0.5)])
        self.assertNotIn("match_rate", {e.rule.key for e in ev if e.state == "CLEARED"})

    def test_rule_thresholds_are_ordered(self):
        for r in DEFAULT_RULES:
            if r.direction == "below":
                self.assertLess(r.trip, r.clear, r.key)
            else:
                self.assertGreater(r.trip, r.clear, r.key)


class TestReplayHealthyLog(unittest.TestCase):
    def test_healthy_prefix_raises_nothing(self):
        # first 60 records of the real Phase 4 log are all clean matches
        import os
        from agent.decision_log import DEFAULT_LOG_PATH, load_log

        snapshot = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "qa", "batch_decision_log.jsonl")
        log = DEFAULT_LOG_PATH if os.path.exists(DEFAULT_LOG_PATH) else snapshot
        if not os.path.exists(log):
            self.skipTest("no decision log present")
        rows = load_log(log)[:60]
        t = SlidingWindowTracker()
        m = AlertManager()
        for r in rows:
            m.update(t.observe(r))
        self.assertEqual(m.firing, [], "healthy data should not trip any alert")


if __name__ == "__main__":
    unittest.main()
