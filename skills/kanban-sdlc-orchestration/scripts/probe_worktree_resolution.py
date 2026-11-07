#!/usr/bin/env python3
"""Replay the installed workspace resolver for one task's stored fields.

Use when any card comment, diagnostic, or blocker claims a `worktree`-kind card
will run its worker in the shared/main checkout. The resolution keys off
``workspace_path``, ``branch_name`` and the board's ``default_workdir`` only --
not off a project link -- so a repo-root ``workspace_path`` is normally
re-anchored to ``<repo>/.worktrees/<task-id>``. Verify against the installed
release rather than reasoning from the field.

SAFETY: pass a disposable throwaway repository (``git init`` + one commit) as the
workspace path. The resolver materializes a worktree, so pointing it at a live
repo creates a stray worktree on the next task id. It is read-only on the board:
the task is constructed in-process and never persisted.

The helper scripts and the interpreter live in the Hermes install, not the
project:            <hermes>/venv/bin/python    with    hermes/venv/bin on PYTHONPATH

Example:
  python3 probe_worktree_resolution.py \
      --task-id t_probe0001 --kind worktree --workspace-path /tmp/probe-repo

Follow-up: remove the probe worktree with the repository's own expected-HEAD
guarded cleanup wrapper (`scripts/cleanup-unused-worktrees.sh --worktree <abs>
--expected-head <sha> [--dry-run]`), never `rm -rf`, so the branch and the
main-checkout branch state are preserved and verified.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path


def _build_task(kb, args):
    """Construct a Task from the stored fields we care about, defaulting the rest."""
    zero_at = {"priority", "created_at", "started_at", "completed_at"}
    as_text = {"body", "assignee", "created_by"}
    kw = {}
    for field in dataclasses.fields(kb.Task):
        name = field.name
        if name == "id":
            kw[name] = args.task_id
        elif name == "title":
            kw[name] = "workspace resolution probe"
        elif name == "status":
            kw[name] = "todo"
        elif name == "workspace_kind":
            kw[name] = args.kind
        elif name == "workspace_path":
            kw[name] = args.workspace_path
        elif name == "branch_name":
            kw[name] = args.branch_name
        elif name in as_text:
            kw[name] = ""
        elif name in zero_at:
            kw[name] = 0
        elif field.default is not dataclasses.MISSING:
            continue  # let the dataclass default stand
        else:
            kw[name] = None
    return kb.Task(**kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", required=True, help="board slug, e.g. project-board")
    ap.add_argument("--task-id", required=True, help="probe task id (not persisted)")
    ap.add_argument("--workspace-path", required=True,
                    help="the card's stored workspace_path (use a THROWAWAY repo)")
    ap.add_argument("--kind", default="worktree", choices=["worktree", "scratch", "dir"])
    ap.add_argument("--branch-name", default=None)
    args = ap.parse_args()

    os.environ.setdefault("HERMES_KANBAN_BOARD", args.board)
    from hermes_cli import kanban_db as kb  # installed release, not the project
    from hermes_cli.kanban_db_workspace import resolve_workspace

    task = _build_task(kb, args)
    resolved = Path(resolve_workspace(task, board=args.board)).resolve()
    requested = Path(args.workspace_path).expanduser().resolve()

    print(f"stored workspace_path : {requested}")
    print(f"resolved by installer : {resolved}")
    print(f"runs in that checkout : {resolved == requested}")
    print(f"created               : {resolved.exists()}")
    if args.kind == "worktree":
        print(f"anchored under        : {resolved.parent}")
        print("expected for a repo-root workspace_path: False / <repo>/.worktrees/<task-id>")
    print("next: remove the probe worktree via the repo's expected-HEAD cleanup wrapper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
