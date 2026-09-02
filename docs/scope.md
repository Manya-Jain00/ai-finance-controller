# Scope

**Track:** Razorpay AI Buildathon — Track 04: AI Finance Controller
("Run the books and the cash position." Agents closing finance-operations loops
across 50+ synthetic records, reporting match rates and unresolved exceptions.)

## What this project does

We scope to **multi-source reconciliation** and **settlement Q&A**; forecasting
and tax-matching are separate loops we deliberately did not attempt.

Concretely: an LLM tool-use agent takes incoming payments from two
mismatched-schema sources (a bank wire file and a gateway settlement file) and
reconciles each one against an open-invoice ledger — resolving it to a single
invoice, a combination of invoices, a partial payment, a fee-deducted payment,
or an honest exception. Every decision is written to an auditable log with a
confidence score and stated reasoning. That log is then graded against a hidden
ground truth for match rate, accuracy, throughput, and exception quality;
monitored live for mid-run degradation; and queryable in natural language.

## Deliberately out of scope

- **Cash forecasting** — predicting future balances/runway.
- **Tax matching** — GST/TDS reconciliation.
- **Live payment-gateway integration** — all data is synthetic and generated
  locally with a known answer key.
