"""Daily review store (``<plugin data>/reviews.db``): one queue for every decision that needs the user.

An item is one decision, keyed by a stable ``key`` (for example ``kanban:my-board:t_a7aa74e8:gate``) and shown
by a permanent short id (``D17``). While an item is active, a second upsert with the same key updates its basis
and keeps its short id. Each generation adds a version (question, options, recommendation); an answer records the
version it answered, so the log keeps the options exactly as shown.

Status:

    open -> answered -> (awaiting_confirm) -> applying -> applied | failed | stale -> open
    open -> deferred -> open
    open -> watching -> open (the generator judged that no human input is needed; the item returns when its basis changes)
    open | watching | deferred -> auto (the user handed this one item to the agents) -> settled | applied | open
       (the recovery tick applies an option or resolves it under ``auto_delegated``, or returns it; ``manual`` takes it back)
    open -> settled (an agent applied a rule the user delegated; ``review_settle`` checks the rule before any write)
    settled + overrule (the user said the settlement was wrong; its rule is suspended until he restores it)
    answered -> ready_for_owner -> acked | not_acked
    any active -> resolved_elsewhere | dismissed | closed

The first answer wins: the status moves from open (or deferred) to answered in one conditional update.
Answers, actions, and deferrals are append-only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .settings import data_dir

# Action -> needs a second confirmation (irreversible). review_exec implements each one except pr_merge*.
ACTIONS: dict[str, bool] = {
    "record_decision": False,
    "set_cycle_fields": False,
    "idea_status": False,
    "idea_reject": True,
    "kanban_comment": False,
    "kanban_unblock": False,
    "kanban_create": False,
    "kanban_archive": True,
    "pr_merge": True,
    "pr_merge_waive_review": True,
    "handoff": False,
    "attend": False,
    "defer": False,
    "acknowledge": False,
    "rule_state": False,
}
SECTIONS = ("gated", "suggested")
ACTIVE = ("open", "deferred", "watching", "auto", "answered", "awaiting_confirm", "applying", "ready_for_owner", "failed")
TERMINAL = ("applied", "settled", "resolved_elsewhere", "acked", "not_acked", "dismissed", "closed", "stale")
ANSWERABLE = ("open", "deferred", "auto")  # the user can still answer an item he handed to the agents
DEFER_KINDS = ("date", "state_change", "indefinite")
SURFACES = ("tab", "telegram", "agent")  # agent: an option applied under the user's auto delegation
AUTO_FROM = ("open", "watching", "deferred")
RULE_STATES = ("active", "suspended")
# ``defer_behind_focus_cycle`` (review_settle): the one rule that parks instead of settling, so its overrule returns
# the item rather than leaving it settled. Kept here as a literal: importing review_settle from reviews would cycle.
RULE_PARK = "defer_behind_focus_cycle"
OPTION_ID = re.compile(r"^[A-D]$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS items (
  short_id TEXT PRIMARY KEY,
  key TEXT NOT NULL,
  section TEXT NOT NULL,
  source TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  project TEXT, initiative TEXT, cycle TEXT,
  owner_profile TEXT, owner_job TEXT,
  deadline TEXT, urgent_rule TEXT,
  basis_json TEXT NOT NULL, basis_hash TEXT NOT NULL,
  blocks_json TEXT NOT NULL DEFAULT '[]',
  links_json TEXT NOT NULL DEFAULT '[]',
  evidence_hash TEXT,
  context TEXT NOT NULL DEFAULT '',
  watch_reason TEXT, watch_hash TEXT,
  opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  closed_note TEXT
);
CREATE INDEX IF NOT EXISTS items_key ON items(key, status);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE TABLE IF NOT EXISTS item_versions (
  short_id TEXT NOT NULL, n INTEGER NOT NULL,
  question TEXT NOT NULL, options_json TEXT NOT NULL, recommend TEXT NOT NULL, context TEXT NOT NULL,
  basis_hash TEXT NOT NULL DEFAULT '', review_id TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (short_id, n)
);
CREATE TABLE IF NOT EXISTS answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  short_id TEXT NOT NULL, version INTEGER NOT NULL, option TEXT NOT NULL, note TEXT NOT NULL,
  surface TEXT NOT NULL, telegram_msg_id TEXT, answered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  answer_id INTEGER, short_id TEXT NOT NULL, action TEXT NOT NULL, args_json TEXT NOT NULL,
  state TEXT NOT NULL, receipt TEXT, error TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deferrals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  short_id TEXT NOT NULL, kind TEXT NOT NULL, until TEXT, reason TEXT NOT NULL,
  basis_hash TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dismissals (key TEXT NOT NULL, evidence_hash TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (key, evidence_hash));
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, window_start TEXT, completed_at TEXT,
  trigger TEXT NOT NULL, model TEXT, digest TEXT, summaries_json TEXT NOT NULL DEFAULT '{}',
  gaps_json TEXT NOT NULL DEFAULT '[]', telegram_json TEXT NOT NULL DEFAULT '[]',
  facts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS review_items (review_id TEXT NOT NULL, short_id TEXT NOT NULL, PRIMARY KEY (review_id, short_id));
CREATE TABLE IF NOT EXISTS urgent_pushes (short_id TEXT NOT NULL, basis_hash TEXT NOT NULL, rule TEXT NOT NULL,
  at TEXT NOT NULL, telegram_msg_id TEXT, PRIMARY KEY (short_id, basis_hash));
CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, key TEXT NOT NULL,
  payload_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_states (id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, state TEXT NOT NULL,
  reason TEXT NOT NULL, short_id TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_deferral_ops (
  operation TEXT PRIMARY KEY, short_id TEXT NOT NULL, state TEXT NOT NULL, data_json TEXT NOT NULL,
  receipt_json TEXT, error TEXT, at TEXT NOT NULL);
"""


class ReviewError(ValueError):
    """A request the review store refuses; nothing was written."""


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


SUMMARY_LIMITS = {"what": 400, "why": 700}


def validate_summary(summary: Any) -> dict[str, str]:
    """A blocker summary: ``what`` is blocked (and what waits on it), ``why`` (the actual cause), in plain words."""
    if not isinstance(summary, dict):
        raise ReviewError("summary must be an object with what and why")
    clean = {}
    for key, limit in SUMMARY_LIMITS.items():
        text = " ".join(str(summary.get(key) or "").split())
        if len(text) < 15:
            raise ReviewError(f"summary.{key} is required: one or two plain sentences")
        if len(text) > limit:
            raise ReviewError(f"summary.{key} is {len(text)} characters; keep it under {limit}")
        clean[key] = text
    return clean


def current(item: dict[str, Any]) -> bool:
    """Answerable now: the item has options, written for its source as it is now."""
    version = item.get("version")
    return bool(version) and version["basis_hash"] == item["basis_hash"]


def _push_key(version: dict[str, Any]) -> str:
    """A version is pushed once; a new version (changed basis or rewritten options) is pushed again."""
    return f"{version['basis_hash']}:v{version['n']}"


def basis_hash(basis: Any) -> str:
    text = json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _receipt_deferral_id(row: sqlite3.Row | None, column: str) -> int | None:
    """The deferral row an agent park recorded in its receipt, or None when it cannot be read."""
    if row is None:
        return None
    try:
        receipt = json.loads(row[column] or "{}")
    except ValueError:
        return None
    value = receipt.get("deferral_id") if isinstance(receipt, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _newest_deferral_id(conn: sqlite3.Connection, short_id: str) -> int | None:
    """The id of the item's newest deferral row: the row that says whose wait holds the item now."""
    row = conn.execute("SELECT id FROM deferrals WHERE short_id = ? ORDER BY id DESC LIMIT 1", (short_id,)).fetchone()
    return int(row[0]) if row else None


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def validate_options(options: list[dict[str, Any]], recommend: str) -> list[dict[str, Any]]:
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise ReviewError("an item needs 2 to 4 options")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise ReviewError("each option must be an object")
        option_id = str(option.get("id", "")).strip().upper()
        label = str(option.get("label", "")).strip()
        action = str(option.get("action", "")).strip()
        if not OPTION_ID.fullmatch(option_id) or option_id in seen:
            raise ReviewError(f"option ids must be unique letters A-D, got '{option_id}'")
        if not label:
            raise ReviewError(f"option {option_id} needs a label")
        if action not in ACTIONS:
            raise ReviewError(f"option {option_id}: unknown action '{action}'")
        args = option.get("args") or {}
        if not isinstance(args, dict):
            raise ReviewError(f"option {option_id}: args must be an object")
        seen.add(option_id)
        clean.append({"id": option_id, "label": label[:300], "action": action, "args": args, "confirm": ACTIONS[action]})
    if str(recommend).strip().upper() not in seen:
        raise ReviewError(f"recommend must name one of the options ({', '.join(sorted(seen))})")
    return clean


def _project_sql(project: str, exclude_initiatives: Iterable[str]) -> tuple[str, list[Any]]:
    """SQL for one project's rows: its own, plus legacy rows that name no other project's initiative.

    Rows written before the ``project`` column existed (NULL or empty) belong to the root that defines the
    initiative they name. One that names no configured initiative anywhere stays visible to every project, so
    nothing disappears from the page.
    """
    clause = "(project = ? OR COALESCE(project, '') = '')"
    params: list[Any] = [project]
    excluded = sorted({str(i) for i in exclude_initiatives if i})
    if excluded:
        clause += (
            f" AND (COALESCE(project, '') != '' OR COALESCE(initiative, '') = '' "
            f"OR initiative NOT IN ({','.join('?' * len(excluded))}))"
        )
        params.extend(excluded)
    return clause, params


class ReviewStore:
    def __init__(self, path: Path | None = None, *, read_only: bool = False) -> None:
        """``read_only`` opens an existing store without creating, migrating or writing it (dry runs)."""
        self.path = path or data_dir() / "reviews.db"
        self.read_only = read_only
        self._lock = threading.Lock()
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"review store {self.path} does not exist")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(item_versions)")}
            if "summary_json" not in columns:  # stores created before blocker summaries
                conn.execute("ALTER TABLE item_versions ADD COLUMN summary_json TEXT NOT NULL DEFAULT '{}'")
            if "refreshed" not in columns:  # stores created before the stale sweep (2026-11-01)
                conn.execute("ALTER TABLE item_versions ADD COLUMN refreshed INTEGER NOT NULL DEFAULT 0")

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000")
            return conn
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    # --- items -------------------------------------------------------------------------------------------

    def _next_short_id(self, conn: sqlite3.Connection) -> str:
        conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('item', 0)")
        conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'item'")
        value = conn.execute("SELECT value FROM counters WHERE name = 'item'").fetchone()[0]
        return f"D{value}"

    def _active_by_key(self, conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        marks = ",".join("?" * len(ACTIVE))
        return conn.execute(
            f"SELECT * FROM items WHERE key = ? AND status IN ({marks}) ORDER BY opened_at DESC LIMIT 1", (key, *ACTIVE)
        ).fetchone()

    def upsert_item(
        self,
        key: str,
        *,
        section: str,
        source: str,
        title: str,
        basis: Any,
        blocks: list[str] | None = None,
        links: list[dict[str, Any]] | None = None,
        opened_at: str | None = None,
        project: str | None = None,
        initiative: str | None = None,
        cycle: str | None = None,
        owner_profile: str | None = None,
        owner_job: str | None = None,
        deadline: str | None = None,
        urgent_rule: str | None = None,
        evidence_hash: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create the item for *key*, or update the active one. Returns ``(item, created)``."""
        key = str(key or "").strip()
        if not key or len(key) > 300:
            raise ReviewError("key is required (max 300 characters)")
        if section not in SECTIONS:
            raise ReviewError(f"section must be one of {', '.join(SECTIONS)}")
        if not str(title or "").strip():
            raise ReviewError("title is required")
        now = now_iso()
        values = {
            "section": section,
            "source": str(source)[:200],
            "title": str(title).strip()[:300],
            "project": project,
            "initiative": initiative,
            "cycle": cycle,
            "owner_profile": owner_profile,
            "owner_job": owner_job,
            "deadline": deadline,
            "urgent_rule": urgent_rule,
            "basis_json": json.dumps(basis, sort_keys=True, default=str),
            "basis_hash": basis_hash(basis),
            "blocks_json": json.dumps(sorted(set(blocks or []))),
            "links_json": json.dumps(links or []),
            "evidence_hash": evidence_hash,
        }
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._active_by_key(conn, key)
            if (
                row is not None
                and row["status"] == "applying"
                and conn.execute(
                    "SELECT 1 FROM agent_deferral_ops WHERE short_id = ? AND state != 'completed'", (row["short_id"],)
                ).fetchone()
            ):
                conn.execute("COMMIT")
                return self._item(conn, row), False
            if row is not None:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE items SET {sets}, updated_at = ? WHERE short_id = ?", (*values.values(), now, row["short_id"])
                )
                short_id, created = row["short_id"], False
            else:
                short_id = self._next_short_id(conn)
                columns = ["short_id", "key", "status", "opened_at", "updated_at", *values]
                conn.execute(
                    f"INSERT INTO items ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    (short_id, key, "open", opened_at or now, now, *values.values()),
                )
                created = True
            conn.execute("COMMIT")
        return self.get(short_id) or {}, created

    def _item(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item.pop("basis_json"))
        item["blocks"] = json.loads(item.pop("blocks_json"))
        item["links"] = json.loads(item.pop("links_json"))
        version = conn.execute(
            "SELECT * FROM item_versions WHERE short_id = ? ORDER BY n DESC LIMIT 1", (item["short_id"],)
        ).fetchone()
        item["version"] = self._version(version) if version else None
        item["refreshed"] = bool(item["version"] and item["version"].get("refreshed"))
        deferral = conn.execute(
            "SELECT kind, until, reason, at FROM deferrals WHERE short_id = ? ORDER BY id DESC LIMIT 1", (item["short_id"],)
        ).fetchone()
        item["deferral"] = dict(deferral) if deferral and item["status"] == "deferred" else None
        mark = (
            conn.execute(
                "SELECT receipt, at FROM actions WHERE short_id = ? AND action = 'mark_auto' ORDER BY id DESC LIMIT 1",
                (item["short_id"],),
            ).fetchone()
            if item["status"] == "auto"
            else None
        )
        item["auto"] = {"note": mark["receipt"] or "", "at": mark["at"]} if mark else None
        return item

    @staticmethod
    def _version(row: sqlite3.Row) -> dict[str, Any]:
        version = dict(row)
        version["options"] = json.loads(version.pop("options_json"))
        version["summary"] = json.loads(version.pop("summary_json", None) or "{}") or None
        return version

    def get(self, short_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE short_id = ?", (str(short_id).upper(),)).fetchone()
            return self._item(conn, row) if row else None

    def get_by_key(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = self._active_by_key(conn, key)
            return self._item(conn, row) if row else None

    def items(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        section: str | None = None,
        project: str | None = None,
        exclude_initiatives: Iterable[str] = (),
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Items newest first, filtered in SQL so a caller's own limit never fills up with another project's rows.

        ``project`` keeps that project's rows plus legacy rows no other project's initiative claims (see
        ``_project_sql``). ``section`` is applied here rather than by the caller for the same reason.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if section:
            clauses.append("section = ?")
            params.append(section)
        if project is not None:
            clause, extra = _project_sql(project, exclude_initiatives)
            clauses.append(clause)
            params.extend(extra)
        query = "SELECT * FROM items" + (f" WHERE {' AND '.join(clauses)}" if clauses else "")
        query += " ORDER BY opened_at DESC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        with self._connect() as conn:
            return [self._item(conn, r) for r in conn.execute(query, params).fetchall()]

    def set_status(self, short_id: str, status: str, *, expect: tuple[str, ...] | None = None, note: str | None = None) -> bool:
        if status not in ACTIVE + TERMINAL:
            raise ReviewError(f"unknown status '{status}'")
        query = "UPDATE items SET status = ?, updated_at = ?, closed_note = COALESCE(?, closed_note) WHERE short_id = ?"
        params: list[Any] = [status, now_iso(), note, str(short_id).upper()]
        if expect:
            query += f" AND status IN ({','.join('?' * len(expect))})"
            params.extend(expect)
        with self._lock, self._connect() as conn:
            return conn.execute(query, params).rowcount > 0

    def close_item(self, short_id: str, status: str, *, note: str = "") -> bool:
        if status not in TERMINAL:
            raise ReviewError(f"close needs a terminal status, got '{status}'")
        return self.set_status(short_id, status, expect=ACTIVE, note=note or None)

    def set_context(self, short_id: str, context: str) -> None:
        """Source facts for the generator (card reason, cycle state). Not shown as the question."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE items SET context = ? WHERE short_id = ? AND status != 'applying'",
                ((context or "")[:6000], str(short_id).upper()),
            )

    def reopen(self, short_id: str) -> bool:
        return self.set_status(short_id, "open", expect=("deferred", "stale", "failed", "watching"))

    def watch(self, short_id: str, reason: str) -> bool:
        """Open -> watching: no human input is needed now. The item returns when its basis changes."""
        reason = (reason or "").strip()
        if not reason:
            raise ReviewError(f"{short_id}: a watch needs a reason")
        with self._lock, self._connect() as conn:
            return (
                conn.execute(
                    "UPDATE items SET status = 'watching', watch_reason = ?, watch_hash = basis_hash, updated_at = ? "
                    "WHERE short_id = ? AND status IN ('open', 'watching')",
                    (reason[:1000], now_iso(), str(short_id).upper()),
                ).rowcount
                > 0
            )

    def mark_auto(self, short_id: str, note: str = "", *, surface: str) -> dict[str, Any]:
        """The user hands one item to the agents: open, watching, or deferred -> auto. The note is his instruction.

        The recovery tick then applies one of its options, resolves it, or returns it (``review_auto``). Rule-learning
        items and urgent consent items stay with him."""
        if surface not in SURFACES or surface == "agent":
            raise ReviewError("only the user marks an item auto (surface tab or telegram)")
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        if item["source"] == "learn":
            raise ReviewError(f"{item['short_id']} changes how agents decide; it stays with you")
        if item.get("urgent_rule"):
            raise ReviewError(f"{item['short_id']} carries urgent rule {item['urgent_rule']}; it stays with you")
        note = (note or "").strip()
        if not self.set_status(item["short_id"], "auto", expect=AUTO_FROM):
            raise ReviewError(f"{item['short_id']} is {item['status']}; only open, watching, or deferred items can be auto")
        self.log_action(
            None, item["short_id"], "mark_auto", {"surface": surface, "from": item["status"]}, "auto", receipt=note[:2000]
        )
        return self.get(item["short_id"]) or {}

    def last_auto_mark(self, short_id: str) -> dict[str, Any] | None:
        """The user's latest auto mark on *short_id*: ``{note, at, surface}``, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT receipt, args_json, at FROM actions WHERE short_id = ? AND action = 'mark_auto' ORDER BY id DESC LIMIT 1",
                (str(short_id).upper(),),
            ).fetchone()
        if row is None:
            return None
        return {"note": row["receipt"] or "", "at": row["at"], "surface": json.loads(row["args_json"] or "{}").get("surface")}

    def unmark_auto(self, short_id: str, *, surface: str) -> dict[str, Any]:
        """The user takes an auto item back: auto -> open. Its options come back as they were."""
        if surface not in SURFACES or surface == "agent":
            raise ReviewError("only the user takes an item back (surface tab or telegram)")
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        if not self.set_status(item["short_id"], "open", expect=("auto",)):
            raise ReviewError(f"{item['short_id']} is {item['status']}, not auto")
        self.log_action(None, item["short_id"], "mark_manual", {"surface": surface}, "open")
        return self.get(item["short_id"]) or {}

    def settle(
        self, short_id: str, rule: str, receipt: str, *, expect: tuple[str, ...] = ("open",), args: dict | None = None
    ) -> bool:
        """Open -> settled: an agent applied a delegated rule. Logs the rule and receipt; no answer row is written.

        A collected gate with the same basis is dismissed, so it does not return at the next sync."""
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        if not self.set_status(item["short_id"], "settled", expect=expect, note=f"{rule}: {receipt}"[:500]):
            return False
        self.log_action(None, item["short_id"], f"settle:{rule}", args or {}, "settled", receipt=receipt[:2000])
        self.dismiss(item["key"], evidence_hash=item["basis_hash"])
        return True

    def claim_under_rule(self, short_id: str, *, basis_hash: str, version: int | None) -> bool:
        """Open -> applying for a rule-owned write, only while the status, basis, and options still match.

        The claim is what makes a concurrent answer lose: an ``applying`` item is not answerable
        (``ANSWERABLE``), so a second writer cannot win between the rule's checks and its writes."""
        short_id = str(short_id).upper()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                won = conn.execute(
                    "UPDATE items SET status = 'applying', updated_at = ? WHERE short_id = ? AND status = 'open' "
                    "AND basis_hash = ? AND (SELECT COALESCE(MAX(n), 0) FROM item_versions WHERE short_id = ?) = ?",
                    (now_iso(), short_id, basis_hash, short_id, version),
                ).rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return won > 0

    def release_rule_claim(self, short_id: str, *, rule: str, error: str) -> bool:
        """Applying -> open after a rule-owned write failed, with one ``failed`` action carrying the error.

        A failure is visible: the item is back to open, so the next tick triages it again, and the failed action
        keeps the error for the user."""
        short_id = str(short_id).upper()
        if not self.set_status(short_id, "open", expect=("applying",)):
            return False
        self.log_action(None, short_id, f"settle:{rule}", {}, "failed", error=str(error)[:2000])
        return True

    def park_under_rule(
        self,
        short_id: str,
        *,
        rule: str,
        basis_hash: str,
        version: int | None,
        reason: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Applying -> deferred for a rule-owned park: one deferral and one settle action, in one transaction.

        The item is not settled and no answer is written: the rule only said "not now", and the deferral is what the
        Deferred group shows. ``receipt`` gains the ``deferral_id`` of the row it just inserted (Appendix F), so the
        return check can prove this park is the item's newest deferral."""
        short_id = str(short_id).upper()
        full: dict[str, Any] = {}
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status, basis_hash FROM items WHERE short_id = ?", (short_id,)).fetchone()
                if row is None or row["status"] != "applying" or row["basis_hash"] != basis_hash:
                    raise ReviewError(f"{short_id} is not applying under this basis; nothing was parked")
                current = conn.execute(
                    "SELECT COALESCE(MAX(n), 0) FROM item_versions WHERE short_id = ?", (short_id,)
                ).fetchone()[0]
                if current != version:
                    raise ReviewError(f"{short_id}'s options changed while parking")
                deferral_id = conn.execute(
                    "INSERT INTO deferrals (short_id, kind, until, reason, basis_hash, at) "
                    "VALUES (?, 'state_change', NULL, ?, ?, ?)",
                    (short_id, reason[:1000], basis_hash, now_iso()),
                ).lastrowid
                full = {**receipt, "deferral_id": deferral_id}
                conn.execute(
                    "INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
                    "VALUES (NULL, ?, ?, ?, 'deferred', ?, ?)",
                    (
                        short_id,
                        f"settle:{rule}",
                        json.dumps({"rule": rule, "basis_hash": basis_hash, "version": version}),
                        json.dumps(full),
                        now_iso(),
                    ),
                )
                conn.execute("UPDATE items SET status = 'deferred', updated_at = ? WHERE short_id = ?", (now_iso(), short_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return full

    def reopen_rule_deferral(
        self, short_id: str, *, rule: str, version: int | None, reason: str, deferral_id: int
    ) -> bool:
        """Deferred -> open because a rule's own condition stopped holding, while that rule's park still holds it.

        ``deferral_id`` is the park's own deferral row. Ownership is re-checked in the same transaction as the
        status change, so a deferral written in between (the user's own wait, or another rule's park) supersedes
        the park and is never returned by rule machinery. The next tick writes fresh options."""
        short_id = str(short_id).upper()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status FROM items WHERE short_id = ?", (short_id,)).fetchone()
                if row is None or row["status"] != "deferred" or _newest_deferral_id(conn, short_id) != deferral_id:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("UPDATE items SET status = 'open', updated_at = ? WHERE short_id = ?", (now_iso(), short_id))
                conn.execute(
                    "INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
                    "VALUES (NULL, ?, 'reopen_deferral', ?, 'reopened', ?, ?)",
                    (
                        short_id,
                        json.dumps({"version": version, "rule": rule, "deferral_id": deferral_id}),
                        reason[:2000],
                        now_iso(),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return True

    def latest_deferral_id(self, short_id: str) -> int | None:
        """The id of the item's newest deferral row: what tells whose park owns a deferred item."""
        with self._connect() as conn:
            return _newest_deferral_id(conn, str(short_id).upper())

    def rule_states(self) -> dict[str, dict[str, Any]]:
        """The latest state row per delegated rule (``active`` or ``suspended``). A rule with no row is active."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_states r WHERE id = (SELECT MAX(id) FROM rule_states WHERE rule = r.rule)"
            ).fetchall()
        return {r["rule"]: dict(r) for r in rows}

    def set_rule_state(self, rule: str, state: str, reason: str, *, short_id: str | None = None) -> bool:
        """Append a rule state row. False when the rule is already in that state (nothing written)."""
        if state not in RULE_STATES:
            raise ReviewError(f"rule state must be one of {', '.join(RULE_STATES)}")
        reason = (reason or "").strip()
        if not reason:
            raise ReviewError("a rule state change needs a reason")
        if (self.rule_states().get(rule) or {"state": "active"})["state"] == state:
            return False
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO rule_states (rule, state, reason, short_id, at) VALUES (?, ?, ?, ?, ?)",
                (rule, state, reason[:1000], short_id, now_iso()),
            )
        return True

    def overrule(self, short_id: str, reason: str) -> dict[str, Any]:
        """The user overrules what an agent did under a rule: that rule is suspended at once.

        A settlement stays settled and the files the rule wrote stay as they are. A rule-owned *park* is returned
        instead: the item goes back to ``open`` with a ``reopen_deferral`` action, because parking is the rule's own
        decision and the user's overrule is exactly "not now is wrong". The newest applicable rule operation is the
        one suspended; a later deferral that already replaced the park is left exactly as it is. ``review_learn.
        overrule`` files the follow-up decision (undo, restore the rule, or keep it suspended) in both cases."""
        reason = (reason or "").strip()
        if len(reason) < 10:
            raise ReviewError("say in a few words why the settlement was wrong")
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        settles = [a for a in self.actions(item["short_id"]) if a["action"].startswith("settle:")]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # The newest applicable rule operation decides which rule this overrule suspends. The item's newest
            # deferral is what holds it now, so a park that a later deferral superseded never hides the operation
            # that parked it, and a historical park is never chosen over the park that is current.
            park = conn.execute(
                "SELECT * FROM actions WHERE short_id = ? AND action = ? AND state = 'deferred' ORDER BY id DESC LIMIT 1",
                (item["short_id"], f"settle:{RULE_PARK}"),
            ).fetchone()
            op = conn.execute(
                "SELECT * FROM agent_deferral_ops WHERE short_id = ? AND state = 'completed' ORDER BY at DESC LIMIT 1",
                (item["short_id"],),
            ).fetchone()
            park_id = _receipt_deferral_id(park, "receipt")
            op_id = _receipt_deferral_id(op, "receipt_json")
            if park is not None and (op is None or (park_id or 0) >= (op_id or 0)):
                try:
                    done = self._overrule_rule_park(conn, item, park, reason)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return done
            if op is not None:
                previous = conn.execute(
                    "SELECT args_json FROM actions WHERE short_id = ? AND action = 'overrule'", (item["short_id"],)
                ).fetchall()
                if any(json.loads(r[0]).get("operation") == op["operation"] for r in previous):
                    conn.execute("ROLLBACK")
                    raise ReviewError(f"{item['short_id']} was already overruled")
                rule = "defer_exploring_cycle"
                args = {"rule": rule, "operation": op["operation"]}
                conn.execute(
                    "INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
                    "VALUES (NULL, ?, 'overrule', ?, 'overruled', ?, ?)",
                    (item["short_id"], json.dumps(args), reason[:2000], now_iso()),
                )
                conn.execute(
                    "INSERT INTO rule_states (rule, state, reason, short_id, at) VALUES (?, 'suspended', ?, ?, ?)",
                    (rule, f"{item['short_id']} overruled: {reason}", item["short_id"], now_iso()),
                )
                conn.execute("COMMIT")
                return {"short_id": item["short_id"], "rule": rule, "receipt": op["receipt_json"], "reason": reason}
            conn.execute("ROLLBACK")
        if item["status"] != "settled" or not settles:
            raise ReviewError(f"{item['short_id']} is {item['status']}; only agent-settled items can be overruled")
        if any(a["action"] == "overrule" for a in self.actions(item["short_id"])):
            raise ReviewError(f"{item['short_id']} was already overruled")
        rule = settles[-1]["action"].split(":", 1)[1]
        self.log_action(None, item["short_id"], "overrule", {"rule": rule}, "overruled", receipt=reason[:2000])
        self.set_rule_state(rule, "suspended", f"{item['short_id']} overruled: {reason}", short_id=item["short_id"])
        return {"short_id": item["short_id"], "rule": rule, "receipt": settles[-1].get("receipt") or "", "reason": reason}

    def _overrule_rule_park(
        self, conn: sqlite3.Connection, item: dict[str, Any], park: sqlite3.Row, reason: str
    ) -> dict[str, Any]:
        """Suspend the parking rule and, while its park still holds the item, return it. One overrule per park.

        Ownership is read inside the caller's transaction: the item returns only while the park's own deferral is
        still the item's newest deferral. When a later deferral replaced it (the user's own wait, or another rule's
        park), that newer wait is left exactly as it is: the overrule still suspends the rule and is recorded, but
        it does not move the item."""
        receipt = json.loads(park["receipt"] or "{}")
        deferral_id = receipt.get("deferral_id")
        short_id = item["short_id"]
        for row in conn.execute(
            "SELECT args_json FROM actions WHERE short_id = ? AND action = 'overrule'", (short_id,)
        ).fetchall():
            if json.loads(row[0] or "{}").get("deferral_id") == deferral_id:
                raise ReviewError(f"{short_id} was already overruled")
        row = conn.execute("SELECT status FROM items WHERE short_id = ?", (short_id,)).fetchone()
        status = row["status"] if row else None
        if status not in ("deferred", "open"):
            raise ReviewError(f"{short_id} is {status}; only a rule-owned park still in review can be overruled")
        holds = deferral_id is not None and _newest_deferral_id(conn, short_id) == deferral_id
        conn.execute(
            "INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
            "VALUES (NULL, ?, 'overrule', ?, 'overruled', ?, ?)",
            (short_id, json.dumps({"rule": RULE_PARK, "deferral_id": deferral_id}), reason[:2000], now_iso()),
        )
        conn.execute(
            "INSERT INTO rule_states (rule, state, reason, short_id, at) VALUES (?, 'suspended', ?, ?, ?)",
            (RULE_PARK, f"{short_id} overruled: {reason}", short_id, now_iso()),
        )
        returned = 0
        if holds and status == "deferred":
            returned = conn.execute(
                "UPDATE items SET status = 'open', updated_at = ? WHERE short_id = ? AND status = 'deferred'",
                (now_iso(), short_id),
            ).rowcount
        if returned:
            conn.execute(
                "INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
                "VALUES (NULL, ?, 'reopen_deferral', ?, 'reopened', ?, ?)",
                (
                    short_id,
                    json.dumps(
                        {
                            "rule": RULE_PARK,
                            "version": (item.get("version") or {}).get("n"),
                            "deferral_id": deferral_id,
                        }
                    ),
                    f"{RULE_PARK}: overruled by the user; the item returns to review",
                    now_iso(),
                ),
            )
        return {
            "short_id": short_id,
            "rule": RULE_PARK,
            "receipt": park["receipt"] or "",
            "reason": reason,
            "returned": bool(returned),
        }

    def answered_items(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """Items with at least one answer at or after *since* (any status), oldest answer first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.* FROM items i WHERE EXISTS (SELECT 1 FROM answers a WHERE a.short_id = i.short_id "
                "AND a.answered_at >= ?) ORDER BY i.opened_at",
                (since or "",),
            ).fetchall()
            return [self._item(conn, r) for r in rows]

    def has_key(self, key: str) -> bool:
        """True when any item, in any status, ever had *key*."""
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM items WHERE key = ? LIMIT 1", (key,)).fetchone() is not None

    def recent_settled(
        self, *, limit: int = 20, project: str | None = None, exclude_initiatives: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        """Items agents settled most recently (shown so the user can audit delegated decisions)."""
        clause = "status = 'settled'"
        params: list[Any] = []
        if project is not None:
            scoped, extra = _project_sql(project, exclude_initiatives)
            clause += f" AND {scoped}"
            params.extend(extra)
        params.append(max(1, min(limit, 200)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE {clause} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
            return [self._item(conn, r) for r in rows]

    def reopen_changed_watches(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT short_id FROM items WHERE status = 'watching' "
                "AND basis_hash != COALESCE(watch_hash, '') ORDER BY short_id"
            ).fetchall()
            ids = [r[0] for r in rows]
            conn.executemany("UPDATE items SET status = 'open', updated_at = ? WHERE short_id = ?", [(now_iso(), s) for s in ids])
        return ids

    def stale_items(self, *, now: dt.datetime | None = None, days: int = 3) -> list[dict[str, Any]]:
        """Gated items still with the user that nothing touched for *days*: no new options, answer, action, or
        deferral (review_sweep). Oldest first. ``last_touched`` is the newest of those, else ``opened_at``.

        ``updated_at`` is not used: every sync rewrites it, so it says nothing about whether anyone looked."""
        now = now or dt.datetime.now(dt.UTC)
        cutoff = (now - dt.timedelta(days=days)).replace(microsecond=0).isoformat()
        statuses = ("open", "watching", "deferred", "failed", "awaiting_confirm")
        marks = ",".join("?" * len(statuses))
        query = f"""
            SELECT i.short_id, MAX(i.opened_at,
                       COALESCE((SELECT MAX(created_at) FROM item_versions WHERE short_id = i.short_id), ''),
                       COALESCE((SELECT MAX(at) FROM actions WHERE short_id = i.short_id), ''),
                       COALESCE((SELECT MAX(answered_at) FROM answers WHERE short_id = i.short_id), ''),
                       COALESCE((SELECT MAX(at) FROM deferrals WHERE short_id = i.short_id), '')) AS touched
            FROM items i WHERE i.section = 'gated' AND i.status IN ({marks})
        """
        with self._connect() as conn:
            rows = [r for r in conn.execute(query, statuses).fetchall() if r["touched"] < cutoff]
            out = []
            for row in sorted(rows, key=lambda r: r["touched"]):
                item = self._item(conn, conn.execute("SELECT * FROM items WHERE short_id = ?", (row["short_id"],)).fetchone())
                if item["status"] == "deferred" and item["deferral"] and item["deferral"].get("kind") == "date":
                    if str(item["deferral"].get("until") or "") >= now.date().isoformat():
                        continue  # parked until a date that has not come yet: not idle, just waiting
                out.append({**item, "last_touched": row["touched"]})
        return out

    def reopen_watch(self, short_id: str, version: int | None, reason: str) -> bool:
        """Watching -> open because the watch's own ground stopped holding (its named owner cards finished).

        The reopen is logged like a met deferral (``reopen_deferral`` with the version it saw), so the next tick asks
        for fresh options even when the basis did not change."""
        if not self.set_status(short_id, "open", expect=("watching",)):
            return False
        self.log_action(
            None, short_id, "reopen_deferral", {"version": version, "from": "watch"}, "reopened", receipt=reason[:2000]
        )
        return True

    # --- versions and answers ----------------------------------------------------------------------------

    def add_version(
        self,
        short_id: str,
        *,
        question: str,
        options: list[dict[str, Any]],
        recommend: str,
        context: str,
        review_id: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(question or "").strip():
            raise ReviewError("question is required")
        clean = validate_options(options, recommend)
        summary_clean = validate_summary(summary) if summary is not None else {}
        short_id = str(short_id).upper()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT basis_hash, status FROM items WHERE short_id = ?", (short_id,)).fetchone()
            if row is not None and row["status"] == "applying":
                conn.execute("ROLLBACK")
                raise ReviewError(f"{short_id} is applying; options cannot change")
            if row is None:
                conn.execute("ROLLBACK")
                raise ReviewError(f"unknown item {short_id}")
            n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM item_versions WHERE short_id = ?", (short_id,)).fetchone()[0]
            # Options written because the stale sweep refreshed the version on show reach the user in the digest only.
            sweep = conn.execute(
                "SELECT args_json FROM actions WHERE short_id = ? AND action = 'sweep_refresh' ORDER BY id DESC LIMIT 1",
                (short_id,),
            ).fetchone()
            refreshed = int(bool(sweep) and json.loads(sweep[0] or "{}").get("version") == n - 1)
            conn.execute(
                "INSERT INTO item_versions (short_id, n, question, options_json, recommend, context, basis_hash, review_id, "
                "created_at, summary_json, refreshed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    short_id,
                    n,
                    question.strip()[:2000],
                    json.dumps(clean),
                    recommend.strip().upper(),
                    (context or "")[:6000],
                    row["basis_hash"],
                    review_id,
                    now_iso(),
                    json.dumps(summary_clean),
                    refreshed,
                ),
            )
            conn.execute("COMMIT")
        return {"short_id": short_id, "n": n}

    def set_summary(self, short_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        """Add a blocker summary to the item's current options without a new version (no new push). For items
        written before summaries existed; later versions carry their own."""
        clean = validate_summary(summary)
        short_id = str(short_id).upper()
        with self._lock, self._connect() as conn:
            moved = conn.execute(
                "UPDATE item_versions SET summary_json = ? WHERE short_id = ? AND n = "
                "(SELECT MAX(n) FROM item_versions WHERE short_id = ?)",
                (json.dumps(clean), short_id, short_id),
            ).rowcount
        if not moved:
            raise ReviewError(f"{short_id} has no options to summarise")
        return clean

    def needs_options(self) -> list[dict[str, Any]]:
        """Open items with no options yet, whose basis changed since the options were written, or that an agent
        reopened (a met deferral condition, or an auto item an agent gave back) while showing the options that are
        still current."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT short_id, args_json FROM actions WHERE action IN ('reopen_deferral', 'auto_return', 'sweep_refresh') "
                "ORDER BY id"
            ).fetchall()
        # The reopen (or an agent returning an auto item) records the version it saw; a newer version answers it.
        stale_at = {r[0]: json.loads(r[1] or "{}").get("version") for r in rows}
        return [
            i
            for i in self.items(statuses=("open",))
            if i["version"] is None
            or i["version"]["basis_hash"] != i["basis_hash"]
            or (stale_at.get(i["short_id"]) is not None and stale_at[i["short_id"]] >= i["version"]["n"])
        ]

    def versions(self, short_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM item_versions WHERE short_id = ? ORDER BY n", (str(short_id).upper(),))
            return [self._version(r) for r in rows.fetchall()]

    def answer(
        self, short_id: str, option: str, *, note: str = "", surface: str, telegram_msg_id: str | None = None
    ) -> dict[str, Any]:
        """Record the user's answer. The first answer wins; later ones are refused, not stored."""
        if surface not in SURFACES:
            raise ReviewError(f"surface must be one of {', '.join(SURFACES)}")
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        version = item["version"]
        if version is None:
            raise ReviewError(f"{item['short_id']} has no options yet")
        if version["basis_hash"] != item["basis_hash"]:
            # The source changed after these options were written; an answer to them would be applied blind.
            raise ReviewError(
                f"{item['short_id']}'s options are out of date (its source changed); new options come with the next tick"
            )
        option = str(option or "").strip().upper()
        if option not in {o["id"] for o in version["options"]}:
            raise ReviewError(f"{item['short_id']} has no option {option or '(empty)'}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" * len(ANSWERABLE))
            won = conn.execute(
                f"UPDATE items SET status = 'answered', updated_at = ? WHERE short_id = ? AND status IN ({marks})",
                (now_iso(), item["short_id"], *ANSWERABLE),
            ).rowcount
            if not won:
                conn.execute("ROLLBACK")
                current = self.get(item["short_id"]) or {}
                reason = "already answered" if self.answers(item["short_id"]) else f"item is {current.get('status')}"
                return {"accepted": False, "reason": reason, "item": current}
            cursor = conn.execute(
                "INSERT INTO answers (short_id, version, option, note, surface, telegram_msg_id, answered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item["short_id"], version["n"], option, (note or "").strip()[:4000], surface, telegram_msg_id, now_iso()),
            )
            conn.execute("COMMIT")
        chosen = next(o for o in version["options"] if o["id"] == option)
        return {"accepted": True, "answer_id": cursor.lastrowid, "option": chosen, "item": self.get(item["short_id"])}

    def latest_answer(self, short_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The winning answer and the option it chose, from the version that was shown when it was given."""
        rows = self.answers(short_id)
        if not rows:
            raise ReviewError(f"{short_id} has no answer")
        answer = rows[-1]
        shown = self.versions(short_id)[answer["version"] - 1]
        return answer, next(o for o in shown["options"] if o["id"] == answer["option"])

    def log_action(
        self,
        answer_id: int | None,
        short_id: str,
        action: str,
        args: dict[str, Any],
        state: str,
        *,
        receipt: str | None = None,
        error: str | None = None,
    ) -> int:
        """Append one executor step (append-only; a later row with the same answer supersedes an earlier one)."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, error, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (answer_id, str(short_id).upper(), action, json.dumps(args, default=str), state, receipt, error, now_iso()),
            )
            return int(cursor.lastrowid or 0)

    def applied_idea_promotions(self) -> list[str]:
        """Items whose answer was applied as an idea promotion (to find ones applied without a destination)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT short_id FROM actions WHERE action = 'idea_status' AND state = 'applied' "
                'AND args_json LIKE \'%"status": "promoted"%\' ORDER BY short_id'
            ).fetchall()
        return [r[0] for r in rows]

    def actions(self, short_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM actions WHERE short_id = ? ORDER BY id", (str(short_id).upper(),))
            return [dict(r) for r in rows.fetchall()]

    def refresh_collected_source(self, short_id: str, basis: Any, context: str, owner_profile: str | None) -> bool:
        """Refresh existing unattended items without inserting rows or changing answers."""
        with self._lock, self._connect() as conn:
            return (
                conn.execute(
                    "UPDATE items SET basis_json = ?, basis_hash = ?, context = ?, owner_profile = ?, updated_at = ? "
                    "WHERE short_id = ? AND status IN ('open', 'deferred', 'watching', 'auto')",
                    (
                        json.dumps(basis, sort_keys=True, default=str),
                        basis_hash(basis),
                        context[:6000],
                        owner_profile,
                        now_iso(),
                        short_id,
                    ),
                ).rowcount
                > 0
            )

    def refresh_basis(self, short_id: str, basis: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE items SET basis_json = ?, basis_hash = ?, updated_at = ? WHERE short_id = ?",
                (json.dumps(basis, sort_keys=True, default=str), basis_hash(basis), now_iso(), str(short_id).upper()),
            )

    def for_owner(self, source: str) -> list[dict[str, Any]]:
        """Handed-off answers waiting for the job that filed them (``request_decision.py --answers``)."""
        out = []
        for item in self.items(statuses=("ready_for_owner",)):
            if item["source"] != source:
                continue
            answer, option = self.latest_answer(item["short_id"])
            out.append(
                {
                    "short_id": item["short_id"],
                    "key": item["key"],
                    "title": item["title"],
                    "question": (item["version"] or {}).get("question"),
                    "option": option,
                    "note": answer["note"],
                    "answered_at": answer["answered_at"],
                }
            )
        return out

    def acknowledge(self, short_id: str, receipt: str) -> bool:
        """The owner applied a handed-off answer. ``ready_for_owner -> acked``, with the owner's receipt logged."""
        receipt = (receipt or "").strip()
        if not receipt:
            raise ReviewError("an acknowledgement needs a receipt (what was applied)")
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        if not self.set_status(item["short_id"], "acked", expect=("ready_for_owner",)):
            return False
        answer, option = self.latest_answer(item["short_id"])
        self.log_action(
            answer["id"], item["short_id"], option["action"], option.get("args") or {}, "acked", receipt=receipt[:2000]
        )
        return True

    def recent_closed(
        self, *, limit: int = 20, project: str | None = None, exclude_initiatives: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        """Items that were answered and closed most recently (the Decisions log head)."""
        marks = ",".join("?" * len(TERMINAL))
        # Agent-settled items appear in their own Review tab section, not under the user's decisions.
        clause = (
            f"i.status IN ({marks}) AND i.status != 'settled' "
            "AND EXISTS (SELECT 1 FROM answers a WHERE a.short_id = i.short_id)"
        )
        params: list[Any] = [*TERMINAL]
        if project is not None:
            scoped, extra = _project_sql(project, exclude_initiatives)
            clause += f" AND {scoped}"
            params.extend(extra)
        params.append(max(1, min(limit, 200)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT i.* FROM items i WHERE {clause} ORDER BY i.updated_at DESC LIMIT ?", params
            ).fetchall()
            return [self._item(conn, r) for r in rows]

    def answers(self, short_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM answers WHERE short_id = ? ORDER BY id", (str(short_id).upper(),))
            return [dict(r) for r in rows.fetchall()]

    # --- deferral and dismissal --------------------------------------------------------------------------

    def defer(
        self, short_id: str, *, kind: str, until: str | None, reason: str, expect: tuple[str, ...] = ("open", "deferred", "auto")
    ) -> dict[str, Any]:
        if kind not in DEFER_KINDS:
            raise ReviewError(f"defer kind must be one of {', '.join(DEFER_KINDS)}")
        reason = (reason or "").strip()
        if kind == "date":
            try:
                dt.date.fromisoformat(str(until or ""))
            except ValueError as exc:
                raise ReviewError("a date deferral needs until=YYYY-MM-DD") from exc
        if kind == "indefinite" and not reason:
            raise ReviewError("an indefinite deferral needs a reason")
        item = self.get(short_id)
        if item is None:
            raise ReviewError(f"unknown item {short_id}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            moved = conn.execute(
                f"UPDATE items SET status = 'deferred', updated_at = ? WHERE short_id = ? "
                f"AND status IN ({','.join('?' * len(expect))})",
                (now_iso(), item["short_id"], *expect),
            ).rowcount
            if not moved:
                conn.execute("ROLLBACK")
                raise ReviewError(f"{item['short_id']} is {item['status']}; only open items can be deferred")
            conn.execute(
                "INSERT INTO deferrals (short_id, kind, until, reason, basis_hash, at) VALUES (?, ?, ?, ?, ?, ?)",
                (item["short_id"], kind, until if kind == "date" else None, reason[:1000], item["basis_hash"], now_iso()),
            )
            conn.execute("COMMIT")
        return self.get(item["short_id"]) or {}

    def due_deferrals(self, *, today: dt.date, basis_hashes: dict[str, str]) -> list[str]:
        """Deferred items whose condition is met: the date arrived, or the key's basis changed."""
        due: list[str] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.short_id, i.key, d.kind, d.until, d.basis_hash "
                "FROM items i JOIN deferrals d ON d.short_id = i.short_id "
                "WHERE i.status = 'deferred' AND d.id = (SELECT MAX(id) FROM deferrals WHERE short_id = i.short_id) "
                "ORDER BY i.short_id"
            ).fetchall()
        for row in rows:
            if row["kind"] == "date" and row["until"] and dt.date.fromisoformat(row["until"]) <= today:
                due.append(row["short_id"])
            elif row["kind"] == "state_change":
                current = basis_hashes.get(row["key"])
                if current is not None and current != row["basis_hash"]:
                    due.append(row["short_id"])
        return due

    def dismiss(self, key: str, *, evidence_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO dismissals (key, evidence_hash, at) VALUES (?, ?, ?)", (key, evidence_hash, now_iso())
            )

    def is_dismissed(self, key: str, evidence_hash: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute("SELECT 1 FROM dismissals WHERE key = ? AND evidence_hash = ?", (key, evidence_hash)).fetchone()
                is not None
            )

    # --- queue ---------------------------------------------------------------------------------------------

    @staticmethod
    def score(item: dict[str, Any], now: dt.datetime) -> float:
        opened = _parse_time(item.get("opened_at")) or now
        age_days = max(1.0, (now - opened).total_seconds() / 86400)
        return max(1, len(item.get("blocks") or [])) * age_days

    def queue(
        self,
        *,
        now: dt.datetime | None = None,
        section: str = "gated",
        project: str | None = None,
        exclude_initiatives: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Open items in one section, urgent first, then by blocked work x age (highest first)."""
        now = now or dt.datetime.now(dt.UTC)
        items = self.items(
            statuses=("open",), section=section, project=project, exclude_initiatives=exclude_initiatives
        )
        return sorted(items, key=lambda i: (not i["urgent_rule"], -self.score(i, now), i["opened_at"]))

    # --- reviews -------------------------------------------------------------------------------------------

    def create_review(self, *, trigger: str, model: str | None, window_start: str | None) -> dict[str, Any]:
        stamp = dt.datetime.now(dt.UTC)
        review_id = f"R{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO reviews (id, generated_at, window_start, trigger, model) VALUES (?, ?, ?, ?, ?)",
                (review_id, stamp.replace(microsecond=0).isoformat(), window_start, trigger, model),
            )
        return self.review(review_id) or {}

    def update_review(self, review_id: str, **fields: Any) -> None:
        allowed = {"digest", "summaries_json", "gaps_json", "telegram_json", "facts_json"}
        bad = set(fields) - allowed
        if bad:
            raise ReviewError(f"cannot set {', '.join(sorted(bad))} on a review")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE reviews SET {sets} WHERE id = ?", (*fields.values(), review_id))

    def link_items(self, review_id: str, short_ids: list[str]) -> None:
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO review_items (review_id, short_id) VALUES (?, ?)",
                [(review_id, s.upper()) for s in short_ids],
            )

    def review(self, review_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            if row is None:
                return None
            review = dict(row)
            for field in ("summaries_json", "gaps_json", "telegram_json", "facts_json"):
                review[field.removesuffix("_json")] = json.loads(review.pop(field) or "null")
            review["items"] = [
                r[0]
                for r in conn.execute(
                    "SELECT short_id FROM review_items WHERE review_id = ? ORDER BY CAST(SUBSTR(short_id, 2) AS INTEGER)",
                    (review_id,),
                )
            ]
            return review

    def latest_review_before(self, review_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM reviews WHERE generated_at < (SELECT generated_at FROM reviews WHERE id = ?) "
                "ORDER BY generated_at DESC, id DESC LIMIT 1",
                (review_id,),
            ).fetchone()
        return self.review(row[0]) if row else None

    def latest_review(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM reviews ORDER BY generated_at DESC, id DESC LIMIT 1").fetchone()
        return self.review(row[0]) if row else None

    def refresh_completion(self) -> list[str]:
        """Mark reviews complete once none of their items is still open. Returns the newly completed ids."""
        done: list[str] = []
        with self._lock, self._connect() as conn:
            pending = conn.execute("SELECT id FROM reviews WHERE completed_at IS NULL").fetchall()
            for (review_id,) in pending:
                still_open = conn.execute(
                    "SELECT 1 FROM review_items ri JOIN items i ON i.short_id = ri.short_id "
                    "WHERE ri.review_id = ? AND i.status = 'open' AND i.section = 'gated' LIMIT 1",
                    (review_id,),
                ).fetchone()
                if still_open is None:
                    conn.execute("UPDATE reviews SET completed_at = ? WHERE id = ?", (now_iso(), review_id))
                    done.append(review_id)
        return done

    def last_completed_review(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM reviews WHERE completed_at IS NOT NULL ORDER BY generated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return self.review(row[0]) if row else None

    def last_completed_at(self) -> str | None:
        review = self.last_completed_review()
        return review["completed_at"] if review else None

    # --- requests and urgent pushes ------------------------------------------------------------------------

    def log_request(self, source: str, key: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO requests (source, key, payload_json, at) VALUES (?, ?, ?, ?)",
                (source, key, json.dumps(payload, default=str), now_iso()),
            )

    def record_urgent_push(self, short_id: str, basis: str, rule: str, telegram_msg_id: str | None) -> bool:
        """False when this item was already pushed for this basis."""
        with self._lock, self._connect() as conn:
            return (
                conn.execute(
                    "INSERT OR IGNORE INTO urgent_pushes (short_id, basis_hash, rule, at, telegram_msg_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (short_id.upper(), basis, rule, now_iso(), telegram_msg_id),
                ).rowcount
                > 0
            )

    def to_push(self) -> list[dict[str, Any]]:
        """Open gated decisions whose current options have not been pushed yet (one push per item and version)."""
        out = []
        with self._connect() as conn:
            pushed = {(r[0], r[1]) for r in conn.execute("SELECT short_id, basis_hash FROM urgent_pushes")}
        for item in self.queue():
            version = item["version"]
            if item["refreshed"]:
                continue  # the stale sweep rewrote it: the daily digest shows it, marked refreshed (user, 2026-11-01)
            if version and version["basis_hash"] == item["basis_hash"] and (item["short_id"], _push_key(version)) not in pushed:
                out.append(item)
        return out

    def record_push(self, item: dict[str, Any], telegram_msg_id: str | None) -> bool:
        return self.record_urgent_push(item["short_id"], _push_key(item["version"]), "new_decision", telegram_msg_id)

    def last_digest_day(self, tz: dt.tzinfo) -> dt.date | None:
        """Local date of the newest review whose digest was delivered (``telegram_json`` not empty)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT generated_at FROM reviews WHERE telegram_json NOT IN ('', '[]') ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        stamp = _parse_time(row[0]) if row else None
        return stamp.astimezone(tz).date() if stamp else None

    def urgent_pushes_since(self, since: str | None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM urgent_pushes WHERE at >= ? ORDER BY at", (since or "",)).fetchall()
        return [dict(r) for r in rows]

    def actions_since(self, since: str | None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM actions WHERE at >= ? ORDER BY id", (since or "",)).fetchall()
        return [dict(r) for r in rows]
