#!/usr/bin/env python3
"""Plan or apply the closed-loop outcome_review migration on an initiative root.

Dry run by default: prints the structured report (per-cycle before/after, release surface and its
evidence, contract gaps, initiative adoption decision) and writes nothing.

  migrate_outcome_loops.py --project <id> --session <session-id>
  migrate_outcome_loops.py --project <id> --session <session-id> --apply
  migrate_outcome_loops.py --project <id> --maintain            # dry run of the daily backfill pass
  migrate_outcome_loops.py --project <id> --maintain --apply

The initial adoption writes require --apply, an owner and the approving --session: pass --owner, or have
the project's plan_assignee configured in the initiatives settings (it is the owner of the
assessment/plan lane). It records one user decision and the initiative outcome_policy
{enabled, owner, session}. --maintain is the daily-tick pass: it backfills contracts only for initiatives
that already carry an enabled outcome_policy, reusing the policy's owner and session, so it writes no
decision row and needs no owner or session. An optional --surfaces file maps cycle ids to
production/main_ci/runtime/manual and overrides the evidence-derived surface; pass "-" to read that JSON
from stdin. --today defaults to the local date and is recorded on the single user adoption decision per
initiative.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from _common import fail, resolve_root

from hermes_initiatives_core import settings
from hermes_initiatives_core.outcome_migration import MigrationRefused, maintain, migrate


def _surfaces(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    if raw.strip().startswith("{"):
        text = raw
    elif raw == "-":
        text = sys.stdin.read()
    else:
        path = Path(raw).expanduser()
        if not path.is_file():
            fail(f"surfaces file not found: {path}")
        text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"surfaces JSON is invalid: {exc}")
    if not isinstance(data, dict):
        fail("surfaces JSON must be an object of cycle id to surface")
    return {str(key): str(value) for key, value in data.items()}


def _owner(explicit: str | None, root: Path, project_id: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    config, error = settings.load_settings()
    if error:
        fail(f"settings file is invalid: {error}")
    project = config.project(project_id) if project_id else None
    if project is None:
        project = next((p for p in config.projects if p.root.expanduser().resolve() == root), None)
    if project is None:
        fail("--owner is required: no configured project matches this root")
    plan_assignee = (project.plan_assignee or "").strip()
    if not plan_assignee:
        fail(f"--owner is required: project '{project.id}' has no plan_assignee")
    return plan_assignee


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project")
    parser.add_argument("--root")
    parser.add_argument("--owner", help="assessment/plan owner profile; defaults to the project's plan_assignee")
    parser.add_argument("--session", help="session that approves and records the adoption decision")
    parser.add_argument("--today", help="ISO date recorded on the adoption decision (default: today)")
    parser.add_argument("--surfaces", help="JSON file (or inline JSON, or '-') mapping cycle id to release surface")
    parser.add_argument("--apply", action="store_true", help="write the migration; without it this is a dry run")
    parser.add_argument(
        "--maintain",
        action="store_true",
        help="daily backfill pass: only initiatives already carrying an enabled outcome_policy, no new decision",
    )
    args = parser.parse_args()

    root = resolve_root(args.root, args.project)
    surfaces = _surfaces(args.surfaces)
    try:
        if args.maintain:
            report = maintain(root, today=args.today or dt.date.today(), apply=args.apply)
        else:
            if not (args.session or "").strip():
                fail("--session is required: it authorizes and dates the adoption decision")
            report = migrate(
                root,
                owner=_owner(args.owner, root, args.project),
                session=args.session,
                today=args.today or dt.date.today(),
                apply=args.apply,
                surfaces=surfaces,
            )
    except MigrationRefused as exc:
        fail(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if not report["ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
