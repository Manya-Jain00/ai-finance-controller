"""Decision-log schema — the audit trail (Phase 3, spec point 3).

Every payment the agent touches produces exactly one `DecisionRecord`. This log
is the single source of truth for Phase 4 (evaluation), Phase 5 (live monitoring)
and Phase 6 (settlement Q&A), so the shape is fixed here and downstream code
reads it rather than re-deriving anything.

Nothing in this module imports the LLM SDK or the ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Resolution vocabulary the agent is allowed to emit. `exception` is the agent's
# word for "could not resolve" — it covers both planted orphans and genuinely
# unresolvable payments. The Phase 4 evaluator maps `exception` <-> the ground
# truth's `orphan`.
MATCH_TYPES = ("single_full", "combined", "partial", "fee_deducted", "exception")

# Below this, a resolution is "low confidence" and the loop forces a second
# strategy before it is allowed to stand (spec point 2).
LOW_CONFIDENCE = 0.60

# Distinct non-terminal tool calls required before an `exception` or a
# low-confidence resolution is accepted.
MIN_STRATEGIES_BEFORE_EXCEPTION = 2


@dataclass
class ToolCall:
    """One tool invocation and what came back, verbatim, for the audit trail."""

    tool: str
    input: dict[str, Any]
    output: Any
    is_error: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Resolution:
    """The agent's final answer for one payment."""

    match_type: str                       # one of MATCH_TYPES
    invoice_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0               # 0.0–1.0
    reasoning: str = ""                   # the narrative — why this answer
    strategies_tried: list[str] = field(default_factory=list)
    exception_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DecisionRecord:
    """Everything known about how one payment was resolved."""

    # --- payment identity / inputs the agent saw ---------------------------
    payment_id: str                       # canonical PAY#### (from payment_id_map)
    payment_ref: str                      # TXN#### / STL#### as it appears in source
    source: str                           # "bank" | "gateway"
    date: str
    amount_received: float
    gross_amount: float
    fee: float
    reference: str
    counterparty: Optional[str]

    # --- what the agent decided ------------------------------------------
    resolution: Optional[Resolution] = None

    # --- how it got there (audit trail) --------------------------------
    tool_calls: list[ToolCall] = field(default_factory=list)
    iterations: int = 0
    forced_retry: bool = False            # did the loop make it try again?

    # --- run metadata --------------------------------------------------
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    latency_s: float = 0.0
    timestamp: str = ""
    error: Optional[str] = None           # set if the loop failed for this payment

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @property
    def resolved(self) -> bool:
        return self.resolution is not None and self.error is None
