#!/usr/bin/env python3
"""Append one observation to a cycle's observation log. Never edits a cycle file.

Example:
  record_observation.py --project my-project --cycle in-seo-geo-acquisition-c14 \\
      --kind release_verified --environment production \\
      --evidence docs/seo/weekly-review.md --detail "15/15 URLs return 200"
"""

from __future__ import annotations

import argparse
import json

from _common import fail, resolve_root

from hermes_initiatives_core.writers import WriteRefused, record_observation

KINDS = ("release_verified", "release_failed", "metric", "guardrail", "note")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cycle", required=True, help="cycle id, e.g. in-seo-geo-acquisition-c14")
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("--project", help="project id from the plugin settings")
    parser.add_argument("--root", help="initiative root folder (overrides --project)")
    parser.add_argument("--metric")
    parser.add_argument("--value")
    parser.add_argument("--evidence-date", help="date the measured data refers to (YYYY-MM-DD)")
    parser.add_argument("--environment")
    parser.add_argument("--evidence", help="vault-relative path or URL of the report that shows this")
    parser.add_argument("--detail")
    parser.add_argument("--supersedes", help="'at' of an earlier observation this one corrects")
    args = parser.parse_args()
    root = resolve_root(args.root, args.project)
    fields = {
        k: getattr(args, k)
        for k in ("kind", "metric", "value", "evidence_date", "environment", "evidence", "detail", "supersedes")
    }
    try:
        row = record_observation(root, args.cycle, fields)
    except WriteRefused as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "observation": row}, ensure_ascii=False))


if __name__ == "__main__":
    main()
