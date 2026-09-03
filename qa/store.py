"""Read-only query layer over the decision log (Phase 6).

`DecisionStore` loads the JSONL audit trail written by the agent (Phase 3/4) and
exposes a handful of plain, testable query methods. The conversational layer in
`qa/assistant.py` calls these through a tool bridge; nothing here imports an LLM
SDK and nothing here reads `ground_truth.json` — the log is the only source.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)

# The live log the agent writes (git-ignored). A committed snapshot ships beside
# this module so the demo and a fresh clone still work without an API key.
LIVE_LOG_PATH = os.path.join(_REPO_ROOT, "agent", "decision_log.jsonl")
SNAPSHOT_LOG_PATH = os.path.join(_THIS_DIR, "batch_decision_log.jsonl")

MATCH_TYPES = ("single_full", "combined", "partial", "fee_deducted", "exception")

# match_type -> words a human might use for it in a question
_SYNONYMS = {
    "exception": {"exception", "orphan", "unmatched", "unresolved", "no match",
                  "no-match", "nomatch", "not matched", "didn't match", "did not match"},
    "combined": {"combined", "combination", "multi-invoice", "multi invoice", "grouped"},
    "partial": {"partial", "part payment", "part-payment", "short payment", "underpaid"},
    "fee_deducted": {"fee_deducted", "fee deducted", "fee-deducted", "gateway fee",
                     "processing fee", "fee"},
    "single_full": {"single_full", "single", "full", "single full", "paid in full",
                    "full payment", "exact"},
}

_HIGH_CONF = 0.90
_LOW_CONF = 0.60


def default_log_path() -> str:
    """The live log if the agent has produced one, else the committed snapshot."""
    if os.path.exists(LIVE_LOG_PATH):
        return LIVE_LOG_PATH
    return SNAPSHOT_LOG_PATH


def resolve_match_type(word: str) -> Optional[str]:
    """Map a free-text phrase like 'orphan' or 'fee deducted' to a match_type."""
    w = (word or "").strip().lower()
    if w in MATCH_TYPES:
        return w
    for mt, words in _SYNONYMS.items():
        if w in words:
            return mt
    return None


def confidence_band(conf: float) -> str:
    if conf >= _HIGH_CONF:
        return "high"
    if conf >= _LOW_CONF:
        return "medium"
    return "low"


def _norm_payment_id(identifier: str) -> str:
    """Accept 'PAY0047', 'pay47', '#47', '47' and return a canonical 'PAY0047'."""
    s = str(identifier).strip().upper()
    s = s.lstrip("#").strip()
    if s.startswith("PAY"):
        s = s[3:]
    if s.isdigit():
        return "PAY" + s.zfill(4)
    return str(identifier).strip().upper()


class DecisionStore:
    def __init__(self, records: list[dict]):
        self.records = records
        self._by_pid: dict[str, dict] = {}
        for r in records:
            for key in (r.get("payment_id"), r.get("payment_ref")):
                if key:
                    self._by_pid.setdefault(str(key).upper(), r)

    # -- construction ---------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str] = None) -> "DecisionStore":
        path = path or default_log_path()
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return cls(rows)

    def __len__(self) -> int:
        return len(self.records)

    # -- lookups ------------------------------------------------------

    def get(self, identifier: str) -> Optional[dict]:
        raw = str(identifier).strip().upper().lstrip("#")
        if raw in self._by_pid:
            return self._by_pid[raw]
        return self._by_pid.get(_norm_payment_id(identifier))

    def by_invoice(self, invoice_id: str) -> list[dict]:
        want = str(invoice_id).strip().upper()
        out = []
        for r in self.records:
            res = r.get("resolution") or {}
            if want in [str(i).upper() for i in res.get("invoice_ids") or []]:
                out.append(r)
        return out

    # -- filtered search --------------------------------------------

    def search(
        self,
        *,
        match_type: Optional[str] = None,
        min_amount: Optional[float] = None,
        max_amount: Optional[float] = None,
        min_confidence: Optional[float] = None,
        max_confidence: Optional[float] = None,
        source: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        forced_retry: Optional[bool] = None,
        preflight_disagreed: Optional[bool] = None,
        had_error: Optional[bool] = None,
        invoice_id: Optional[str] = None,
        reference_contains: Optional[str] = None,
        counterparty_contains: Optional[str] = None,
        reasoning_contains: Optional[str] = None,
    ) -> list[dict]:
        mt = resolve_match_type(match_type) if match_type else None
        src = source.strip().lower() if source else None
        inv = invoice_id.strip().upper() if invoice_id else None

        def keep(r: dict) -> bool:
            res = r.get("resolution") or {}
            amt = r.get("amount_received")
            conf = res.get("confidence")
            if mt and res.get("match_type") != mt:
                return False
            if src and (r.get("source") or "").lower() != src:
                return False
            if min_amount is not None and (amt is None or amt < min_amount):
                return False
            if max_amount is not None and (amt is None or amt > max_amount):
                return False
            if min_confidence is not None and (conf is None or conf < min_confidence):
                return False
            if max_confidence is not None and (conf is None or conf > max_confidence):
                return False
            if start_date and (r.get("date") or "") < start_date:
                return False
            if end_date and (r.get("date") or "") > end_date:
                return False
            if forced_retry is not None and bool(r.get("forced_retry")) != forced_retry:
                return False
            if had_error is not None and bool(r.get("error")) != had_error:
                return False
            if preflight_disagreed is not None and self._disagreed(r) != preflight_disagreed:
                return False
            if inv and inv not in [str(i).upper() for i in res.get("invoice_ids") or []]:
                return False
            if reference_contains and reference_contains.lower() not in (r.get("reference") or "").lower():
                return False
            if counterparty_contains and counterparty_contains.lower() not in (r.get("counterparty") or "").lower():
                return False
            if reasoning_contains and reasoning_contains.lower() not in (res.get("reasoning") or "").lower():
                return False
            return True

        return [r for r in self.records if keep(r)]

    # -- aggregates -------------------------------------------------

    def summary(self) -> dict:
        n = len(self.records)
        resolved = [r for r in self.records if (r.get("resolution") and not r.get("error"))]
        by_type: dict[str, int] = {mt: 0 for mt in MATCH_TYPES}
        matched_value = 0.0
        confidences: list[float] = []
        for r in resolved:
            res = r["resolution"]
            by_type[res.get("match_type", "exception")] = by_type.get(res.get("match_type", "exception"), 0) + 1
            if res.get("confidence") is not None:
                confidences.append(res["confidence"])
            if res.get("match_type") != "exception":
                matched_value += r.get("amount_received") or 0.0
        exceptions = by_type.get("exception", 0)
        dates = sorted(r.get("date") for r in self.records if r.get("date"))
        return {
            "payments": n,
            "resolved": len(resolved),
            "errors": sum(1 for r in self.records if r.get("error")),
            "by_match_type": by_type,
            "matched_count": len(resolved) - exceptions,
            "exception_count": exceptions,
            "total_amount_received": round(sum(r.get("amount_received") or 0.0 for r in self.records), 2),
            "total_matched_value": round(matched_value, 2),
            "mean_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "forced_retry_count": sum(1 for r in self.records if r.get("forced_retry")),
            "preflight_disagreement_count": sum(1 for r in self.records if self._disagreed(r)),
            "date_range": [dates[0], dates[-1]] if dates else None,
            "models": sorted({r.get("model") for r in self.records if r.get("model")}),
        }

    def group_by(self, field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            key = self._group_key(r, field)
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    # -- helpers ---------------------------------------------------

    @staticmethod
    def _disagreed(r: dict) -> bool:
        pf = r.get("preflight") or {}
        res = r.get("resolution") or {}
        suggested = pf.get("suggested_match_type")
        got = res.get("match_type")
        return bool(suggested and got and suggested != got)

    def _group_key(self, r: dict, field: str) -> str:
        res = r.get("resolution") or {}
        f = field.strip().lower()
        if f in ("match_type", "type", "resolution"):
            return res.get("match_type") or ("error" if r.get("error") else "unresolved")
        if f == "source":
            return r.get("source") or "unknown"
        if f == "model":
            return r.get("model") or "unknown"
        if f in ("confidence_band", "confidence"):
            c = res.get("confidence")
            return confidence_band(c) if c is not None else "none"
        if f in ("preflight_agreement", "preflight"):
            return "agreed" if not self._disagreed(r) else "overridden"
        if f in ("forced_retry", "retry"):
            return "forced_retry" if r.get("forced_retry") else "first_try"
        if f == "month":
            return (r.get("date") or "????-??")[:7]
        raise ValueError(f"cannot group by {field!r}")


# ---------------------------------------------------------------------------
# Compact projections for the LLM / CLI — full records are large.
# ---------------------------------------------------------------------------

def compact(r: dict) -> dict:
    res = r.get("resolution") or {}
    return {
        "payment_id": r.get("payment_id"),
        "date": r.get("date"),
        "source": r.get("source"),
        "amount_received": r.get("amount_received"),
        "reference": r.get("reference"),
        "counterparty": r.get("counterparty"),
        "match_type": res.get("match_type") or ("ERROR" if r.get("error") else None),
        "invoice_ids": res.get("invoice_ids") or [],
        "confidence": res.get("confidence"),
        "forced_retry": bool(r.get("forced_retry")),
    }


def detailed(r: dict) -> dict:
    res = r.get("resolution") or {}
    pf = r.get("preflight") or {}
    out = compact(r)
    out.update({
        "gross_amount": r.get("gross_amount"),
        "fee": r.get("fee"),
        "reasoning": res.get("reasoning"),
        "exception_reason": res.get("exception_reason"),
        "strategies_tried": res.get("strategies_tried") or [],
        "tool_calls": [tc.get("tool") for tc in r.get("tool_calls") or []],
        "iterations": r.get("iterations"),
        "preflight_suggested": pf.get("suggested_match_type"),
        "preflight_overridden": DecisionStore._disagreed(r),
        "model": r.get("model"),
        "error": r.get("error"),
    })
    return out


_PAY_RE = re.compile(r"\bPAY\d{3,4}\b", re.I)


def cited_payment_ids(text: str) -> list[str]:
    seen: list[str] = []
    for m in _PAY_RE.findall(text or ""):
        mu = m.upper()
        if mu not in seen:
            seen.append(mu)
    return seen
