#!/usr/bin/env python3
"""Append Kanban cards to a cycle's ``kanban_cards``. Append only: never removes, never reorders.

Run it right after creating cards for a cycle. Each card body should carry the line
``- Initiative cycle: <cycle-id>``; the page shows such cards even before they are linked, and
``--sweep`` links every one of them that is not listed or excluded.

Examples:
  link_cards.py --project my-project --cycle in-sale-precompute-runtime-c01 --card t_f0c9b4f4 --card t_63c07726
  link_cards.py --project my-project --sweep
"""

from __future__ import annotations

import argparse
import json

from _common import fail, resolve_root

from hermes_initiatives_core import settings
from hermes_initiatives_core.writers import WriteRefused, link_cards, sweep_links


def _board(args: argparse.Namespace) -> str | None:
    if args.board:
        return args.board
    config, _ = settings.load_settings()
    project = config.project(args.project) if args.project else (config.projects[0] if len(config.projects) == 1 else None)
    if project is None or not project.kanban_board:
        fail("no Kanban board: pass --board, or --project with a kanban_board in the plugin settings")
    return project.kanban_board


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", help="project id from the plugin settings")
    parser.add_argument("--root", help="initiative root folder (overrides --project)")
    parser.add_argument("--board", help="Kanban board (default: the project's kanban_board)")
    parser.add_argument("--cycle", help="cycle id, e.g. in-sale-precompute-runtime-c01")
    parser.add_argument("--card", action="append", default=[], help="card id; repeat for several")
    parser.add_argument("--sweep", action="store_true", help="link every card whose body names a cycle")
    args = parser.parse_args()
    if args.sweep == bool(args.cycle or args.card):
        fail("pass either --cycle with --card, or --sweep")
    if not args.sweep and not (args.cycle and args.card):
        fail("--cycle needs at least one --card")
    root = resolve_root(args.root, args.project)
    board = _board(args)
    try:
        if args.sweep:
            print(json.dumps({"ok": True, "linked": sweep_links(root, board)}))
        else:
            print(json.dumps({"ok": True, "cycle": args.cycle, "linked": link_cards(root, board, args.cycle, args.card)}))
    except WriteRefused as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
