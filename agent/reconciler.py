"""The agent loop (Phase 3) — Google Gemini backend.

`Reconciler.reconcile(payment)` runs one payment through a Gemini tool-use loop
and returns a `DecisionRecord`. A manual loop (automatic function calling
disabled) is used deliberately: we need to intercept the terminating
`submit_resolution` call to enforce the "try a second strategy before an
exception" rule (spec point 2) and to capture the full tool transcript for the
audit trail (spec point 3).

Model: defaults to `gemini-3.5-flash-lite` (15 req/min on the free tier);
override with the FC_AGENT_MODEL environment variable (e.g. `gemini-3.5-flash`,
which is stronger but only 5 req/min — pair it with FC_MIN_CALL_INTERVAL=13).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from tools.loaders import Invoice, Payment

from .preflight import Preflight
from .prompts import SYSTEM_PROMPT
from .schema import (
    LOW_CONFIDENCE,
    MIN_STRATEGIES_BEFORE_EXCEPTION,
    DecisionRecord,
    Resolution,
    ToolCall,
)
from .tools_bridge import SUBMIT_TOOL_NAME, TOOL_SPECS, ToolBridge

DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_ITERATIONS = 12
MAX_OUTPUT_TOKENS = 4096

# Free-tier rate-limit handling. Measured free-tier limits (requests/minute):
#   gemini-3.5-flash / 3.6-flash / 3.7-flash : 5 RPM
#   gemini-3.5-flash-lite / 3.1-flash-lite   : 15 RPM
# We self-throttle to stay just under, and back off on 429s. Override the
# interval with FC_MIN_CALL_INTERVAL (seconds) if you switch to a flash model.
MIN_CALL_INTERVAL_S = float(os.environ.get("FC_MIN_CALL_INTERVAL", "4.5"))
MAX_429_RETRIES = 6

# The SDK logs a noisy "direct use of AFC" info line even when AFC is disabled.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


def _model() -> str:
    return os.environ.get("FC_AGENT_MODEL", DEFAULT_MODEL)


def _strip_unsupported(schema: dict) -> dict:
    """Gemini's function schema rejects a few JSON-Schema keywords."""
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


def _gemini_tool() -> types.Tool:
    decls = []
    for spec in TOOL_SPECS:
        decls.append(
            types.FunctionDeclaration(
                name=spec["name"],
                description=spec["description"],
                parameters_json_schema=_strip_unsupported(copy.deepcopy(spec["input_schema"])),
            )
        )
    return types.Tool(function_declarations=decls)


def _retry_delay_seconds(err: genai_errors.APIError, default: float = 12.0) -> float:
    """Pull Google's suggested retry delay out of a 429, else fall back."""
    try:
        for detail in (getattr(err, "details", None) or {}).get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                raw = str(detail.get("retryDelay", "")).rstrip("s")
                return float(raw) + 1.0
    except (AttributeError, ValueError, TypeError):
        pass
    m = re.search(r"retry in ([\d.]+)s", str(getattr(err, "message", err)))
    return float(m.group(1)) + 1.0 if m else default


def _payment_view(payment: Payment) -> dict:
    """Exactly what the agent is told about the payment — no answer key."""
    return {
        "payment_ref": payment.payment_ref,
        "source": payment.source,
        "date": payment.date,
        "amount_received": payment.amount_received,
        "gross_amount": payment.gross_amount,
        "fee": payment.fee,
        "reference": payment.reference,
        "counterparty_name": payment.counterparty_name,
        "counterparty_email": payment.counterparty_email,
    }


class Reconciler:
    def __init__(
        self,
        invoices: list[Invoice],
        *,
        client: "genai.Client | None" = None,
        model: str | None = None,
        payment_id_map: dict[str, str] | None = None,
        min_call_interval: float | None = None,
    ):
        self.bridge = ToolBridge(invoices)
        self.client = client or genai.Client()
        self.model = model or _model()
        self._tool = _gemini_tool()
        # payment_ref (TXN####/STL####) -> canonical PAY####
        self.payment_id_map = payment_id_map or {}
        self.min_call_interval = (
            MIN_CALL_INTERVAL_S if min_call_interval is None else min_call_interval
        )
        self._last_call_at = 0.0

    # ------------------------------------------------------------------

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[self._tool],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            ),
        )

    def _throttle(self) -> None:
        wait = self.min_call_interval - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _generate(self, contents, config, *, verbose: bool = False):
        """One model call, throttled, with 429 backoff."""
        for attempt in range(MAX_429_RETRIES + 1):
            self._throttle()
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except genai_errors.APIError as e:
                if e.code != 429 or attempt == MAX_429_RETRIES:
                    raise
                delay = _retry_delay_seconds(e)
                if verbose:
                    print(f"    (rate limited; waiting {delay:.0f}s, retry {attempt + 1})")
                time.sleep(delay)

    def reconcile(
        self,
        payment: Payment,
        *,
        verbose: bool = False,
        preflight: Preflight | None = None,
    ) -> DecisionRecord:
        rec = DecisionRecord(
            payment_id=self.payment_id_map.get(payment.payment_ref, payment.payment_ref),
            payment_ref=payment.payment_ref,
            source=payment.source,
            date=payment.date,
            amount_received=payment.amount_received,
            gross_amount=payment.gross_amount,
            fee=payment.fee,
            reference=payment.reference,
            counterparty=payment.counterparty_name or payment.counterparty_email,
            model=self.model,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        prompt = "Reconcile this payment:\n\n" + json.dumps(_payment_view(payment), indent=2)
        if preflight is not None:
            rec.preflight = preflight.to_dict()
            prompt += (
                "\n\nA deterministic pre-analysis has already been run for you, using the "
                "same tools you have. Its tool results are trustworthy:\n\n"
                + json.dumps(preflight.to_dict(), indent=2, default=str)
                + "\n\nIf this pre-analysis already resolves the payment, confirm the amounts "
                "yourself (call get_invoices if you need the figures) and then call "
                "submit_resolution. If it is inconclusive or you disagree, investigate "
                "further with your tools before submitting."
            )

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=prompt)])
        ]

        # The pre-flight already executed these search strategies deterministically;
        # their results are in the record, so they count toward the "try a second
        # strategy before an exception" rule (spec point 2).
        distinct_tools: set[str] = set(preflight.strategies_run) if preflight else set()
        distinct_tools.discard("parse_reference")  # a parse alone was never a full strategy
        forced_retry_done = False
        started = time.monotonic()
        usage_in = usage_out = 0
        config = self._config()

        try:
            for _ in range(MAX_ITERATIONS):
                rec.iterations += 1
                response = self._generate(contents, config, verbose=verbose)
                um = response.usage_metadata
                if um:
                    usage_in += um.prompt_token_count or 0
                    usage_out += (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)

                cand = (response.candidates or [None])[0]
                parts = list(cand.content.parts) if (cand and cand.content and cand.content.parts) else []
                fcalls = [p.function_call for p in parts if p.function_call]

                if verbose:
                    for p in parts:
                        if p.text and p.text.strip():
                            print(f"    · {p.text.strip()}")

                if not fcalls:
                    # Model answered without a tool call — nudge it once to submit.
                    if cand and cand.content:
                        contents.append(cand.content)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text="You must finish by calling submit_resolution.")],
                    ))
                    continue

                contents.append(cand.content)
                resp_parts: list[types.Part] = []
                submitted: Resolution | None = None

                for fc in fcalls:
                    args = dict(fc.args or {})
                    if fc.name == SUBMIT_TOOL_NAME:
                        res = _resolution_from_input(args)
                        needs_second = (
                            res.match_type == "exception" or res.confidence < LOW_CONFIDENCE
                        )
                        if (
                            needs_second
                            and len(distinct_tools) < MIN_STRATEGIES_BEFORE_EXCEPTION
                            and not forced_retry_done
                        ):
                            forced_retry_done = True
                            rec.forced_retry = True
                            if verbose:
                                print("    ! forcing a second strategy before accepting")
                            resp_parts.append(types.Part.from_function_response(
                                name=fc.name,
                                response={"accepted": False, "instruction": (
                                    f"Not accepted. You are submitting '{res.match_type}' at "
                                    f"confidence {res.confidence:.2f} but have only tried one "
                                    "strategy. Try a materially different approach (a different "
                                    "tool, or a reference-parse vs an amount/customer search) "
                                    "and then call submit_resolution again."
                                )},
                            ))
                            rec.tool_calls.append(ToolCall(
                                tool=fc.name, input=args,
                                output="rejected: needs a second strategy", is_error=True,
                            ))
                        else:
                            submitted = res
                            resp_parts.append(types.Part.from_function_response(
                                name=fc.name, response={"accepted": True},
                            ))
                            rec.tool_calls.append(ToolCall(tool=fc.name, input=args, output="recorded"))
                    else:
                        out, is_err = self.bridge.dispatch(fc.name, args)
                        if not is_err:
                            distinct_tools.add(fc.name)
                        rec.tool_calls.append(ToolCall(
                            tool=fc.name, input=args, output=out, is_error=is_err,
                        ))
                        if verbose:
                            print(f"    -> {fc.name}({json.dumps(args)}) => {json.dumps(out, default=str)[:200]}")
                        resp_parts.append(types.Part.from_function_response(
                            name=fc.name, response={"result": out},
                        ))

                contents.append(types.Content(role="user", parts=resp_parts))

                if submitted is not None:
                    rec.resolution = submitted
                    break
            else:
                rec.error = f"hit MAX_ITERATIONS ({MAX_ITERATIONS}) without a resolution"

            if rec.resolution is None and rec.error is None:
                rec.error = "loop ended without a submitted resolution"

        except genai_errors.APIError as e:
            rec.error = f"{type(e).__name__}: {e}"

        rec.latency_s = round(time.monotonic() - started, 2)
        rec.usage = {"input_tokens": usage_in, "output_tokens": usage_out}
        return rec


def _resolution_from_input(data: dict) -> Resolution:
    reason = data.get("exception_reason")
    return Resolution(
        match_type=data.get("match_type", "exception"),
        invoice_ids=[str(x).strip().upper() for x in data.get("invoice_ids") or []],
        confidence=float(data.get("confidence", 0.0) or 0.0),
        reasoning=(data.get("reasoning") or "").strip(),
        strategies_tried=list(data.get("strategies_tried") or []),
        exception_reason=(reason.strip() or None) if isinstance(reason, str) else reason,
    )
