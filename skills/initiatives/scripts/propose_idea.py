#!/usr/bin/env python3
"""Propose a new idea for the user's triage. Creates one new file; never edits an existing idea.

Example:
  propose_idea.py --project my-project --title "Raise CTR on calculator queries" \\
      --size cycle --related-initiative in-seo-geo-acquisition \\
      --problem "..." --expected-outcome "..." \\
      --evidence docs/seo/weekly-review.md
"""

from __future__ import annotations

import argparse
import json

from _common import fail, resolve_root

from hermes_initiatives_core.writers import WriteRefused, propose_idea


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", required=True)
    parser.add_argument("--evidence", action="append", required=True, help="repeat for several links")
    parser.add_argument("--size", default="cycle", choices=("initiative", "cycle", "task"))
    parser.add_argument("--project")
    parser.add_argument("--root")
    parser.add_argument("--related-initiative")
    parser.add_argument("--problem")
    parser.add_argument("--expected-outcome")
    parser.add_argument("--source", help="override the automatic <profile>/<run> source")
    parser.add_argument("--body", help="free text for the note body")
    args = parser.parse_args()
    root = resolve_root(args.root, args.project)
    fields = {
        "title": args.title,
        "evidence": args.evidence,
        "size": args.size,
        "related_initiative": args.related_initiative,
        "problem": args.problem,
        "expected_outcome": args.expected_outcome,
        "source": args.source,
        "body": args.body,
    }
    try:
        idea = propose_idea(root, fields)
    except WriteRefused as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "idea": idea}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
