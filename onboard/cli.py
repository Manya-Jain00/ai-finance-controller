"""Bring your own data — map a differently-shaped CSV onto Payment/Invoice.

    python -m onboard.cli path/to/file.csv --kind payment
    python -m onboard.cli path/to/file.csv --kind invoice --apply --json out.json
    python -m onboard.cli onboard/demo_foreign_bank.csv --kind payment --source-label acme_bank --apply

Dry-run (default): shows the AI's proposed mapping and three example rows
mapped by it, for a human to eyeball. Nothing is written. Add --apply once
you've looked at the mapping and it's right — it then builds the full list of
Payment/Invoice objects (pure Python from here, no LLM) and prints a summary.

Requires GEMINI_API_KEY in the environment or a .env file.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from .mapper import MappingError, apply_mapping, propose_mapping
from .schema import fields_for


def _read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _print_mapping(mapping, rows: list[dict]) -> None:
    fields = fields_for(mapping.kind)
    guessed = {g.target_field: g for g in mapping.guesses}
    example = rows[0] if rows else {}

    print(f"\nproposed mapping ({mapping.kind}, {len(mapping.headers)} columns in the file):")
    print(f"  {'target field':<20} {'source column':<20} {'conf':<7} example")
    print("  " + "-" * 78)
    for f in fields:
        g = guessed.get(f.name)
        col = (g.source_column if g else None) or "—"
        conf = (g.confidence if g else "-")
        ex = example.get(g.source_column, "") if (g and g.source_column) else ""
        flag = "" if col != "—" else ("  [MISSING, required]" if f.required else "  [not present]")
        print(f"  {f.name:<20} {col:<20} {conf:<7} {str(ex)[:28]}{flag}")

    if mapping.notes:
        print(f"\n  notes from the model: {mapping.notes}")

    missing = mapping.missing_required()
    if missing:
        print(f"\n  ! required field(s) not found: {', '.join(missing)} — this file cannot be "
              f"onboarded as-is (or the header names are ambiguous — check the notes above).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--kind", choices=["payment", "invoice"], required=True)
    ap.add_argument("--source-label", default="onboarded", help="tag stored as Payment.source")
    ap.add_argument("--apply", action="store_true", help="build the full object list, not just a preview")
    ap.add_argument("--json", help="write the mapped records to this path (implies --apply)")
    ap.add_argument("--sample-rows", type=int, default=5)
    args = ap.parse_args(argv)

    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        print("ERROR: set GEMINI_API_KEY (or put it in a .env file) first.", file=sys.stderr)
        return 2

    try:  # Windows consoles default to cp1252; keep the em dashes printable
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    rows = _read_rows(args.csv_path)
    if not rows:
        print("that file has no rows.", file=sys.stderr)
        return 2
    headers = list(rows[0].keys())

    print(f"reading {args.csv_path} ({len(rows)} rows, {len(headers)} columns) — asking the model "
          f"to map it to the '{args.kind}' schema...")
    mapping = propose_mapping(headers, rows[: args.sample_rows], args.kind)
    _print_mapping(mapping, rows)

    if not (args.apply or args.json):
        print("\n(dry run — re-run with --apply once this mapping looks right)")
        return 0 if not mapping.missing_required() else 1

    try:
        records = apply_mapping(rows, mapping, args.kind, source_label=args.source_label)
    except MappingError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2

    print(f"\nmapped {len(records)} {args.kind}(s). first one:")
    print(json.dumps(records[0].to_dict(), indent=2, default=str))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in records], f, indent=2, default=str)
        print(f"\nwrote {len(records)} records -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
