#!/usr/bin/env python3
"""Read-only outcome-loop signals for the existing review wake gate."""

from __future__ import annotations

import datetime as dt
import json

from _common import fail

from hermes_initiatives_core import views
from hermes_initiatives_core.settings import load_settings


def main() -> None:
    config, error = load_settings()
    if error:
        fail(error)
    signals = []
    for project in config.projects:
        listing = views.build_list(project, today=dt.date.today())
        if listing["freshness"]["kanban"] != "fresh":
            fail("Outcome-loop Kanban source unavailable")
        for initiative in listing["initiatives"]:
            if initiative["status"] != "active":
                continue
            for cycle in initiative["open_cycles"]:
                loop = cycle.get("outcome_loop") or {}
                if loop.get("enabled"):
                    signals.append(
                        {
                            "project": project.id,
                            "cycle": cycle["id"],
                            "epoch": loop["epoch"],
                            "state": loop["state"],
                            "due": loop.get("due_date"),
                            "card": loop.get("assessment_card"),
                            "receipt": (loop.get("assessment") or {}).get("at"),
                        }
                    )
    print(json.dumps({"signals": signals}, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
