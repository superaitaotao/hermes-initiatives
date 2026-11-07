#!/usr/bin/env python3
"""Inspect the current assessment epoch, or append a report/proposal receipt. Never sets a verdict."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from _common import fail, resolve_root

from hermes_initiatives_core import kanban
from hermes_initiatives_core.lifecycle import as_list
from hermes_initiatives_core.outcome_loop import derive
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import active_observations, load
from hermes_initiatives_core.writers import WriteRefused, record_observation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project")
    parser.add_argument("--root")
    parser.add_argument("--board")
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--epoch")
    parser.add_argument("--status", choices=("met", "missed", "insufficient_data", "measurement_failed"))
    parser.add_argument("--evidence")
    parser.add_argument("--proposal")
    parser.add_argument("--detail")
    args = parser.parse_args()
    root = resolve_root(args.root, args.project)
    config, error = load_settings()
    if error:
        fail(error)
    project = next((p for p in config.projects if p.root.resolve() == root.resolve()), None)
    board = args.board or (project.kanban_board if project else None)
    snapshot = load(root)
    doc = snapshot.cycles.get(args.cycle)
    if doc is None:
        fail("unknown cycle")
    fm = doc.frontmatter
    delivery = [str(c) for c in as_list(fm.get("kanban_cards"))]
    markers = kanban.cycle_markers(board)
    if markers["state"] != "fresh":
        fail(f"Kanban unavailable: {markers.get('error')}")
    excluded = set(as_list(fm.get("excluded_cards")))
    delivery = list(dict.fromkeys([*delivery, *(c for c in markers["markers"].get(args.cycle, []) if c not in excluded)]))
    assessments = [str(c) for c in as_list(fm.get("assessment_cards"))]
    found = kanban.read_cards(board, delivery + assessments)
    if found["state"] != "fresh":
        fail(f"Kanban unavailable: {found.get('error')}")
    state = derive(
        fm,
        active_observations(snapshot.observations.get(args.cycle)),
        [found["cards"][c] for c in delivery],
        [found["cards"][c] for c in assessments],
        dt.date.today(),
    )
    if args.inspect:
        print(json.dumps(state, default=str))
        return
    if not state["enabled"] or state["state"] == "source_unavailable":
        fail("assessment workflow unavailable")
    if args.epoch != state["epoch"]:
        fail("assessment epoch changed; inspect and assess the current inputs before recording")
    for name in ("evidence", "proposal"):
        try:
            text = Path(getattr(args, name) or "").expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            fail(f"assessment {name} unreadable: {exc}")
        if f"Assessment epoch: {args.epoch}" not in text.splitlines():
            fail(f"assessment {name} must bind the collection epoch; recollect rather than relabel stale evidence")
    try:
        row = record_observation(
            root,
            args.cycle,
            {"kind": "assessment", **{k: getattr(args, k) for k in ("epoch", "status", "evidence", "proposal", "detail")}},
        )
    except WriteRefused as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "observation": row}))


if __name__ == "__main__":
    main()
