#!/usr/bin/env python3
"""Status summaries for the initiative cards, refreshed by the 30-minute tick.

  status_summaries.py tick --project <id> [--force]     print the facts of every active initiative whose summary is
                                                        missing or out of date (unchanged ones are just re-confirmed)
  status_summaries.py write --project <id> --file out.json
                                                        store {"summaries": {"in-…": {"text", "evidence_hash"}}};
                                                        all or nothing
  status_summaries.py show --project <id>               print what the page shows

Only summaries.db in the plugin data directory is written. No initiative, cycle, or Kanban file is touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import fail

from hermes_initiatives_core import summaries
from hermes_initiatives_core.reviews import ReviewStore
from hermes_initiatives_core.settings import load_settings


def _project(project_id: str | None):
    config, error = load_settings()
    if error:
        fail(f"settings file is invalid: {error}")
    if project_id:
        match = config.project(project_id)
        if match is None:
            fail(f"unknown project '{project_id}'; known: {', '.join(p.id for p in config.projects) or 'none'}")
        return match
    if len(config.projects) == 1:
        return config.projects[0]
    fail("pass --project; settings list " + str(len(config.projects)) + " projects")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("tick", "write", "show"))
    parser.add_argument("--project")
    parser.add_argument("--file")
    parser.add_argument("--force", action="store_true", help="treat every summary as out of date")
    args = parser.parse_args()
    project = _project(args.project)
    store, reviews = summaries.SummaryStore(), ReviewStore()
    if args.command == "tick":
        print(json.dumps(summaries.tick_input(project, store, reviews, force=args.force), ensure_ascii=False, indent=1))
    elif args.command == "show":
        print(json.dumps(summaries.public_view(project.id, store), ensure_ascii=False, indent=1))
    else:
        if not args.file:
            fail("write needs --file")
        try:
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            result = summaries.write_summaries(project, store, reviews, payload)
        except (OSError, json.JSONDecodeError, summaries.SummaryError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            sys.exit(2)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
