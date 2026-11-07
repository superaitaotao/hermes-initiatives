#!/usr/bin/env python3
"""Move breaker-routed ``triage`` cards to ``ready`` or ``review`` through the
dashboard's supported status verbs, without pausing the fleet.

``triage`` has no CLI exit, so the only supported route is the dashboard
``PATCH /api/plugins/kanban/tasks/<id>`` verb on a temporary loopback dashboard.
This script owns that whole procedure so an operator never re-derives it:

1. Resolve the board DB from ``hermes kanban boards list --json``.
2. Start one ``hermes dashboard --isolated`` on a free loopback port and read the
   SPA-injected session token from ``GET /``.
3. Per card: preflight (expected status, no claim/run/PID, no task-owned process
   outside this script's ancestry), then hold the board's dispatch lock
   (``kanban.db.dispatch.lock``, the same flock every dispatcher tick takes
   non-blocking) across the transition. The dispatcher skips its tick for this
   board only, so ``triage -> ready -> review`` cannot be claimed mid-pair.
   ``hermes pause`` is not used: it writes ``$HERMES_HOME/ESTOP`` and a
   profile-scoped pause is invisible to the root gateway's dispatcher.
4. Read the card back. Only when the readback shows the requested status does
   the script post the card comment, so no ``RECOVERED`` comment precedes a
   failed transition.
5. Stop the dashboard in ``finally``.

Plan file (JSON list), one object per card::

    {"task": "t_abc", "to": "review", "assignee": "reviewer-profile",
     "summary": "...", "published_pr": "https://github.com/o/r/pull/1",
     "comment": "**RECOVERED** — ...", "expect": "triage"}

``to`` is ``ready`` or ``review``; ``review`` requires ``assignee``. A PR-backed
``review`` also requires ``published_pr``. A ``local-only`` card instead declares
``"completion_contract": "local-only"`` and no ``published_pr``; preflight refuses
the card when that declaration differs from the card's stored contract. Pass
``implementer`` (the profile that did the work, from the latest
``review_requested`` event) when the card is currently assigned to its reviewer;
the route restores it before ``review`` so ``request-changes`` can route back.
``expect`` defaults to ``triage``. Exit 0 when every card
landed, 1 when any card was refused (others still run), 2 on setup failure.

Usage:
    kanban_triage_route.py --board <slug> --plan plan.json [--author <name>] [--dry-run]
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERMES = shutil.which("hermes") or "hermes"
TOKEN_RE = re.compile(r"window\.__HERMES_SESSION_TOKEN__\s*=\s*(\"(?:\\.|[^\"\\])*\")")
LOCK_WAIT_SECONDS = 30.0
DASHBOARD_START_SECONDS = 90.0


class RouteRefused(Exception):
    """A per-card precondition or transition failed; the card was left unchanged
    or in the state the readback reports."""


def cli(*args: str) -> str:
    result = subprocess.run([HERMES, *args], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"hermes {' '.join(args[:4])} failed: {(result.stderr or result.stdout)[:300]}")
    return result.stdout


def board_record(board: str) -> dict:
    for row in json.loads(cli("kanban", "boards", "list", "--json")):
        if row.get("slug") == board and row.get("db_path"):
            return row
    raise RuntimeError(f"board {board!r} not found")


def board_db(board: str) -> Path:
    return Path(board_record(board)["db_path"])


def show(board: str, task: str) -> dict:
    return json.loads(cli("kanban", "--board", board, "show", task, "--json"), strict=False)


def ancestry() -> set[int]:
    pids, pid = set(), os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return pids


def owner_reason(task: str, environ: list[bytes], cwd: str, cgroup: str,
                 workspace: str | None, shared: bool) -> str | None:
    if f"HERMES_KANBAN_TASK={task}".encode() in environ:
        return "env"
    if re.search(r"(?<![a-zA-Z0-9_])" + re.escape(task) + r"(?![a-zA-Z0-9_])", cgroup):
        return "cgroup"
    if not shared and workspace and (cwd == workspace or cwd.startswith(workspace + "/")):
        return "cwd"
    return None


def shared_workspace_reasons(workspace: str | Path | None,
                             default_workdir: str | Path | None,
                             other_workspaces: list[str | Path]) -> list[str]:
    if not workspace:
        return []
    workspace_path = os.path.realpath(workspace)
    reasons = []
    if default_workdir and workspace_path == os.path.realpath(default_workdir):
        reasons.append("board default_workdir")
    if (Path(workspace_path) / ".git").is_dir():
        reasons.append("primary git checkout")
    others = [os.path.realpath(path) for path in other_workspaces if path]
    count = sum(path == workspace_path or path.startswith(workspace_path + "/")
                for path in others)
    if count:
        reasons.append(f"{count} other card workspaces")
    return reasons


def task_processes(task: str, workspace: str | None, exclude: set[int],
                   shared: bool = False) -> tuple[list[tuple[int, str]], int]:
    found: list[tuple[int, str]] = []
    ignored = 0
    workspace_path = os.path.realpath(workspace) if workspace else None
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) in exclude:
            continue
        try:
            env = (proc / "environ").read_bytes().split(b"\0")
            cwd = str((proc / "cwd").resolve())
            cgroup = (proc / "cgroup").read_text()
        except OSError:
            continue
        reason = owner_reason(task, env, cwd, cgroup, workspace_path, shared)
        if reason:
            found.append((int(proc.name), reason))
        elif shared and owner_reason(task, env, cwd, cgroup, workspace_path, False) == "cwd":
            ignored += 1
    return found, ignored


def preflight(db: Path, item: dict, exclude: set[int],
              default_workdir: str | None = None) -> dict:
    task = item["task"]
    expect = item.get("expect", "triage")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, current_run_id, worker_pid, claim_lock, workspace_path, "
            "completion_contract FROM tasks WHERE id = ?", (task,)).fetchone()
        other_workspaces = [other["workspace_path"] for other in conn.execute(
            "SELECT workspace_path FROM tasks WHERE id != ? "
            "AND status NOT IN ('done', 'archived') AND workspace_path IS NOT NULL", (task,))]
    finally:
        conn.close()
    if row is None:
        raise RouteRefused(f"{task}: not found")
    if row["status"] != expect:
        raise RouteRefused(f"{task}: status {row['status']!r}, expected {expect!r}")
    declared = item.get("completion_contract")
    if declared is not None and declared != row["completion_contract"]:
        raise RouteRefused(f"{task}: plan declares completion_contract {declared!r}, "
                           f"card has {row['completion_contract']!r}")
    live = {k: row[k] for k in ("current_run_id", "worker_pid", "claim_lock") if row[k]}
    if live:
        raise RouteRefused(f"{task}: live claim/run {live}")
    reasons = shared_workspace_reasons(row["workspace_path"], default_workdir, other_workspaces)
    procs, ignored = task_processes(task, row["workspace_path"], exclude, bool(reasons))
    if reasons:
        print(f"note {task}: shared workspace ({', '.join(reasons)}); "
              f"ignored {ignored} cwd-only processes")
    if procs:
        details = ", ".join(f"{pid} ({reason})" for pid, reason in procs)
        raise RouteRefused(f"{task}: task-owned processes [{details}]")
    return dict(row)


@contextlib.contextmanager
def dispatch_lock(db: Path):
    """Hold the board's dispatcher flock; the dispatcher skips its tick meanwhile."""
    path = db.with_name(db.name + ".dispatch.lock")
    with path.open("a+b") as handle:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RouteRefused("dispatch lock busy for 30s") from None
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def loopback_dashboard():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [HERMES, "dashboard", "--host", "127.0.0.1", "--port", str(port),
         "--skip-build", "--isolated", "--no-open"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + DASHBOARD_START_SECONDS
        token = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
                match = TOKEN_RE.search(html)
                if match:
                    token = json.loads(match.group(1))
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.5)
        if not token:
            raise RuntimeError(f"temporary dashboard on {base} never served a session token "
                               f"(exit={proc.poll()})")
        yield base, token
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def patch(base: str, token: str, board: str, task: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{base}/api/plugins/kanban/tasks/{task}?board={board}",
        data=json.dumps(payload).encode(), method="PATCH",
        headers={"Content-Type": "application/json", "X-Hermes-Session-Token": token})
    try:
        return json.loads(urllib.request.urlopen(request, timeout=60).read())
    except urllib.error.HTTPError as exc:
        raise RouteRefused(f"{task}: PATCH {payload.get('status')} -> HTTP {exc.code} "
                           f"{exc.read().decode(errors='replace')[:300]}") from None


def steps_for(item: dict) -> list[dict]:
    to = item.get("to")
    summary = item.get("summary")
    if to == "ready":
        step = {"status": "ready", "summary": summary}
        if item.get("assignee"):
            step["assignee"] = item["assignee"]
        return [step]
    if to == "review":
        if not item.get("assignee"):
            raise RouteRefused(f"{item['task']}: review route needs assignee")
        local_only = item.get("completion_contract") == "local-only"
        if local_only and item.get("published_pr"):
            raise RouteRefused(f"{item['task']}: local-only review must not declare published_pr")
        if not local_only and not item.get("published_pr"):
            raise RouteRefused(f"{item['task']}: PR-backed review route needs published_pr")
        implementer = item.get("implementer")
        if implementer and implementer == item["assignee"]:
            raise RouteRefused(f"{item['task']}: implementer and reviewer must differ")
        # request_review accepts only ready/running, so the pair runs under the lock.
        # It records the card's assignee as implementer, so restore the implementer
        # first; otherwise a later request-changes has no profile to route back to.
        ready = {"status": "ready"}
        if implementer:
            ready["assignee"] = implementer
        review = {"status": "review", "assignee": item["assignee"], "summary": summary}
        if not local_only:
            review["metadata"] = {"published_pr": item["published_pr"]}
        return [ready, review]
    raise RouteRefused(f"{item['task']}: unsupported target {to!r}")


def route(db: Path, board: str, base: str, token: str, item: dict, author: str,
          exclude: set[int], default_workdir: str | None = None) -> str:
    task = item["task"]
    if not str(item.get("comment") or "").strip():
        raise RouteRefused(f"{task}: comment is required")
    steps = steps_for(item)
    with dispatch_lock(db):
        preflight(db, item, exclude, default_workdir)
        for step in steps:
            patch(base, token, board, task, step)
    card = show(board, task)
    status = card["task"]["status"]
    if status != item["to"]:
        raise RouteRefused(f"{task}: readback status {status!r}, wanted {item['to']!r}; "
                           "no comment posted")
    cli("kanban", "--board", board, "comment", task, item["comment"], "--author", author)
    return f"{task}: {item.get('expect', 'triage')} -> {status} (assignee {card['task']['assignee']})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", required=True)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--author", default=os.environ.get("HERMES_PROFILE") or "operator")
    parser.add_argument("--dry-run", action="store_true",
                        help="preflight every card; start no dashboard and change nothing")
    args = parser.parse_args()

    items = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        print("plan must be a non-empty JSON list", file=sys.stderr)
        return 2
    try:
        record = board_record(args.board)
        db = Path(record["db_path"])
        default_workdir = record.get("default_workdir")
    except RuntimeError as exc:
        print(f"SETUP FAILED: {exc}", file=sys.stderr)
        return 2
    exclude = ancestry()

    if args.dry_run:
        failed = 0
        for item in items:
            try:
                steps_for(item)
                preflight(db, item, exclude, default_workdir)
                print(f"OK {item['task']}: preflight passed for -> {item['to']}")
            except RouteRefused as exc:
                failed += 1
                print(f"REFUSED {exc}")
        return 1 if failed else 0

    failed = 0
    try:
        with loopback_dashboard() as (base, token):
            for item in items:
                try:
                    print("ROUTED " + route(db, args.board, base, token, item, args.author, exclude,
                                           default_workdir))
                except RouteRefused as exc:
                    failed += 1
                    print(f"REFUSED {exc}")
    except RuntimeError as exc:
        print(f"SETUP FAILED: {exc}", file=sys.stderr)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
