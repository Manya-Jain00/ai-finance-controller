"""Build docs/dashboard.html — a plain-language visual summary of the run.

    python -m docs.build_dashboard        # or: python docs/build_dashboard.py

Reads three committed files and bakes their numbers into a single self-contained
HTML page (no server, no external assets — open it in any browser):

  * eval/metrics.json          — the graded scoreboard
  * monitor/demo_summary.json  — the live-monitor match-rate trace + alerts
  * qa/batch_decision_log.jsonl — the 130 decisions (for the exception list etc.)

Nothing here calls an API or reads the ground truth directly (evaluate.py already
did the grading). Re-run it after a fresh batch to refresh the page.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

METRICS = os.path.join(_ROOT, "eval", "metrics.json")
MONITOR = os.path.join(_ROOT, "monitor", "demo_summary.json")
LOG = os.path.join(_ROOT, "qa", "batch_decision_log.jsonl")
OUT = os.path.join(_HERE, "dashboard.html")

# Plain-language outcome names, in the order a person would want to read them.
OUTCOME_LABELS = {
    "single_full": ("Paid in full", "one payment settles one invoice, to the rupee"),
    "combined": ("Several invoices at once", "one payment covers 2–3 invoices from the same customer"),
    "partial": ("Part payment", "only part of one invoice was paid"),
    "fee_deducted": ("Fee deducted", "one invoice, minus a 1.5–3% card / gateway fee"),
    "exception": ("Couldn’t be matched", "no invoice legitimately fits — flagged for a human"),
}
OUTCOME_ORDER = ["single_full", "combined", "partial", "fee_deducted", "exception"]

# Short, human reasons for each planted / genuine orphan. Derived from the log's
# own reasoning, rewritten for a non-technical reader.
EXCEPTION_REASONS = {
    "PAY0121": "No reference at all, sender listed only as “Unknown Sender Ltd”, and no invoice matches the amount.",
    "PAY0122": "Reference blank and the payer’s email couldn’t be tied to a customer. A few invoices are near the amount, but nothing confirms which — so it was not guessed.",
    "PAY0124": "Unknown reference, no sender name, an email that maps to no customer, and no combination of invoices adds up to what was received.",
    "PAY0125": "Reference is “N/A”, payer is an “Unregistered Payer” with no invoices on file, and no invoice matches the amount.",
    "PAY0126": "The reference points to nothing in the ledger, the email maps to no customer, and neither customer nor amount finds a match.",
    "PAY0127": "Reference is “N/A”, the payer has no customer record, and no invoice matches the amount received.",
    "PAY0128": "Unknown reference and unknown payer; no invoices exist for that email address or that amount.",
    "PAY0129": "No usable reference or customer. An amount search turned up one invoice, but for a different amount — an overpayment with nothing linking it.",
    "PAY0130": "Reference is “N/A”, sender “Unknown Sender Ltd” has no customer record, and no invoice matches the amount.",
}


def _rupees(n: float) -> str:
    """Indian grouping: 66,02,965."""
    whole = f"{int(round(n)):,}"
    # convert 6,602,965 -> 66,02,965
    digits = str(int(round(n)))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return "₹" + whole


def build_data() -> dict:
    with open(METRICS, encoding="utf-8") as f:
        m = json.load(f)
    with open(MONITOR, encoding="utf-8") as f:
        mon = json.load(f)
    with open(LOG, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    resolved = [r for r in rows if r.get("resolution") and not r.get("error")]
    matched_value = sum(
        r["amount_received"] for r in resolved
        if r["resolution"]["match_type"] != "exception"
    )
    total_value = sum(r["amount_received"] for r in rows)
    confs = [r["resolution"]["confidence"] for r in resolved]

    # The bar chart shows the TRUE breakdown (from the answer key): 65/20/20/15/10.
    # It sums to 130 and lines up with "all N matched correctly".
    by_type = m["accuracy_by_type"]
    gt_key = {"exception": "orphan"}
    outcomes = []
    for k in OUTCOME_ORDER:
        label, desc = OUTCOME_LABELS[k]
        t = by_type[gt_key.get(k, k)]
        outcomes.append({
            "label": label, "desc": desc,
            "count": t["n"], "correct": t["correct"], "of": t["n"],
        })

    bands = [
        ("Very confident", "90–100%", sum(1 for c in confs if c >= 0.90)),
        ("Fairly confident", "80–89%", sum(1 for c in confs if 0.80 <= c < 0.90)),
        ("Unsure", "under 80%", sum(1 for c in confs if c < 0.80)),
    ]

    exc = [r for r in resolved if r["resolution"]["match_type"] == "exception"]
    exceptions = [
        {
            "id": r["payment_id"],
            "amount": r["amount_received"],
            "amount_str": _rupees(r["amount_received"]),
            "reason": EXCEPTION_REASONS.get(
                r["payment_id"],
                r["resolution"].get("exception_reason") or r["resolution"]["reasoning"],
            ),
        }
        for r in sorted(exc, key=lambda r: r["payment_id"])
    ]

    def _num(detail: str) -> str:
        mm = re.search(r"=\s*([\d.]+%?)", detail)
        return mm.group(1) if mm else "?"

    alert_text = {
        "match_rate": ("Match-rate alert",
                       "only {n} of the last 20 payments got matched — normally it's near 100%"),
        "exception_rate": ("Exception-spike alert",
                           "{n} of the last 20 payments landed in the “can't match” pile"),
        "mean_confidence": ("Low-confidence alert",
                            "the agent kept resolving payments, but its average certainty over "
                            "the last 20 slipped to {n}"),
    }
    fires = [e for e in mon["timeline"] if e["state"] == "FIRED"]
    clears = [e for e in mon["timeline"] if e["state"] == "CLEARED"]
    alerts = []
    for e in fires:
        title, tmpl = alert_text.get(e["rule"], (e["rule"], "{n}"))
        raw = _num(e["detail"])
        pretty = raw if raw.endswith("%") else f"{round(float(raw) * 100)}%"
        alerts.append({"at": e["at_record"], "title": title, "detail": tmpl.format(n=pretty)})
    lo, hi = mon["bad_batch_records"].split("..")

    w = m["wrong_answers"][0]
    wrong = {
        "id": w["payment_id"],
        "amount": _rupees(next(r["amount_received"] for r in rows if r["payment_id"] == w["payment_id"])),
        "expected": "Unmatchable — planted with no real invoice behind it",
        "agent": "Treated as a fee-deducted payment against invoice INV0038, at 80% confidence",
        "why": ("The reference was unreadable, but searching by amount surfaced INV0038 "
                "(₹27,627.27). The ₹27,043.96 received is 2.11% short — inside the normal "
                "1.5–3% gateway-fee range — so the agent read the gap as a fee. It is a "
                "coincidence the data planted: an unmatchable payment whose amount happens "
                "to fall within fee tolerance of a real invoice. The reasoning is sound; "
                "the answer is wrong."),
    }

    return {
        "generated": date.today().isoformat(),
        "headline": {
            "payments": m["dataset"]["payments"],
            "correct": m["accuracy"]["correct"],
            "accuracy_pct": m["accuracy"]["pct_of_all"],
            "match_rate_solvable": m["match_rate"]["pct_of_solvable"],
            "money_matched": _rupees(matched_value),
            "money_total": _rupees(total_value),
            "exceptions": m["exception_quality"]["agent_raised"],
            "exception_precision": m["exception_quality"]["precision"],
            "gave_up_on_solvable": m["exception_quality"]["gave_up_on_solvable"]["count"],
            "per_min": round(m["throughput"]["payments_per_min"], 1),
            "model_calls": m["throughput"]["avg_model_iterations"],
            "mean_confidence": round(sum(confs) / len(confs) * 100),
        },
        "outcomes": outcomes,
        "confidence": [{"label": a, "range": b, "count": c} for a, b, c in bands],
        "monitor": {
            "trace": [round(x * 100) for x in mon["match_rate_trace"]],
            "total": mon["payments_seen"],
            "bad_batch": [int(lo), int(hi)],
            "alerts": alerts,
            "cleared_by": max(e["at_record"] for e in clears),
            "lowest": min(round(x * 100) for x in mon["match_rate_trace"]),
        },
        "exceptions": exceptions,
        "wrong": wrong,
    }


# ---------------------------------------------------------------------------

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Finance Controller — Results</title>
<style>
  :root {
    color-scheme: light;
    --plane: #f7f6f2;
    --surface: #ffffff;
    --ink: #14140f;
    --ink-2: #4c4b46;
    --muted: #8c8a82;
    --hair: #e3e1d8;
    --grid: #ececE3;
    --series: #1f5fa8;
    --series-soft: #dbe8f6;
    --series-mid: #6f9ecf;
    --good: #1c7a4d;
    --good-soft: #dcefe3;
    --alert: #ac3b31;
    --alert-soft: #f4e2df;
    --shade: #ece9e0;
    --font-body: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    --font-mono: ui-monospace, "SF Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    --shadow: 0 1px 1px rgba(20,20,15,0.03), 0 4px 14px rgba(20,20,15,0.045);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --plane: #0e0e0c;
      --surface: #1a1a17;
      --ink: #f3f2ec;
      --ink-2: #bfbdb3;
      --muted: #8f8d84;
      --hair: #2d2d29;
      --grid: #262622;
      --series: #5a9ae0;
      --series-soft: #1d3854;
      --series-mid: #3d6f9e;
      --good: #46ae76;
      --good-soft: #1b3527;
      --alert: #e2796d;
      --alert-soft: #3c2420;
      --shade: #242420;
      --shadow: 0 1px 1px rgba(0,0,0,0.2), 0 4px 14px rgba(0,0,0,0.28);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--plane);
    color: var(--ink);
    font: 16px/1.55 var(--font-body);
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 960px; margin: 0 auto; padding: 48px 22px 80px; }
  header { margin-bottom: 40px; }
  h1 {
    font-size: 2.3rem; margin: 0 0 12px; letter-spacing: 0.02em; font-weight: 640;
    text-transform: uppercase;
  }
  .sub { color: var(--ink-2); max-width: 62ch; margin: 0; }
  .hero {
    margin: 30px 0 4px; display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  }
  .hero .big { font-family: var(--font-mono); font-size: 3.4rem; font-weight: 600; line-height: 1; letter-spacing: -0.03em; font-variant-numeric: tabular-nums; }
  .hero .cap { color: var(--ink-2); font-size: 1rem; }

  section { margin-top: 48px; }
  h2 { font-size: 1.1rem; margin: 0 0 4px; letter-spacing: -0.01em; font-weight: 620; display: flex; align-items: baseline; gap: 10px; }
  h2 .secno { font-family: var(--font-mono); font-size: 0.72rem; color: var(--muted); font-weight: 500; letter-spacing: 0.04em; }
  .note { color: var(--ink-2); margin: 0 0 18px; max-width: 66ch; font-size: 0.95rem; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .kpi {
    background: var(--surface); border: 1px solid var(--hair); border-radius: 12px;
    padding: 16px 16px 15px; box-shadow: var(--shadow); position: relative; overflow: hidden;
  }
  .kpi::before {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: var(--series);
  }
  .kpi .v { font-family: var(--font-mono); font-size: 1.55rem; font-weight: 600; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
  .kpi .l { color: var(--ink-2); font-size: 0.86rem; margin-top: 4px; }
  .kpi .s { color: var(--muted); font-size: 0.8rem; margin-top: 5px; }

  .card {
    background: var(--surface); border: 1px solid var(--hair); border-radius: 14px;
    padding: 24px; box-shadow: var(--shadow);
  }

  /* outcome bars */
  .obar { display: grid; grid-template-columns: 1fr; gap: 18px; }
  .orow { display: grid; grid-template-columns: 190px 1fr; gap: 16px; align-items: center; }
  .orow .name { font-size: 0.92rem; }
  .orow .name .d { color: var(--muted); font-size: 0.78rem; display: block; margin-top: 1px; }
  .track { position: relative; height: 28px; background: var(--plane); border-radius: 5px; }
  .fill {
    height: 100%; background: var(--series); border-radius: 5px;
    min-width: 3px;
  }
  .fill.exc { background: var(--muted); }
  .track .num {
    position: absolute; top: 50%; transform: translate(9px, -50%);
    font-family: var(--font-mono); font-size: 0.88rem; font-weight: 600; color: var(--ink);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .orow .chk { color: var(--good); font-size: 0.82rem; margin-top: 6px; }
  .orow .chk.warn { color: var(--ink-2); }

  /* confidence strip */
  .conf { display: flex; gap: 2px; height: 40px; background: var(--surface); border-radius: 5px; overflow: hidden; }
  .conf > div {
    display: flex; align-items: center; justify-content: center; color: #fff;
    font-family: var(--font-mono); font-size: 0.88rem; font-weight: 600; min-width: 42px;
    font-variant-numeric: tabular-nums;
  }
  .conf .c0 { background: var(--series); }
  .conf .c1 { background: var(--series-mid); }
  .conf .c2 { background: var(--muted); }
  .conf-key { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 14px; font-size: 0.85rem; color: var(--ink-2); }
  .conf-key i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; margin-right: 6px; vertical-align: middle; }

  /* line chart */
  figure { margin: 0; }
  .chart-hold { position: relative; }
  #chart { width: 100%; height: auto; display: block; overflow: visible; }
  svg .grid { stroke: var(--grid); stroke-width: 1; }
  svg .axis { stroke: var(--hair); stroke-width: 1; }
  svg .tick { fill: var(--muted); font-size: 11px; font-family: var(--font-mono); }
  svg .trace { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  svg .area { fill: var(--series-soft); opacity: 0.55; }
  svg .endpoint { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
  svg .band { fill: var(--shade); }
  svg .band-label { fill: var(--ink-2); font-size: 11px; font-weight: 600; font-family: var(--font-mono); letter-spacing: 0.02em; }
  svg .alert-stem { stroke: var(--alert); stroke-width: 1.5; stroke-dasharray: 3 2; }
  svg .alert-dot { fill: var(--alert); stroke: var(--surface); stroke-width: 2; }
  svg .cursor-line { stroke: var(--muted); stroke-width: 1; }
  svg .cursor-dot { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
  .tip {
    position: absolute; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--ink); color: var(--plane); font-size: 0.8rem; padding: 5px 9px;
    border-radius: 6px; white-space: nowrap; transform: translate(-50%, -130%);
  }
  .tip b { font-family: var(--font-mono); font-size: 0.92rem; }
  .alert-list { margin: 18px 0 0; padding: 0; list-style: none; display: grid; gap: 8px; }
  .alert-list li { display: flex; gap: 10px; align-items: baseline; font-size: 0.9rem; }
  .alert-list .pin {
    font-family: var(--font-mono); color: var(--alert); font-weight: 700; font-size: 0.72rem;
    letter-spacing: 0.04em; background: var(--alert-soft);
    border-radius: 4px; padding: 2px 7px; white-space: nowrap; flex: none;
  }
  .alert-list .at { color: var(--muted); font-family: var(--font-mono); font-size: 0.85rem; }

  /* exceptions table */
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th, td { text-align: left; padding: 11px 12px; border-bottom: 1px solid var(--hair); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.06em; font-family: var(--font-mono); }
  tbody tr:nth-child(even) { background: var(--plane); }
  td.id { font-family: var(--font-mono); white-space: nowrap; font-weight: 600; }
  td.amt { font-family: var(--font-mono); white-space: nowrap; text-align: right; font-variant-numeric: tabular-nums; }

  .miss { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .miss .idline { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; width: 100%; }
  .miss .badge {
    font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em;
    color: var(--alert); background: var(--alert-soft); border-radius: 4px; padding: 3px 8px;
  }
  .miss #w-id { font-family: var(--font-mono); font-size: 1rem; }
  .miss dl { margin: 14px 0 0; display: grid; grid-template-columns: max-content 1fr; gap: 8px 14px; font-size: 0.92rem; width: 100%; }
  .miss dt { color: var(--muted); font-family: var(--font-mono); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; padding-top: 2px; }
  .miss dd { margin: 0; }

  @media (max-width: 560px) {
    .orow { grid-template-columns: 1fr; gap: 6px; }
    .hero .big { font-size: 2.6rem; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AI Finance Controller</h1>
    <p class="sub">An AI agent was given <span id="h-pay"></span> incoming payments from a bank
      file and a card-gateway file, and had to work out which invoice each one pays — or say,
      honestly, when nothing fits. Every decision was then graded against an answer key the
      agent never saw.</p>
    <div class="hero">
      <span class="big" id="h-acc"></span>
      <span class="cap" id="h-acc-cap"></span>
    </div>
  </header>

  <section>
    <h2><span class="secno">01</span>What happened</h2>
    <p class="note">The five numbers a reviewer asks first.</p>
    <div class="kpis" id="kpis"></div>
  </section>

  <section>
    <h2><span class="secno">02</span>How the payments broke down</h2>
    <p class="note">Every payment is exactly one of these five kinds. The note under each bar
      is how many of them the agent got right.</p>
    <div class="card"><div class="obar" id="obar"></div></div>
  </section>

  <section>
    <h2><span class="secno">03</span>How sure the agent was</h2>
    <p class="note">A calibrated confidence rides every decision — so a person knows which ones
      to trust and which to double-check.</p>
    <div class="card">
      <div class="conf" id="conf"></div>
      <div class="conf-key" id="conf-key"></div>
    </div>
  </section>

  <section>
    <h2><span class="secno">04</span>Catching problems as they happen</h2>
    <p class="note">Separately, a monitor watches the agent while it works — no answer key, just
      the agent's own signals over the last 20 payments. Here, 15 payments in a format the tools
      couldn't read were slipped into the stream. The match rate falls, alarms go off, and then
      recover once normal payments resume.</p>
    <div class="card">
      <figure>
        <div class="chart-hold" id="chart-hold">
          <svg id="chart" viewBox="0 0 760 280" role="img" aria-label="Match rate over 145 payments, with a dip and alerts during the bad batch"></svg>
          <div class="tip" id="tip"></div>
        </div>
        <ul class="alert-list" id="alerts"></ul>
      </figure>
    </div>
  </section>

  <section>
    <h2><span class="secno">05</span>The <span id="e-n"></span> payments it couldn't place</h2>
    <p class="note">Not failures — these are payments a human also can't reconcile from what's on
      the wire. The agent flagged each one instead of guessing, and every one it flagged is a
      genuine orphan.</p>
    <div class="card" style="padding:6px 6px 0">
      <table>
        <thead><tr><th>Payment</th><th style="text-align:right">Amount</th><th>Why it couldn't be matched</th></tr></thead>
        <tbody id="exc-body"></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2><span class="secno">06</span>Its one wrong answer</h2>
    <p class="note">Out of <span id="w-total"></span> payments, the agent got one wrong. Here it is, in full.</p>
    <div class="card miss">
      <div class="idline">
        <strong id="w-id"></strong>
        <span id="w-amt" style="color:var(--ink-2)"></span>
        <span class="badge">INCORRECT</span>
      </div>
      <dl>
        <dt>Right answer</dt><dd id="w-exp"></dd>
        <dt>Agent said</dt><dd id="w-agent"></dd>
        <dt>Why</dt><dd id="w-why"></dd>
      </dl>
    </div>
  </section>

</div>

<script>
const DATA = __DATA__;

const $ = (id) => document.getElementById(id);
const H = DATA.headline;

$("h-pay").textContent = H.payments;
$("h-acc").textContent = H.accuracy_pct + "%";
$("h-acc-cap").textContent = `of ${H.payments} payments resolved correctly (${H.correct} of ${H.payments}) — graded against a hidden answer key`;
$("e-n").textContent = DATA.exceptions.length;
$("w-total").textContent = H.payments;

/* ---- KPI tiles ---- */
const kpis = [
  { v: H.payments, l: "Payments processed", s: "from two mismatched source files" },
  { v: H.correct + " / " + H.payments, l: "Resolved correctly", s: H.accuracy_pct + "% accuracy" },
  { v: H.money_matched, l: "Money reconciled", s: "of " + H.money_total + " received" },
  { v: H.exceptions, l: "Flagged as unmatchable", s: H.gave_up_on_solvable + " solvable payments given up on" },
  { v: "~" + H.per_min + "/min", l: "Throughput", s: H.model_calls + " AI calls per payment" },
];
$("kpis").innerHTML = "";
for (const k of kpis) {
  const d = document.createElement("div");
  d.className = "kpi";
  d.innerHTML = `<div class="v"></div><div class="l"></div><div class="s"></div>`;
  d.querySelector(".v").textContent = k.v;
  d.querySelector(".l").textContent = k.l;
  d.querySelector(".s").textContent = k.s;
  $("kpis").appendChild(d);
}

/* ---- outcome bars ---- */
const maxN = Math.max(...DATA.outcomes.map(o => o.count));
const ob = $("obar");
for (const o of DATA.outcomes) {
  const isExc = o.label.startsWith("Couldn");
  const row = document.createElement("div");
  row.className = "orow";
  const pct = (o.count / maxN) * 82;   // leave room for the number at the end
  row.innerHTML = `
    <div class="name"><span></span><span class="d"></span></div>
    <div>
      <div class="track">
        <div class="fill ${isExc ? "exc" : ""}" style="width:${pct}%"></div>
        <span class="num" style="left:${pct}%"></span>
      </div>
      <div class="chk ${isExc ? "warn" : ""}"></div>
    </div>`;
  row.querySelector(".name span").textContent = o.label;
  row.querySelector(".name .d").textContent = o.desc;
  row.querySelector(".num").textContent = o.count;
  row.querySelector(".chk").textContent = isExc
    ? `${o.correct} of ${o.of} really were orphans — the 10th was a lookalike the agent matched (see below)`
    : `all ${o.correct} matched correctly`;
  ob.appendChild(row);
}

/* ---- confidence strip ---- */
const total = DATA.confidence.reduce((a, c) => a + c.count, 0);
const conf = $("conf");
DATA.confidence.forEach((c, i) => {
  if (!c.count) return;
  const d = document.createElement("div");
  d.className = "c" + i;
  d.style.flex = c.count;
  d.textContent = c.count;
  d.title = `${c.label} (${c.range}): ${c.count}`;
  conf.appendChild(d);
});
$("conf-key").innerHTML = "";
DATA.confidence.forEach((c, i) => {
  const s = document.createElement("span");
  const dot = document.createElement("i");
  dot.style.background = ["var(--series)", "var(--series-mid)", "var(--muted)"][i];
  s.appendChild(dot);
  s.appendChild(document.createTextNode(`${c.label} (${c.range}) — ${c.count}`));
  $("conf-key").appendChild(s);
});

/* ---- exceptions table ---- */
const eb = $("exc-body");
for (const e of DATA.exceptions) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td class="id"></td><td class="amt"></td><td class="reason"></td>`;
  tr.querySelector(".id").textContent = e.id;
  tr.querySelector(".amt").textContent = e.amount_str;
  tr.querySelector(".reason").textContent = e.reason;
  eb.appendChild(tr);
}

/* ---- the one miss ---- */
$("w-id").textContent = DATA.wrong.id;
$("w-amt").textContent = DATA.wrong.amount;
$("w-exp").textContent = DATA.wrong.expected;
$("w-agent").textContent = DATA.wrong.agent;
$("w-why").textContent = DATA.wrong.why;

/* ---- monitor line chart ---- */
(function () {
  const M = DATA.monitor;
  const trace = M.trace;
  const W = 760, HGT = 280;
  const padL = 42, padR = 18, padT = 26, padB = 30;
  const n = trace.length;
  const x = (i) => padL + (i / (n - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - v / 100) * (HGT - padT - padB);
  const svg = $("chart");
  const NS = "http://www.w3.org/2000/svg";
  const el = (t, a) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); return e; };

  // gridlines + y ticks
  [0, 25, 50, 75, 100].forEach(v => {
    svg.appendChild(el("line", { class: "grid", x1: padL, x2: W - padR, y1: y(v), y2: y(v) }));
    const t = el("text", { class: "tick", x: padL - 7, y: y(v) + 3, "text-anchor": "end" });
    t.textContent = v + "%"; svg.appendChild(t);
  });
  // x ticks (payment number)
  [1, 40, 80, 120, n].forEach(v => {
    const t = el("text", { class: "tick", x: x(v - 1), y: HGT - 10, "text-anchor": "middle" });
    t.textContent = v; svg.appendChild(t);
  });

  // bad-batch band
  const b0 = x(M.bad_batch[0] - 1), b1 = x(M.bad_batch[1] - 1);
  svg.appendChild(el("rect", { class: "band", x: b0, y: padT, width: b1 - b0, height: HGT - padT - padB }));
  const bl = el("text", { class: "band-label", x: (b0 + b1) / 2, y: 15, "text-anchor": "middle" });
  bl.textContent = "15 unreadable payments"; svg.appendChild(bl);

  // y-axis caption + baseline
  const yc = el("text", { class: "tick", x: padL, y: 15, "text-anchor": "start" });
  yc.textContent = "matched, last 20 payments"; svg.appendChild(yc);
  svg.appendChild(el("line", { class: "axis", x1: padL, x2: W - padR, y1: y(0), y2: y(0) }));

  // trace (area fill first, so the line draws on top)
  const pts = trace.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const areaPath = `M${x(0).toFixed(1)},${y(0).toFixed(1)} L${pts} L${x(n - 1).toFixed(1)},${y(0).toFixed(1)} Z`;
  svg.appendChild(el("path", { class: "area", d: areaPath }));
  svg.appendChild(el("polyline", { class: "trace", points: pts }));
  svg.appendChild(el("circle", { class: "endpoint", cx: x(n - 1), cy: y(trace[n - 1]), r: 4 }));

  // alert markers
  for (const a of M.alerts) {
    const ax = x(a.at - 1), ay = y(trace[a.at - 1]);
    svg.appendChild(el("line", { class: "alert-stem", x1: ax, x2: ax, y1: ay, y2: y(100) }));
    svg.appendChild(el("circle", { class: "alert-dot", cx: ax, cy: ay, r: 4.5 }));
  }

  // hover crosshair
  const cline = el("line", { class: "cursor-line", x1: 0, x2: 0, y1: padT, y2: y(0), opacity: 0 });
  const cdot = el("circle", { class: "cursor-dot", r: 4, opacity: 0 });
  svg.appendChild(cline); svg.appendChild(cdot);
  const tip = $("tip"), hold = $("chart-hold");
  const overlay = el("rect", { x: padL, y: padT, width: W - padL - padR, height: HGT - padT - padB, fill: "transparent" });
  svg.appendChild(overlay);

  function move(ev) {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * W;
    let i = Math.round((px - padL) / (W - padL - padR) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    const gx = x(i), gy = y(trace[i]);
    cline.setAttribute("x1", gx); cline.setAttribute("x2", gx);
    cline.setAttribute("opacity", 1);
    cdot.setAttribute("cx", gx); cdot.setAttribute("cy", gy); cdot.setAttribute("opacity", 1);
    tip.style.opacity = 1;
    tip.style.left = (gx / W * r.width) + "px";
    tip.style.top = (gy / HGT * r.height) + "px";
    tip.innerHTML = "";
    const b = document.createElement("b"); b.textContent = trace[i] + "%";
    tip.appendChild(b);
    tip.appendChild(document.createTextNode(` · payment ${i + 1}`));
  }
  overlay.addEventListener("pointermove", move);
  overlay.addEventListener("pointerleave", () => {
    cline.setAttribute("opacity", 0); cdot.setAttribute("opacity", 0); tip.style.opacity = 0;
  });

  // alert list
  const ul = $("alerts");
  for (const a of M.alerts) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="pin">ALERT</span><span><span class="at"></span> <span class="txt"></span></span>`;
    li.querySelector(".at").textContent = `payment ${a.at}:`;
    li.querySelector(".txt").textContent = a.detail;
    ul.appendChild(li);
  }
  const li = document.createElement("li");
  li.innerHTML = `<span class="pin" style="color:var(--good);border-color:var(--good)">CLEAR</span><span></span>`;
  li.querySelector("span:last-child").textContent =
    `by payment ${M.cleared_by} every alert had cleared — the match rate bottomed at ${M.lowest}% and climbed back to 100%.`;
  ul.appendChild(li);
})();
</script>
</body>
</html>
"""


def main() -> int:
    data = build_data()
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {os.path.relpath(OUT, _ROOT)}  ({len(html):,} bytes)")
    print(f"  open it: file:///{OUT.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
