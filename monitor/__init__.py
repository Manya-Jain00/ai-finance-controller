"""Phase 5 — live monitoring layer.

A sliding-window health tracker plus alert rules that run *during* the batch
reconciliation, not just at the end. The point is to catch the system degrading
mid-run — e.g. a block of payments arriving in a reference format the tools
cannot parse — and raise an alert while there is still time to react, rather
than discovering it at month-end close.

Nothing here opens ``ground_truth.json``. Live monitoring only sees what the
agent itself produced: the decision-log stream (match type, confidence,
exception flag, forced retries, pre-flight agreement, effort). Accuracy against
the answer key is Phase 4's job and happens offline.
"""
