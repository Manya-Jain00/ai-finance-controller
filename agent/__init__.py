"""Phase 3 — the LLM tool-use reconciliation agent.

    from agent import Reconciler
    from tools import load_invoices, load_payments

Public surface for Phase 4 (batch run + evaluation), Phase 5 (monitoring) and
Phase 6 (Q&A), which all read the decision log rather than re-running the agent.
"""

from .reconciler import Reconciler, DEFAULT_MODEL
from .preflight import Preflight, build_preflight
from .schema import DecisionRecord, Resolution, ToolCall, MATCH_TYPES
from .decision_log import write_log, append_record, load_log, iter_log, DEFAULT_LOG_PATH
from .tools_bridge import ToolBridge

__all__ = [
    "Reconciler",
    "DEFAULT_MODEL",
    "Preflight",
    "build_preflight",
    "ToolBridge",
    "DecisionRecord",
    "Resolution",
    "ToolCall",
    "MATCH_TYPES",
    "write_log",
    "append_record",
    "load_log",
    "iter_log",
    "DEFAULT_LOG_PATH",
]
