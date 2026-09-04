"""Propose a column mapping with Gemini; apply a confirmed mapping with plain Python.

`propose_mapping` is the ONLY function here that calls the LLM, and all it ever
sees is a file's headers plus a handful of sample rows - never the full data,
never the accounting decision of what matches what. `apply_mapping` is pure,
deterministic, and has no LLM dependency at all: once a human has looked at the
proposed mapping and confirmed it, turning every row into a `Payment`/`Invoice`
is exactly as auditable as the hand-written loaders in `tools/loaders.py`.

    from onboard.mapper import propose_mapping, apply_mapping
    guess = propose_mapping(headers, sample_rows, kind="payment")
    # ... a human looks at guess.as_dict() and confirms it ...
    payments = apply_mapping(all_rows, guess, kind="payment", source_label="acme_bank")
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from tools.loaders import Invoice, Payment

from .schema import ColumnMapping, FieldGuess, fields_for, required_fields

DEFAULT_MODEL = "gemini-3.5-flash-lite"
MIN_CALL_INTERVAL_S = float(os.environ.get("FC_MIN_CALL_INTERVAL", "4.5"))
MAX_RETRIES = 6
SUBMIT_TOOL_NAME = "submit_mapping"
MAX_SAMPLE_ROWS = 5

logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class MappingError(ValueError):
    """A confirmed mapping doesn't cover a required field - refuse, don't guess."""


def _model() -> str:
    return os.environ.get("FC_ONBOARD_MODEL") or os.environ.get("FC_AGENT_MODEL") or DEFAULT_MODEL


def _strip_unsupported(schema: dict) -> dict:
    out = {}
    for k, v in schema.items():
        if k == "additionalProperties":
            continue
        if isinstance(v, dict):
            out[k] = _strip_unsupported(v)
        elif isinstance(v, list):
            out[k] = [_strip_unsupported(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def _retry_delay_seconds(err: genai_errors.APIError, default: float = 12.0) -> float:
    try:
        for detail in (getattr(err, "details", None) or {}).get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                raw = str(detail.get("retryDelay", "")).rstrip("s")
                return float(raw) + 1.0
    except (AttributeError, ValueError, TypeError):
        pass
    m = re.search(r"retry in ([\d.]+)s", str(getattr(err, "message", err)))
    return float(m.group(1)) + 1.0 if m else default


def _tool_spec() -> dict[str, Any]:
    return {
        "name": SUBMIT_TOOL_NAME,
        "description": (
            "Report your best mapping from the target fields to this file's actual column "
            "headers. Call this exactly once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "guesses": {
                    "type": "array",
                    "description": "One entry for every target field listed in the prompt.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_field": {"type": "string"},
                            "source_column": {
                                "type": "string",
                                "description": (
                                    "The exact header from the file that holds this field. "
                                    "Empty string if nothing in the file maps to it - never guess."
                                ),
                            },
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                        "required": ["target_field", "source_column", "confidence"],
                    },
                },
                "notes": {
                    "type": "string",
                    "description": "Anything a human reviewer should double-check. Empty string if none.",
                },
            },
            "required": ["guesses"],
        },
    }


def _gemini_tool() -> types.Tool:
    spec = _tool_spec()
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parameters_json_schema=_strip_unsupported(copy.deepcopy(spec["input_schema"])),
        )
    ])


PROMPT_TEMPLATE = """\
You are onboarding a new data file into a payments-reconciliation system. The \
system needs these target fields (kind: {kind}):

{field_list}

The file's actual column headers are:
{headers}

Here are up to {n} sample rows, as-is:
{sample_json}

Map each target field to the ONE header that holds that data. If nothing in \
the file provides a field, set source_column to an empty string - do not force \
a mapping that isn't really there. A header may be reused for at most one \
target field. Call {tool} exactly once with one entry per target field listed \
above.
"""


def _build_prompt(headers: list[str], sample_rows: list[dict], kind: str) -> str:
    fields = fields_for(kind)
    field_list = "\n".join(
        f"  - {f.name}{' (required)' if f.required else ''}: {f.description}" for f in fields
    )
    samples = sample_rows[:MAX_SAMPLE_ROWS]
    return PROMPT_TEMPLATE.format(
        kind=kind,
        field_list=field_list,
        headers=json.dumps(headers),
        n=len(samples),
        sample_json=json.dumps(samples, indent=2, default=str),
        tool=SUBMIT_TOOL_NAME,
    )


def propose_mapping(
    headers: list[str],
    sample_rows: list[dict],
    kind: str,
    *,
    client: "genai.Client | None" = None,
    model: str | None = None,
    verbose: bool = False,
) -> ColumnMapping:
    """One Gemini call: propose target-field -> source-column for `headers`.

    Never reads the full dataset - only `headers` and up to `MAX_SAMPLE_ROWS` of
    `sample_rows` are shown to the model.
    """
    client = client or genai.Client()
    model = model or _model()
    config = types.GenerateContentConfig(
        tools=[_gemini_tool()],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.0,
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY", allowed_function_names=[SUBMIT_TOOL_NAME]
            )
        ),
    )
    prompt = _build_prompt(headers, sample_rows, kind)
    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

    response = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            break
        except genai_errors.APIError as e:
            code = e.code or 0
            if not (code == 429 or code >= 500) or attempt == MAX_RETRIES:
                raise
            delay = _retry_delay_seconds(e) if code == 429 else min(2 ** attempt, 30)
            if verbose:
                print(f"  (retrying in {delay:.0f}s after {code})")
            time.sleep(delay)

    cand = (response.candidates or [None])[0]
    parts = list(cand.content.parts) if (cand and cand.content and cand.content.parts) else []
    fcalls = [p.function_call for p in parts if p.function_call]
    if not fcalls or fcalls[0].name != SUBMIT_TOOL_NAME:
        raise RuntimeError("model did not call submit_mapping - try again")

    args = dict(fcalls[0].args or {})
    guesses = [
        FieldGuess(
            target_field=g["target_field"],
            source_column=(g.get("source_column") or "").strip() or None,
            confidence=g.get("confidence", "medium"),
        )
        for g in (args.get("guesses") or [])
    ]
    return ColumnMapping(kind=kind, headers=list(headers), guesses=guesses, notes=args.get("notes") or "")


# ---------------------------------------------------------------------------
# Applying a CONFIRMED mapping - pure Python, no LLM, from here down.
# ---------------------------------------------------------------------------


def _get(row: dict, mapping: dict[str, str | None], field: str) -> str | None:
    col = mapping.get(field)
    if not col:
        return None
    val = row.get(col)
    return val.strip() if isinstance(val, str) else val


def _money(row: dict, mapping: dict[str, str | None], field: str) -> float | None:
    v = _get(row, mapping, field)
    if v in (None, ""):
        return None
    return round(float(str(v).replace(",", "")), 2)


def apply_mapping(
    rows: list[dict],
    mapping: ColumnMapping,
    kind: str,
    *,
    source_label: str = "onboarded",
) -> list:
    """Turn every row into a `Payment` (kind='payment') or `Invoice` (kind='invoice').

    Deterministic - the same mapping always produces the same objects. Raises
    `MappingError` up front, before touching a single row, if a required field
    was never mapped.
    """
    missing = mapping.missing_required()
    if missing:
        raise MappingError(
            f"mapping is missing required field(s): {', '.join(missing)} - "
            f"nothing in the file's headers ({mapping.headers}) was mapped to them"
        )
    m = mapping.as_dict()

    if kind == "payment":
        out = []
        for r in rows:
            recv = _money(r, m, "amount_received")
            gross = _money(r, m, "gross_amount")
            fee = _money(r, m, "fee")
            out.append(Payment(
                payment_ref=_get(r, m, "payment_ref") or "",
                source=source_label,
                date=_get(r, m, "date") or "",
                amount_received=recv if recv is not None else 0.0,
                gross_amount=gross if gross is not None else (recv if recv is not None else 0.0),
                fee=fee if fee is not None else 0.0,
                reference=_get(r, m, "reference") or "",
                counterparty_name=_get(r, m, "counterparty_name"),
                counterparty_email=_get(r, m, "counterparty_email"),
            ))
        return out

    if kind == "invoice":
        out = []
        for r in rows:
            name = _get(r, m, "customer_name") or ""
            cust_id = _get(r, m, "customer_id") or re.sub(r"[^A-Z0-9]+", "", name.upper())
            amt = _money(r, m, "invoice_amount")
            out.append(Invoice(
                invoice_id=_get(r, m, "invoice_id") or "",
                customer_id=cust_id,
                customer_name=name,
                invoice_amount=amt if amt is not None else 0.0,
                invoice_date=_get(r, m, "invoice_date") or "",
                due_date=_get(r, m, "due_date") or "",
            ))
        return out

    raise ValueError(f"kind must be 'payment' or 'invoice', got {kind!r}")
