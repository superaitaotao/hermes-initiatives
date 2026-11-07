"""Launch records and session links for sessions started from the initiatives page.

A launch is written before ``session.create``. Its state moves forward only:

    pending -> created_unlinked -> linked -> submitted
    pending -> create_failed            (retry allowed)

A launch is about the whole initiative (``discuss``), one cycle or one Kanban card (``cycle``/``card``, with
``subject`` = the cycle or card id), the ideas inbox (``triage``), or a card that waits on the user (``attend``,
``subject`` = the card id; started from the Review tab's "Waiting on you" list), or one Review item (``blocker``,
``subject`` = its short id; started from the item in the Review tab), or one task (``task``, ``subject`` = the task
or legacy bug id; started from the Tasks tab), or one legacy bug (``bug``, ``subject`` = the bug id; retained so
existing session history stays visible and retryable).
    linked  -> prompt_failed -> submitted (retry the prompt)

Session ids belong to one Hermes host and profile, so this store lives in the plugin data directory on
that host, never in the synced vault. Removing a launch never touches the session itself.
"""

from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .settings import data_dir

STATES = ("pending", "created_unlinked", "linked", "prompt_failed", "submitted", "create_failed")
KINDS = ("discuss", "cycle", "card", "triage", "attend", "blocker", "bug", "task")
SUBJECT_KINDS = {"cycle", "card"}
# A card that waits on the user (Review "Waiting on you"); the card need not belong to an initiative.
CARD_SUBJECT_KINDS = SUBJECT_KINDS | {"attend"}
# Kinds whose launch is about one thing named by ``subject``: a cycle, a card, a Review item (``blocker``), or a bug.
SUBJECTED_KINDS = CARD_SUBJECT_KINDS | {"blocker", "bug", "task"}
TRANSITIONS = {
    "pending": {"created_unlinked", "linked", "create_failed"},
    "create_failed": {"pending", "created_unlinked", "linked"},
    "created_unlinked": {"linked", "create_failed"},
    "linked": {"submitted", "prompt_failed"},
    "prompt_failed": {"submitted", "prompt_failed"},
    "submitted": set(),
}
FIELDS = (
    "id",
    "project",
    "initiative",
    "kind",
    "subject",
    "connection_id",
    "profile",
    "target_profile",
    "title",
    "state",
    "stored_session_id",
    "runtime_session_id",
    "error",
    "created_at",
    "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS launches (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  initiative TEXT,
  kind TEXT NOT NULL,
  subject TEXT,
  connection_id TEXT NOT NULL,
  profile TEXT NOT NULL,
  target_profile TEXT NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  stored_session_id TEXT,
  runtime_session_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS launches_project ON launches(project, created_at);
CREATE INDEX IF NOT EXISTS launches_initiative ON launches(initiative, created_at);
"""


class LaunchError(ValueError):
    """A launch request or transition that the state machine refuses."""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class LaunchStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "launches.db"
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(launches)")}
            if "subject" not in columns:  # stores created before cycle and card launches
                conn.execute("ALTER TABLE launches ADD COLUMN subject TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create(
        self,
        *,
        project: str,
        initiative: str | None,
        kind: str,
        subject: str | None = None,
        connection_id: str,
        profile: str,
        target_profile: str,
        title_prefix: str,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise LaunchError(f"kind must be one of {', '.join(KINDS)}")
        if (kind in SUBJECTED_KINDS) != bool(subject):
            raise LaunchError("cycle, card, attend, blocker, and bug launches need a subject; other kinds take none")
        for name, value in (("connection_id", connection_id), ("profile", profile), ("target_profile", target_profile)):
            if not value or len(value) > 200:
                raise LaunchError(f"{name} is required (max 200 characters)")
        launch_id = "L" + secrets.token_hex(4)
        title = f"{title_prefix.strip()[:120]} · {launch_id}"
        now = _now()
        row = {
            "id": launch_id,
            "project": project,
            "initiative": initiative,
            "kind": kind,
            "subject": subject,
            "connection_id": connection_id,
            "profile": profile,
            "target_profile": target_profile,
            "title": title,
            "state": "pending",
            "stored_session_id": None,
            "runtime_session_id": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO launches ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})", [row[f] for f in FIELDS]
            )
        return row

    def get(self, launch_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM launches WHERE id = ?", (launch_id,)).fetchone()
        return dict(row) if row else None

    def transition(
        self,
        launch_id: str,
        state: str,
        *,
        stored_session_id: str | None = None,
        runtime_session_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if state not in STATES:
            raise LaunchError(f"state must be one of {', '.join(STATES)}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM launches WHERE id = ?", (launch_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(launch_id)
            current = dict(row)
            if state not in TRANSITIONS[current["state"]]:
                conn.execute("ROLLBACK")
                raise LaunchError(f"cannot move launch from {current['state']} to {state}")
            stored = stored_session_id or current["stored_session_id"]
            if state in ("linked", "submitted", "prompt_failed") and not stored:
                conn.execute("ROLLBACK")
                raise LaunchError(f"state {state} needs stored_session_id")
            if current["stored_session_id"] and stored_session_id and stored_session_id != current["stored_session_id"]:
                conn.execute("ROLLBACK")
                raise LaunchError("a launch cannot change its stored session id")
            conn.execute(
                "UPDATE launches SET state = ?, stored_session_id = ?, runtime_session_id = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    state,
                    stored,
                    runtime_session_id or current["runtime_session_id"],
                    (error or "")[:500] or None,
                    _now(),
                    launch_id,
                ),
            )
            conn.execute("COMMIT")
        return self.get(launch_id) or {}

    def delete(self, launch_id: str) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM launches WHERE id = ?", (launch_id,)).rowcount > 0

    def list(
        self,
        *,
        project: str,
        initiative: str | None = None,
        kind: str | None = None,
        subject: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM launches WHERE project = ?"
        params: list[Any] = [project]
        for column, value in (("initiative", initiative), ("kind", kind), ("subject", subject)):
            if value:
                query += f" AND {column} = ?"
                params.append(value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
