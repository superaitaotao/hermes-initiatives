#!/usr/bin/env python3
"""Regression tests for kanban_triage_route.steps_for (review-route contract).

Run: python3 -m unittest discover -s \
  <hermes-home>/shared-skills/software-development/kanban-sdlc-orchestration/scripts/tests -v
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "kanban_triage_route.py"
spec = importlib.util.spec_from_file_location("kanban_triage_route", SCRIPT)
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

PR = "https://github.com/o/r/pull/7"


class ReviewRouteContract(unittest.TestCase):
    def test_local_only_review_needs_no_published_pr(self) -> None:
        steps = route.steps_for({"task": "t_x", "to": "review", "assignee": "rev",
                                 "summary": "s", "completion_contract": "local-only"})
        self.assertEqual([s["status"] for s in steps], ["ready", "review"])
        self.assertEqual(steps[1]["assignee"], "rev")
        self.assertNotIn("metadata", steps[1])

    def test_pr_backed_review_still_requires_published_pr(self) -> None:
        with self.assertRaises(route.RouteRefused):
            route.steps_for({"task": "t_x", "to": "review", "assignee": "rev",
                             "completion_contract": "example/operator"})

    def test_pr_backed_review_carries_published_pr(self) -> None:
        steps = route.steps_for({"task": "t_x", "to": "review", "assignee": "rev",
                                 "published_pr": PR})
        self.assertEqual(steps[1]["metadata"], {"published_pr": PR})

    def test_local_only_review_rejects_a_published_pr(self) -> None:
        with self.assertRaises(route.RouteRefused):
            route.steps_for({"task": "t_x", "to": "review", "assignee": "rev",
                             "completion_contract": "local-only", "published_pr": PR})

    def test_review_always_requires_assignee(self) -> None:
        with self.assertRaises(route.RouteRefused):
            route.steps_for({"task": "t_x", "to": "review", "completion_contract": "local-only"})

    def test_implementer_is_restored_before_review_so_changes_can_route_back(self) -> None:
        steps = route.steps_for({"task": "t_x", "to": "review", "assignee": "rev",
                                 "implementer": "planner", "completion_contract": "local-only"})
        self.assertEqual(steps[0], {"status": "ready", "assignee": "planner"})
        self.assertEqual(steps[1]["assignee"], "rev")

    def test_implementer_equal_to_reviewer_is_refused(self) -> None:
        with self.assertRaises(route.RouteRefused):
            route.steps_for({"task": "t_x", "to": "review", "assignee": "rev",
                             "implementer": "rev", "completion_contract": "local-only"})


class ProcessOwnershipContract(unittest.TestCase):
    @contextmanager
    def workspace_card(self, shared):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "W"
            workspace.mkdir()
            db = Path(tmp) / "kanban.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER, "
                             "worker_pid INTEGER, claim_lock TEXT, workspace_path TEXT, "
                             "completion_contract TEXT)")
                conn.execute("INSERT INTO tasks VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
                             ("t_u1", "triage", str(workspace), "local-only"))
                if shared:
                    conn.execute("INSERT INTO tasks VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
                                 ("t_u1b", "ready", str(workspace / ".worktrees/t_u1b"),
                                  "local-only"))
            env = dict(os.environ)
            env.pop("HERMES_KANBAN_TASK", None)
            child = subprocess.Popen(["sleep", "60"], cwd=workspace, env=env)
            try:
                yield db, workspace, child
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_u1_shared_workspace_does_not_own_cwd_only_child(self):
        with self.workspace_card(shared=True) as (db, workspace, child):
            self.assertIsNone(child.poll())
            row = route.preflight(db, {"task": "t_u1"}, route.ancestry(), None)
            self.assertEqual(row["status"], "triage")
            self.assertEqual(row["workspace_path"], str(workspace))

    def test_u2_exclusive_workspace_refuses_cwd_only_child(self):
        with self.workspace_card(shared=False) as (db, workspace, child):
            with self.assertRaises(route.RouteRefused) as refused:
                route.preflight(db, {"task": "t_u1"}, route.ancestry(), None)
            self.assertIn(str(child.pid), str(refused.exception))
            self.assertIn("(cwd)", str(refused.exception))

    def test_u3_owner_proofs_and_token_boundaries(self):
        cases = [
            ([b"HERMES_KANBAN_TASK=t_abc"], "", "env"),
            ([], "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
             "hermes-worker-kanban-t_abc-run-7.scope", "cgroup"),
            ([], "0::/app.slice/t_abc-review745-desktop.service", "cgroup"),
            ([], "0::/app.slice/hermes-worker-kanban-t_abcd-run-7.scope", None),
            ([b"HERMES_KANBAN_TASK=t_abcd"], "", None),
            ([b"HERMES_KANBAN_TASK=t_abc"], "0::/t_abc-run-7.scope", "env"),
        ]
        for environ, cgroup, expected in cases:
            with self.subTest(environ=environ, cgroup=cgroup):
                self.assertEqual(route.owner_reason("t_abc", environ, "/W", cgroup,
                                                    "/W", True), expected)

    def test_u4_shared_workspace_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "W"
            workspace.mkdir()
            primary = root / "primary"
            (primary / ".git").mkdir(parents=True)
            linked = root / "linked"
            linked.mkdir()
            (linked / ".git").write_text("gitdir: /primary/.git/worktrees/linked")
            alias = root / "alias"
            alias.symlink_to(workspace, target_is_directory=True)
            cases = [
                (workspace, workspace, [], ["board default_workdir"]),
                (primary, None, [], ["primary git checkout"]),
                (workspace, None, [workspace / ".worktrees/x"], ["1 other card workspaces"]),
                (workspace, None, [root / "W-2"], []),
                (linked, None, [], []),
                (alias, workspace, [], ["board default_workdir"]),
                (workspace, None, [alias], ["1 other card workspaces"]),
            ]
            for ws, default, others, expected in cases:
                with self.subTest(workspace=ws, default=default, others=others):
                    self.assertEqual(route.shared_workspace_reasons(ws, default, others), expected)


if __name__ == "__main__":
    unittest.main()
