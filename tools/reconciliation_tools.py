"""The agent's toolkit — plain, testable functions with zero LLM involvement.

Five tools, matching the Phase 2 spec:

    find_invoices_by_amount     - candidate invoices near a given amount
    find_invoices_by_customer   - a customer's invoices, by name / id / email
    parse_remittance_reference  - decode a payment reference string into invoice ids
    check_combination           - which subset of invoices sums to a payment
    check_fee_schedule          - is a shortfall explained by a standard gateway fee

Every function is pure: it takes data in and returns structured data out. The
agent loop (Phase 3) is the only thing that will ever wire these to an LLM.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field, asdict

from .loaders import Invoice

# Gateway fee band used by the data generator: 1.5%–3% of gross.
FEE_PCT_MIN = 0.015
FEE_PCT_MAX = 0.03

# Default money comparison tolerance (absolute, in currency units).
MONEY_TOL = 0.02


def _cents(x: float) -> int:
    return round(x * 100)


def _close(a: float, b: float, abs_tol: float = MONEY_TOL) -> bool:
    return abs(_cents(a) - _cents(b)) <= _cents(abs_tol)


# ---------------------------------------------------------------------------
# 1. find_invoices_by_amount
# ---------------------------------------------------------------------------


def find_invoices_by_amount(
    invoices: list[Invoice],
    amount: float,
    abs_tolerance: float = MONEY_TOL,
    pct_tolerance: float = 0.0,
) -> list[Invoice]:
    """Return invoices whose amount matches `amount`.

    A candidate matches when it is within `abs_tolerance` currency units *or*
    within `pct_tolerance` (fraction, e.g. 0.03 for 3%) of `amount`, whichever
    is looser. Results are sorted by closeness to `amount`.

    Use a tight tolerance for exact single matches; widen `pct_tolerance` to
    ~0.03 when you suspect a gateway fee was deducted, or higher for partials.
    """
    tol = max(abs_tolerance, abs(amount) * pct_tolerance)
    hits = [inv for inv in invoices if abs(inv.invoice_amount - amount) <= tol + 1e-9]
    hits.sort(key=lambda inv: abs(inv.invoice_amount - amount))
    return hits


# ---------------------------------------------------------------------------
# 2. find_invoices_by_customer
# ---------------------------------------------------------------------------


_EMAIL_CUST_RE = re.compile(r"(cust)\s*0*(\d+)\s*@", re.IGNORECASE)


def _normalise_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def customer_id_from_email(email: str | None) -> str | None:
    """`cust019@example.com` -> `CUST019`. Returns None if it doesn't fit."""
    if not email:
        return None
    m = _EMAIL_CUST_RE.search(email)
    if not m:
        return None
    return f"CUST{int(m.group(2)):03d}"


def find_invoices_by_customer(
    invoices: list[Invoice],
    *,
    customer_id: str | None = None,
    name: str | None = None,
    email: str | None = None,
) -> list[Invoice]:
    """Return every invoice belonging to one customer.

    Provide any one of `customer_id`, `name` (matched loosely — exact
    normalised match or containment either direction), or `email` (a
    `custNNN@...` address). Results keep ledger order.
    """
    if email and not customer_id:
        customer_id = customer_id_from_email(email)

    if customer_id:
        cid = customer_id.strip().upper()
        return [inv for inv in invoices if inv.customer_id.upper() == cid]

    if name:
        target = _normalise_name(name)
        if not target:
            return []
        out = []
        for inv in invoices:
            cand = _normalise_name(inv.customer_name)
            if cand == target or target in cand or cand in target:
                out.append(inv)
        return out

    return []


# ---------------------------------------------------------------------------
# 3. parse_remittance_reference
# ---------------------------------------------------------------------------


@dataclass
class ParsedReference:
    raw: str
    invoice_ids: list[str] = field(default_factory=list)
    kind: str = "none"          # none | single | list | range | unparseable
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_TOKEN_RE = re.compile(r"([A-Za-z]{2,6})\s*0*(\d+)")
_RANGE_RE = re.compile(
    r"([A-Za-z]{2,6})\s*(\d+)\s*[-‒–—]\s*(\d+)"
)
_LIST_SPLIT_RE = re.compile(r"[+,/&]| and ", re.IGNORECASE)

_NULL_TOKENS = {"", "n/a", "na", "none", "unknown", "-", "--"}

_ID_WIDTH = 4  # invoice ids are INV + 4 digits


def _canon_id(prefix: str, number: int) -> str:
    return f"{prefix.upper()}{number:0{_ID_WIDTH}d}"


def parse_remittance_reference(reference: str) -> ParsedReference:
    """Decode a payment reference string into concrete invoice ids.

    Handles the shorthand seen in the data:
      - ``INV0042``                     -> single
      - ``INV0002+INV0044+INV0054``     -> explicit list (also , / & "and")
      - ``INV0070-0071`` / ``INV0077-79`` -> inclusive range
      - ``N/A`` / ``UNKNOWN`` / empty   -> nothing to match (kind="none")

    It never invents ids beyond what the string implies; a range wider than
    60 is treated as suspicious and only the endpoints are returned.
    """
    raw = reference or ""
    text = raw.strip()
    if text.lower() in _NULL_TOKENS:
        return ParsedReference(raw=raw, kind="none", note="no reference supplied")

    # --- explicit list: multiple tokens joined by + , / & "and" ---------------
    parts = [p for p in _LIST_SPLIT_RE.split(text) if p and p.strip()]
    if len(parts) > 1:
        ids: list[str] = []
        for p in parts:
            m = _TOKEN_RE.search(p)
            if m:
                cid = _canon_id(m.group(1), int(m.group(2)))
                if cid not in ids:
                    ids.append(cid)
        if ids:
            return ParsedReference(
                raw=raw,
                invoice_ids=ids,
                kind="list" if len(ids) > 1 else "single",
                note=f"parsed {len(ids)} id(s) from a delimited list",
            )
        return ParsedReference(raw=raw, kind="unparseable", note="delimited but no ids found")

    # --- range: PREFIX<start>-<end> -----------------------------------------
    m = _RANGE_RE.search(text)
    if m:
        prefix, start_s, end_s = m.group(1), m.group(2), m.group(3)
        start = int(start_s)
        # a short right-hand side borrows the left side's leading digits:
        # 0077-79 -> end "0079"
        if len(end_s) < len(start_s):
            end_s_full = start_s[: len(start_s) - len(end_s)] + end_s
        else:
            end_s_full = end_s
        end = int(end_s_full)
        if end < start:
            return ParsedReference(
                raw=raw,
                invoice_ids=[_canon_id(prefix, start), _canon_id(prefix, end)],
                kind="unparseable",
                note="range end precedes start",
            )
        if end - start > 60:
            return ParsedReference(
                raw=raw,
                invoice_ids=[_canon_id(prefix, start), _canon_id(prefix, end)],
                kind="range",
                note="suspiciously wide range; only endpoints returned",
            )
        ids = [_canon_id(prefix, n) for n in range(start, end + 1)]
        return ParsedReference(
            raw=raw,
            invoice_ids=ids,
            kind="range" if len(ids) > 1 else "single",
            note=f"expanded inclusive range {ids[0]}..{ids[-1]}",
        )

    # --- single token -------------------------------------------------------
    m = _TOKEN_RE.search(text)
    if m:
        return ParsedReference(
            raw=raw,
            invoice_ids=[_canon_id(m.group(1), int(m.group(2)))],
            kind="single",
            note="single invoice id",
        )

    return ParsedReference(raw=raw, kind="unparseable", note="no invoice id pattern found")


# ---------------------------------------------------------------------------
# 4. check_combination
# ---------------------------------------------------------------------------


@dataclass
class Combination:
    invoice_ids: list[str]
    total: float
    diff: float                # signed: total - target
    customer_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def check_combination(
    invoices: list[Invoice],
    target_amount: float,
    *,
    min_k: int = 2,
    max_k: int = 3,
    abs_tolerance: float = MONEY_TOL,
    same_customer: bool = True,
    max_pool: int = 30,
) -> list[Combination]:
    """Find subsets of `invoices` whose amounts sum to `target_amount`.

    Combined payments in the data cover 2–3 invoices from the *same* customer,
    so `same_customer=True` and `max_k=3` by default. Combinations are returned
    best-fit first (smallest absolute difference, then fewest invoices).

    To keep this cheap the search is bounded: with `same_customer=True` each
    customer group is searched independently; otherwise, if the pool exceeds
    `max_pool` only pairs (k=2) are considered and a note is implied by the
    absence of larger combinations.
    """
    results: list[Combination] = []

    if same_customer:
        groups: dict[str, list[Invoice]] = {}
        for inv in invoices:
            groups.setdefault(inv.customer_id, []).append(inv)
        buckets = list(groups.items())
    else:
        buckets = [(None, list(invoices))]

    for cid, pool in buckets:
        top_k = max_k
        if len(pool) > max_pool:
            top_k = min(max_k, 2)  # only pairs are safe on a big unfiltered pool
        for k in range(max(2, min_k), top_k + 1):
            if k > len(pool):
                break
            for combo in itertools.combinations(pool, k):
                total = round(sum(c.invoice_amount for c in combo), 2)
                if _close(total, target_amount, abs_tolerance):
                    results.append(
                        Combination(
                            invoice_ids=[c.invoice_id for c in combo],
                            total=total,
                            diff=round(total - target_amount, 2),
                            customer_id=cid,
                        )
                    )

    results.sort(key=lambda c: (abs(c.diff), len(c.invoice_ids)))
    return results


# ---------------------------------------------------------------------------
# 5. check_fee_schedule
# ---------------------------------------------------------------------------


@dataclass
class FeeCheckResult:
    explained: bool
    shortfall: float            # invoice_amount - amount_received
    implied_fee_pct: float | None
    schedule_min_pct: float
    schedule_max_pct: float
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_fee_schedule(
    amount_received: float,
    invoice_amount: float,
    *,
    fee_pct_min: float = FEE_PCT_MIN,
    fee_pct_max: float = FEE_PCT_MAX,
    slack: float = 0.002,
) -> FeeCheckResult:
    """Decide whether `amount_received` being short of `invoice_amount` is
    fully explained by a standard gateway processing fee (1.5%–3% of gross).

    `explained=True` means the shortfall sits inside the fee band (with a
    small `slack`). A larger gap (a genuine partial payment) or an overpayment
    returns `explained=False` with an informative note.
    """
    shortfall = round(invoice_amount - amount_received, 2)

    if invoice_amount <= 0:
        return FeeCheckResult(False, shortfall, None, fee_pct_min, fee_pct_max,
                              "invoice amount is non-positive")

    if shortfall < -0.01:
        return FeeCheckResult(False, shortfall, None, fee_pct_min, fee_pct_max,
                              "amount received exceeds the invoice (overpayment)")

    if abs(shortfall) <= 0.01:
        return FeeCheckResult(False, shortfall, 0.0, fee_pct_min, fee_pct_max,
                              "amounts already match; no fee needed")

    implied = round(shortfall / invoice_amount, 4)
    explained = (fee_pct_min - slack) <= implied <= (fee_pct_max + slack)
    if explained:
        note = f"shortfall is {implied*100:.2f}% of invoice - within the {fee_pct_min*100:.1f}-{fee_pct_max*100:.1f}% fee band"
    elif implied < fee_pct_min - slack:
        note = f"shortfall is only {implied*100:.2f}% - smaller than any standard fee"
    else:
        note = f"shortfall is {implied*100:.2f}% - too large for a fee, likely a partial payment"
    return FeeCheckResult(explained, shortfall, implied, fee_pct_min, fee_pct_max, note)
