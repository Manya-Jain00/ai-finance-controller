"""The settlement Q&A loop (Phase 6) — Google Gemini backend.

`SettlementQA.ask(question)` runs one natural-language question through a Gemini
tool-use loop whose only tools are read-only queries over the decision log
(`qa/query_tools.py`), and returns an `Answer` with the prose reply plus the full
list of tool calls it made — so every answer is traceable back to the log.

Same backend and rate-limit handling as the agent loop; the model default can be
overridden with FC_QA_MODEL (falls back to FC_AGENT_MODEL, then the built-in).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .prompts import SYSTEM_PROMPT
from .query_tools import TOOL_SPECS, QueryBridge
from .store import DecisionStore, cited_payment_ids

DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_ITERATIONS = 8
MAX_OUTPUT_TOKENS = 2048
MIN_CALL_INTERVAL_S = float(os.environ.get("FC_MIN_CALL_INTERVAL", "4.5"))
# Retries cover both rate limits (429) and transient server errors (500/503),
# which the free tier hands out fairly often under load.
MAX_RETRIES = 8

logging.getLogger("google_genai.models").setLevel(logging.ERROR)


def _model() -> str:
    return os.environ.get("FC_QA_MODEL") or os.environ.get("FC_AGENT_MODEL") or DEFAULT_MODEL


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
    decls = [
        types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parameters_json_schema=_strip_unsupported(copy.deepcopy(spec["input_schema"])),
        )
        for spec in TOOL_SPECS
    ]
    return types.Tool(function_declarations=decls)


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


@dataclass
class Answer:
    question: str
    answer: str = ""
    payment_ids: list[str] = field(default_factory=list)   # PAY#### cited in the answer
    tool_calls: list[dict] = field(default_factory=list)    # {tool, input, output}
    iterations: int = 0
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    latency_s: float = 0.0
    error: str | None = None
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SettlementQA:
    def __init__(
        self,
        store: DecisionStore,
        *,
        client: "genai.Client | None" = None,
        model: str | None = None,
        min_call_interval: float | None = None,
    ):
        self.store = store
        self.bridge = QueryBridge(store)
        self.client = client or genai.Client()
        self.model = model or _model()
        self._tool = _gemini_tool()
        self.min_call_interval = (
            MIN_CALL_INTERVAL_S if min_call_interval is None else min_call_interval
        )
        self._last_call_at = 0.0
        # rolling conversation for multi-turn use (qa.chat)
        self.history: list[types.Content] = []

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
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except genai_errors.APIError as e:
                code = e.code or 0
                retryable = code == 429 or code >= 500
                if not retryable or attempt == MAX_RETRIES:
                    raise
                delay = _retry_delay_seconds(e) if code == 429 else min(2 ** attempt, 30)
                if verbose:
                    what = "rate limited" if code == 429 else f"server {code}"
                    print(f"    ({what}; waiting {delay:.0f}s, retry {attempt + 1})")
                time.sleep(delay)

    # ------------------------------------------------------------------

    def ask(self, question: str, *, verbose: bool = False, remember: bool = False) -> Answer:
        ans = Answer(
            question=question,
            model=self.model,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        config = self._config()
        contents: list[types.Content] = list(self.history)
        contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
        turn_start = len(contents) - 1
        usage_in = usage_out = 0
        started = time.monotonic()

        try:
            for _ in range(MAX_ITERATIONS):
                ans.iterations += 1
                response = self._generate(contents, config, verbose=verbose)
                um = response.usage_metadata
                if um:
                    usage_in += um.prompt_token_count or 0
                    usage_out += (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)

                cand = (response.candidates or [None])[0]
                parts = list(cand.content.parts) if (cand and cand.content and cand.content.parts) else []
                fcalls = [p.function_call for p in parts if p.function_call]
                text = "".join(p.text for p in parts if p.text).strip()

                if not fcalls:
                    if text:
                        ans.answer = text
                        if cand and cand.content:
                            contents.append(cand.content)
                        break
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text="Please answer the question now, grounded in the log.")],
                    ))
                    continue

                if verbose and text:
                    print(f"    · {text}")
                contents.append(cand.content)
                resp_parts: list[types.Part] = []
                for fc in fcalls:
                    args = dict(fc.args or {})
                    out, is_err = self.bridge.dispatch(fc.name, args)
                    ans.tool_calls.append({"tool": fc.name, "input": args, "output": out})
                    if verbose:
                        blob = json.dumps(out, default=str)
                        print(f"    -> {fc.name}({json.dumps(args, default=str)}) => {blob[:240]}")
                    resp_parts.append(types.Part.from_function_response(
                        name=fc.name, response={"result": out} if not is_err else {"error": out},
                    ))
                contents.append(types.Content(role="user", parts=resp_parts))
            else:
                ans.error = f"hit MAX_ITERATIONS ({MAX_ITERATIONS}) without an answer"

            if not ans.answer and not ans.error:
                ans.error = "loop ended without an answer"

        except genai_errors.APIError as e:
            ans.error = f"{type(e).__name__}: {e}"

        ans.payment_ids = cited_payment_ids(ans.answer)
        ans.usage = {"input_tokens": usage_in, "output_tokens": usage_out}
        ans.latency_s = round(time.monotonic() - started, 2)

        if remember and ans.answer and not ans.error:
            self.history = contents[:turn_start] + [
                contents[turn_start],
                types.Content(role="model", parts=[types.Part(text=ans.answer)]),
            ]
        return ans
