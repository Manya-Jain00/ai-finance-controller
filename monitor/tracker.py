"""Sliding-window health tracker (Phase 5, spec point 1).

Feed it one decision record at a time (a ``DecisionRecord`` or the plain dict
that lands in the log). It keeps the last ``window`` records and, after each
one, reports a ``WindowStats`` snapshot — the running match rate, mean
confidence, exception rate and effort over that window.

Pure and deterministic: no I/O, no LLM, no ground truth. The alert layer
(`monitor.alerts`) consumes these snapshots; the driver (`monitor.live`) wires
the two together and renders them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Iterable

DEFAULT_WINDOW = 20

# Below this many records in the window, the stats are too noisy to alert on.
DEFAULT_MIN_SAMPLES = 10


def _as_dict(record: Any) -> dict:
    if hasattr(record, "to_dict"):
        return record.to_dict()
    if isinstance(record, dict):
        return record
    raise TypeError(f"cannot read a decision record from {type(record)!r}")


@dataclass
class Observation:
    """The handful of signals the monitor extracts from one decision record."""

    payment_id: str
    resolved: bool          # agent submitted a resolution and did not error
    is_error: bool
    is_match: bool          # resolved to a concrete match (not an exception)
    is_exception: bool      # resolved, but as an exception / orphan
    confidence: float       # 0.0 for errors / unresolved
    forced_retry: bool
    preflight_suggested: bool
    preflight_disagreed: bool   # pre-flight had an opinion and the agent overrode it
    tool_calls: int
    iterations: int
    match_type: str | None

    @classmethod
    def from_record(cls, record: Any) -> "Observation":
        d = _as_dict(record)
        res = d.get("resolution") or None
        err = d.get("error")
        resolved = bool(res) and not err
        mt = res.get("match_type") if res else None

        pf = d.get("preflight") or {}
        pf_mt = pf.get("suggested_match_type")
        pf_suggested = bool(pf_mt)

        return cls(
            payment_id=d.get("payment_id") or d.get("payment_ref") or "?",
            resolved=resolved,
            is_error=bool(err),
            is_match=resolved and mt != "exception",
            is_exception=resolved and mt == "exception",
            confidence=float(res.get("confidence", 0.0) or 0.0) if resolved else 0.0,
            forced_retry=bool(d.get("forced_retry")),
            preflight_suggested=pf_suggested,
            preflight_disagreed=bool(pf_suggested and resolved and pf_mt != mt),
            tool_calls=len(d.get("tool_calls") or []),
            iterations=int(d.get("iterations") or 0),
            match_type=mt,
        )


@dataclass
class WindowStats:
    """A snapshot over the current sliding window."""

    total_seen: int         # records fed so far (not just in the window)
    window_n: int           # records currently in the window
    warm: bool              # window_n >= min_samples — safe to alert on

    match_rate: float       # concrete matches / window_n
    mean_confidence: float  # over resolved records in the window (0.0 if none)
    exception_rate: float
    error_rate: float
    forced_retry_rate: float
    preflight_disagree_rate: float   # over records where pre-flight had an opinion
    mean_tool_calls: float
    mean_iterations: float

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"match {self.match_rate:>4.0%}  conf {self.mean_confidence:.2f}  "
            f"exc {self.exception_rate:>4.0%}  retry {self.forced_retry_rate:>4.0%}  "
            f"pf!= {self.preflight_disagree_rate:>4.0%}  "
            f"eff {self.mean_tool_calls:.1f}tc/{self.mean_iterations:.1f}it"
        )


class SlidingWindowTracker:
    """Keeps the last ``window`` observations and reports stats after each one."""

    def __init__(self, window: int = DEFAULT_WINDOW, min_samples: int = DEFAULT_MIN_SAMPLES):
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self.min_samples = min(min_samples, window)
        self._buf: deque[Observation] = deque(maxlen=window)
        self.total_seen = 0
        self.history: list[WindowStats] = []

    def observe(self, record: Any) -> WindowStats:
        obs = record if isinstance(record, Observation) else Observation.from_record(record)
        self._buf.append(obs)
        self.total_seen += 1
        stats = self._stats()
        self.history.append(stats)
        return stats

    def observe_all(self, records: Iterable[Any]) -> WindowStats | None:
        stats = None
        for r in records:
            stats = self.observe(r)
        return stats

    @property
    def current(self) -> WindowStats | None:
        return self.history[-1] if self.history else None

    # ------------------------------------------------------------------

    def _stats(self) -> WindowStats:
        buf = list(self._buf)
        n = len(buf)
        resolved = [o for o in buf if o.resolved]
        pf = [o for o in buf if o.preflight_suggested]

        return WindowStats(
            total_seen=self.total_seen,
            window_n=n,
            warm=n >= self.min_samples,
            match_rate=sum(o.is_match for o in buf) / n if n else 0.0,
            mean_confidence=(
                sum(o.confidence for o in resolved) / len(resolved) if resolved else 0.0
            ),
            exception_rate=sum(o.is_exception for o in buf) / n if n else 0.0,
            error_rate=sum(o.is_error for o in buf) / n if n else 0.0,
            forced_retry_rate=sum(o.forced_retry for o in buf) / n if n else 0.0,
            preflight_disagree_rate=(
                sum(o.preflight_disagreed for o in pf) / len(pf) if pf else 0.0
            ),
            mean_tool_calls=sum(o.tool_calls for o in buf) / n if n else 0.0,
            mean_iterations=sum(o.iterations for o in buf) / n if n else 0.0,
        )
