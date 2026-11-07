#!/usr/bin/env python3
"""Read-only Kanban board status digest.

First read of an operator status report: prints one block per non-terminal card
with status, assignee, priority, block kind, breaker counters, the newest
lifecycle event and its waiting reason, notify-subscription coverage, and a
reminder to re-derive live external state before reporting.

The board database is opened read-only (``file:...?mode=ro``) so a live
dispatcher is never blocked. The path is resolved from
``hermes kanban boards list --json`` rather than guessed: a Hermes home can hold
a decoy ``kanban.db`` that is not the board.

Usage:
    python3 kanban_board_status.py [--board <slug>]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

LIFECYCLE_KINDS = (
    "blocked", "scheduled", "block_loop_detected", "unblocked", "promoted",
    "specified", "protocol_violation", "gave_up", "crashed", "completed",
)
INTERESTING_FIELDS = (
    "block_kind", "block_recurrences", "consecutive_failures", "max_retries",
    "current_run_id", "worker_pid", "last_heartbeat_at", "goal_mode",
    "completion_contract",
)


def resolve_db(board: str | None) -> Path:
    """Resolve the physical board database; never assume a home-level kanban.db."""
    try:
        out = subprocess.run(
            ["hermes", "kanban", "boards", "list", "--json"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        rows = json.loads(out)
        if not board:
            # Without an explicit slug the CLI's *current* board is the board,
            # not whichever row happens to sort first: a Hermes home can hold a
            # stale/empty `default` board whose db_path would otherwise win.
            for row in rows:
                if row.get("is_current") and row.get("db_path"):
                    return Path(row["db_path"])
        for row in rows:
            if board and row.get("slug") != board:
                continue
            if row.get("db_path"):
                return Path(row["db_path"])
    except Exception as exc:  # noqa: BLE001 - fall through to the conventional path
        print(f"# boards probe failed ({exc}); trying conventional paths", file=sys.stderr)
    slug = board or "default"
    for candidate in (
        Path.home() / ".hermes" / "kanban" / "boards" / slug / "kanban.db",
        Path.home() / ".hermes" / "kanban.db",
    ):
        if candidate.exists():
            return candidate
    return Path.home() / ".hermes" / "kanban" / "boards" / slug / "kanban.db"


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def stamp(ts: object) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "?"


def flat(text: object, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", help="board slug (default: the CLI's current board)")
    args = ap.parse_args()

    db = resolve_db(args.board)
    print(f"board db: {db}")
    if not db.exists():
        print("MISSING — resolve the board with `hermes kanban boards list --json`")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    task_cols = columns(conn, "tasks")
    event_cols = columns(conn, "task_events")

    counts = dict(conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"))
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    subscribed = {r[0] for r in conn.execute("SELECT DISTINCT task_id FROM kanban_notify_subs")}

    rows = conn.execute(
        "SELECT * FROM tasks WHERE status NOT IN ('done','archived') "
        "ORDER BY priority DESC, created_at"
    ).fetchall()
    print(f"non-terminal cards: {len(rows)}")

    for row in rows:
        task = dict(row)
        task_id = task["id"]
        print(f"\n=== {task_id}  [{task.get('status')}]  prio={task.get('priority')}  "
              f"@{task.get('assignee')}")
        print(f"    {flat(task.get('title'), 140)}")

        bits = []
        for key in INTERESTING_FIELDS:
            if key in task_cols and task.get(key) not in (None, "", 0):
                bits.append(f"{key}={flat(task[key], 80)}")
        if bits:
            print("    " + "  ".join(bits))
        if task.get("last_failure_error"):
            print("    last_failure_error: " + flat(task["last_failure_error"], 300))
        print(f"    notify_sub: {'yes' if task_id in subscribed else 'NO — this card is silent'}")

        if "kind" in event_cols:
            placeholders = ",".join("?" * len(LIFECYCLE_KINDS))
            query = (
                "SELECT kind, payload, created_at FROM task_events WHERE task_id=? "
                f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 3"
            )
            for event in conn.execute(query, (task_id, *LIFECYCLE_KINDS)):
                reason = ""
                try:
                    payload = json.loads(event["payload"] or "{}")
                    reason = payload.get("reason") or payload.get("error") or ""
                except (TypeError, ValueError):
                    reason = event["payload"] or ""
                print(f"      {stamp(event['created_at'])} {event['kind']}: {flat(reason, 220)}")

    print("\n# Next: re-derive live external state before reporting — target-branch head,")
    print("# newest run conclusion for that head, open PRs, and whether each card's recorded")
    print("# SHA is still the head (git merge-base --is-ancestor <sha> origin/<branch>).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
