"""Alert rules over the sliding-window stats (Phase 5, spec point 2).

Each rule watches one field of `WindowStats`. It *trips* when the metric crosses
a threshold and only *clears* once it recovers past a slightly better one
(hysteresis) so a metric hovering on the line does not spam fire/clear events.

The headline rule for the Phase 5 demo is ``low_match_rate``: "match rate over
the last N records dropped below X%". The others (confidence, exception spike,
pre-flight disagreement, retry spike, errors) are corroborating signals that
usually move together when a bad batch arrives.

Thresholds are calibrated against the Phase 4 healthy run (130 payments):
window match rate ~0.85-1.0, mean confidence ~0.94 (never below 0.70),
exception rate ~0.07, forced-retry rate ~0.02, pre-flight disagreement ~0.008.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .tracker import WindowStats

Direction = Literal["below", "above"]


@dataclass(frozen=True)
class AlertRule:
    key: str                     # attribute on WindowStats
    label: str
    direction: Direction
    trip: float                  # cross this -> fire
    clear: float                 # recover past this -> clear
    why: str                     # what it means when it fires
    min_samples_ok: bool = True  # require WindowStats.warm before firing

    def value(self, stats: WindowStats) -> float:
        return float(getattr(stats, self.key))

    def is_tripped(self, stats: WindowStats) -> bool:
        v = self.value(stats)
        return v < self.trip if self.direction == "below" else v > self.trip

    def is_cleared(self, stats: WindowStats) -> bool:
        v = self.value(stats)
        return v >= self.clear if self.direction == "below" else v <= self.clear

    def describe(self, stats: WindowStats, *, clearing: bool = False) -> str:
        v = self.value(stats)
        pct = self.key.endswith("_rate") or self.key == "match_rate"
        fmt = (lambda x: f"{x:.0%}") if pct else (lambda x: f"{x:.2f}")
        if clearing:
            arrow = ">=" if self.direction == "below" else "<="
            return (f"{self.key} over last {stats.window_n} = {fmt(v)} "
                    f"({arrow} {fmt(self.clear)} clear)")
        arrow = "<" if self.direction == "below" else ">"
        return (f"{self.key} over last {stats.window_n} = {fmt(v)} "
                f"({arrow} {fmt(self.trip)} trip)")


# The rule set. Order = severity for display.
DEFAULT_RULES: list[AlertRule] = [
    AlertRule(
        key="match_rate", label="LOW MATCH RATE", direction="below",
        trip=0.70, clear=0.80,
        why="the agent is failing to commit to a match far more often than "
            "normal - an upstream format or data change is likely",
    ),
    AlertRule(
        key="mean_confidence", label="LOW CONFIDENCE", direction="below",
        trip=0.78, clear=0.85,
        why="the agent is resolving payments but is unsure about them",
    ),
    AlertRule(
        key="exception_rate", label="EXCEPTION SPIKE", direction="above",
        trip=0.35, clear=0.20,
        why="a burst of payments is being dumped into the exception bucket",
    ),
    AlertRule(
        key="preflight_disagree_rate", label="PRE-FLIGHT DISAGREEMENT", direction="above",
        trip=0.35, clear=0.15,
        why="the agent keeps overriding the deterministic pre-analysis — the "
            "cheap path has stopped working for this segment",
    ),
    AlertRule(
        key="forced_retry_rate", label="RETRY SPIKE", direction="above",
        trip=0.30, clear=0.12,
        why="the 2-strategy rule is firing repeatedly; first attempts are weak",
    ),
    AlertRule(
        key="error_rate", label="ERROR SPIKE", direction="above",
        trip=0.15, clear=0.05,
        why="the loop itself is failing (API errors, no resolution)",
    ),
]


@dataclass
class AlertEvent:
    state: Literal["FIRED", "CLEARED"]
    rule: AlertRule
    at_record: int               # WindowStats.total_seen when it happened
    detail: str
    window_n: int

    def line(self) -> str:
        mark = "!!!" if self.state == "FIRED" else "..."
        return f"{mark} ALERT {self.state}  {self.rule.label}  [{self.detail}]  @ record {self.at_record}"


@dataclass
class AlertManager:
    rules: list[AlertRule] = field(default_factory=lambda: list(DEFAULT_RULES))
    active: dict[str, AlertEvent] = field(default_factory=dict)
    events: list[AlertEvent] = field(default_factory=list)

    def update(self, stats: WindowStats) -> list[AlertEvent]:
        """Feed the latest window stats; return any fire/clear events this tick."""
        fired_now: list[AlertEvent] = []
        for rule in self.rules:
            on = rule.key in self.active
            if not on and rule.is_tripped(stats):
                if rule.min_samples_ok and not stats.warm:
                    continue
                ev = AlertEvent("FIRED", rule, stats.total_seen,
                                rule.describe(stats), stats.window_n)
                self.active[rule.key] = ev
                self.events.append(ev)
                fired_now.append(ev)
            elif on and rule.is_cleared(stats):
                ev = AlertEvent("CLEARED", rule, stats.total_seen,
                                rule.describe(stats, clearing=True), stats.window_n)
                del self.active[rule.key]
                self.events.append(ev)
                fired_now.append(ev)
        return fired_now

    @property
    def firing(self) -> list[AlertRule]:
        return [ev.rule for ev in self.active.values()]

    def report(self) -> dict:
        fires = [e for e in self.events if e.state == "FIRED"]
        return {
            "rules": len(self.rules),
            "total_fire_events": len(fires),
            "distinct_rules_fired": sorted({e.rule.key for e in fires}),
            "still_active_at_end": sorted(self.active),
            "timeline": [
                {"state": e.state, "rule": e.rule.key, "at_record": e.at_record,
                 "detail": e.detail}
                for e in self.events
            ],
        }
