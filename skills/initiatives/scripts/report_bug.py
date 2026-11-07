#!/usr/bin/env python3
"""File a legacy bug note (``bugs/bug-*.md``, status open); never edits an existing bug.

New tasks and bugs should use ``report_task.py``; this script remains so existing callers and the old
install layout keep working.

Example:
  report_bug.py --project my-project --title "Valuation chart drops the latest sale" --severity high \\
      --symptom "The Sep 1 sale is missing from the chart but shown in the card" \\
      --expected "Every sale in the window is plotted" \\
      --repro "Open /tools/valuation for Affinity At Serangoon 24 #09-34" \\
      --where frontend/components/valuation-history-chart.tsx \\
      --related-initiative in-property-search-ux --evidence docs/tasks/chart-bug.md
"""

from __future__ import annotations

import argparse
import json

from _common import fail, resolve_root

from hermes_initiatives_core.writers import WriteRefused, report_bug


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", required=True)
    parser.add_argument("--symptom", required=True, help="what goes wrong, as observed")
    parser.add_argument("--severity", default="medium", choices=("high", "medium", "low"))
    parser.add_argument("--expected", help="what should happen instead")
    parser.add_argument("--repro", help="steps or command that show it")
    parser.add_argument("--where", help="page, file, or service where it shows")
    parser.add_argument("--evidence", action="append", default=[], help="repeat for several links")
    parser.add_argument("--project")
    parser.add_argument("--root")
    parser.add_argument("--related-initiative")
    parser.add_argument("--source", help="override the automatic <profile>/<run> source")
    parser.add_argument("--body", help="free text for the note body")
    args = parser.parse_args()
    root = resolve_root(args.root, args.project)
    fields = {
        "title": args.title,
        "symptom": args.symptom,
        "severity": args.severity,
        "expected": args.expected,
        "repro": args.repro,
        "where": args.where,
        "evidence": args.evidence,
        "related_initiative": args.related_initiative,
        "source": args.source,
        "body": args.body,
    }
    try:
        bug = report_bug(root, fields)
    except WriteRefused as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "bug": bug}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
