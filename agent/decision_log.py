"""Read/write the decision log — the Phase 3 audit trail.

Stored as JSONL (one `DecisionRecord` per line) so Phase 4/5/6 can stream it and
so a partial run is still readable. Nothing here touches the LLM or ground truth.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Iterator

from .schema import DecisionRecord, Resolution, ToolCall

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_PATH = os.path.join(_THIS_DIR, "decision_log.jsonl")


def write_log(records: Iterable[DecisionRecord], path: str = DEFAULT_LOG_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec.to_dict(), default=str) + "\n")
    return path


def append_record(record: DecisionRecord, path: str = DEFAULT_LOG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), default=str) + "\n")


def load_log(path: str = DEFAULT_LOG_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def iter_log(path: str = DEFAULT_LOG_PATH) -> Iterator[DecisionRecord]:
    for row in load_log(path):
        yield _record_from_dict(row)


def _record_from_dict(row: dict) -> DecisionRecord:
    res = row.get("resolution")
    tool_calls = [ToolCall(**tc) for tc in row.get("tool_calls", [])]
    kwargs = {k: v for k, v in row.items() if k not in ("resolution", "tool_calls")}
    rec = DecisionRecord(**kwargs)
    rec.tool_calls = tool_calls
    rec.resolution = Resolution(**res) if res else None
    return rec
