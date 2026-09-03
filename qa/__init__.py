"""Phase 6 — the settlement Q&A layer.

A thin conversational layer that answers natural-language questions about a
finished reconciliation batch by querying the decision log (`agent/decision_log.jsonl`).
It never re-runs the agent and never reads the ground truth — the audit trail is
the single source of truth.
"""
