"""Read-only Kanban card and PR lookup. Opens the board database with ``mode=ro`` and never writes."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .model import PR_URL

CARD_COLUMNS = ("id", "title", "status", "assignee", "completed_at", "completion_contract")
# Hermes's documented board slug: lowercase, 1-64 chars, letters/digits/-/_, not starting with - or _.
_BOARD_SLUG = re.compile(r"[a-z0-9][a-z0-9\-_]{0,63}")


def _fenced() -> bool:
    """Whether this execution is a fenced Kanban worker, where Hermes resolves every board through the env pin.

    Dispatched workers carry ``HERMES_KANBAN_TASK``; delegated children carry ``HERMES_DELEGATED_CHILD_CONTEXT`` in
    their environment, or (in-process) the public ``agent.delegation_context`` flag this asks for when available.
    """
    import os

    if os.environ.get("HERMES_KANBAN_TASK", "").strip() or os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT", "").strip():
        return True
    try:
        from agent.delegation_context import explicit_board_intent_is_pinned
    except ImportError:  # Hermes not importable (a standalone script): the environment is the whole story
        return False
    return bool(explicit_board_intent_is_pinned())


def board_path(board: str) -> Path:
    """The board's SQLite file, by the layout the Kanban guide documents (no private Hermes import).

    ``HERMES_KANBAN_DB`` pins it (dispatcher-injected into workers); the ``default`` board is
    ``<kanban home>/kanban.db``; any other board is ``<kanban home>/kanban/boards/<slug>/kanban.db``. The kanban
    home is ``HERMES_KANBAN_HOME``, else the shared Hermes root every profile's boards live under.
    """
    import os

    from .settings import hermes_root

    slug = str(board or "").strip().lower()
    if not _BOARD_SLUG.fullmatch(slug):
        raise ValueError(f"invalid board slug {board!r}: 1-64 lowercase letters, digits, '-' or '_', not leading '-'/'_'")
    board = slug

    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    # A named board outranks the pin, except inside a dispatcher-fenced worker or its delegated children: there the
    # pin is the isolation boundary and Hermes resolves every board through it, so the plugin does the same.
    if pinned and _fenced():
        return Path(pinned).expanduser()
    home = Path(os.environ.get("HERMES_KANBAN_HOME", "").strip() or hermes_root()).expanduser()
    return home / "kanban.db" if board == "default" else home / "kanban" / "boards" / board / "kanban.db"


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Kanban board database not found: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        import datetime as dt

        return dt.datetime.fromtimestamp(value, tz=dt.UTC).isoformat()
    return str(value)


def _pr_url(value: Any) -> str | None:
    match = PR_URL.search(value) if isinstance(value, str) else None
    return match.group(0) if match else None


def pr_info(contract: Any, runs: list[sqlite3.Row]) -> dict[str, Any]:
    """PR link and merge state from the newest run metadata, else the completion contract."""
    url = None
    merged_at = None
    merge_commit = None
    for run in runs:
        try:
            meta = json.loads(run["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            continue
        if url is None:
            url = _pr_url(meta.get("published_pr"))
        if merged_at is None and meta.get("merged_at") and meta.get("merge_commit"):
            merged_at, merge_commit = str(meta["merged_at"]), str(meta["merge_commit"])
        if url and merged_at:
            break
    if url is None:
        url = _pr_url(contract)
    if url is None:
        return {"state": "none", "url": None, "merged_at": None, "merge_commit": None}
    return {
        "state": "merged" if merged_at else "open_or_unknown",
        "url": url,
        "merged_at": merged_at,
        "merge_commit": merge_commit[:12] if merge_commit else None,
    }


def read_cards(board: str | None, card_ids: list[str], path: Path | None = None) -> dict[str, Any]:
    """Return ``{"state", "error", "cards": {id: card}}``. A missing card is ``not_found``, never an error."""
    if not card_ids:
        return {"state": "fresh", "error": None, "cards": {}}
    if not board and path is None:
        return {"state": "unavailable", "error": "no Kanban board configured", "cards": {}}
    try:
        db = path or board_path(str(board))
        conn = _connect(db)
    except (ImportError, OSError, sqlite3.Error) as exc:
        return {"state": "unavailable", "error": str(exc), "cards": {}}
    try:
        marks = ",".join("?" * len(card_ids))
        rows = conn.execute(f"SELECT {', '.join(CARD_COLUMNS)} FROM tasks WHERE id IN ({marks})", card_ids).fetchall()
        runs: dict[str, list[sqlite3.Row]] = {}
        for run in conn.execute(f"SELECT task_id, metadata FROM task_runs WHERE task_id IN ({marks}) ORDER BY id DESC", card_ids):
            runs.setdefault(run["task_id"], []).append(run)
    except sqlite3.Error as exc:
        return {"state": "unavailable", "error": f"Kanban schema not readable: {exc}", "cards": {}}
    finally:
        conn.close()
    found = {row["id"]: row for row in rows}
    cards: dict[str, Any] = {}
    for card_id in card_ids:
        row = found.get(card_id)
        if row is None:
            cards[card_id] = {"id": card_id, "state": "not_found"}
            continue
        cards[card_id] = {
            "id": card_id,
            "state": "fresh",
            "title": row["title"],
            "status": row["status"],
            "assignee": row["assignee"],
            "completed_at": _iso(row["completed_at"]),
            "pr": pr_info(row["completion_contract"], runs.get(card_id, [])),
        }
    return {"state": "fresh", "error": None, "cards": cards}


TEXT_LIMIT = 20_000
COMMENT_LIMIT = 30

# The one body line that ties a card to an initiative cycle: "- Initiative cycle: <cycle-id>". Prose does not count.
CYCLE_MARKER = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?Initiative cycle:[ \t]*`?(in-[a-z0-9]+(?:-[a-z0-9]+)*-c\d{2,3})`?(?![\w-])",
    re.IGNORECASE | re.MULTILINE,
)


def cycle_markers(board: str | None, path: Path | None = None) -> dict[str, Any]:
    """Return ``{"state", "error", "markers": {cycle_id: [card_id]}}`` from non-archived card bodies, in card order."""
    if not board and path is None:
        return {"state": "unavailable", "error": "no Kanban board configured", "markers": {}}
    try:
        conn = _connect(path or board_path(str(board)))
    except (ImportError, OSError, sqlite3.Error) as exc:
        return {"state": "unavailable", "error": str(exc), "markers": {}}
    try:
        rows = conn.execute(
            "SELECT id, body FROM tasks WHERE status != 'archived' AND body LIKE '%nitiative cycle:%' ORDER BY created_at, rowid"
        ).fetchall()
    except sqlite3.Error as exc:
        return {"state": "unavailable", "error": f"Kanban schema not readable: {exc}", "markers": {}}
    finally:
        conn.close()
    markers: dict[str, list[str]] = {}
    for row in rows:
        for cycle_id in dict.fromkeys(m.lower() for m in CYCLE_MARKER.findall(row["body"] or "")):
            markers.setdefault(cycle_id, []).append(row["id"])
    return {"state": "fresh", "error": None, "markers": markers}


# The card-comment handoff the agents write: a bold header line, then a "**You:**" line.
HANDOFF_HEADER = re.compile(r"^\*\*([^*\n]{2,120})\*\*", re.MULTILINE)
HANDOFF_YOU = re.compile(r"^\*\*You:\*\*[ \t]*(.+)$", re.MULTILINE)


def parse_handoff(body: str | None) -> dict[str, str | None]:
    """``{"header", "you"}`` from a handoff comment; either is None when the comment does not carry it."""
    text = body or ""
    header, you = HANDOFF_HEADER.search(text), HANDOFF_YOU.search(text)
    return {"header": header.group(1).strip() if header else None, "you": you.group(1).strip() if you else None}


def newest_comments(board: str | None, card_ids: list[str], path: Path | None = None) -> dict[str, Any]:
    """``{"state", "error", "cards": {id: {status, block_kind, author, body, created_at}}}`` for each found card.

    ``body`` and ``author`` are None when the card has no comment. Read-only; a missing card is left out."""
    if not card_ids:
        return {"state": "fresh", "error": None, "cards": {}}
    if not board and path is None:
        return {"state": "unavailable", "error": "no Kanban board configured", "cards": {}}
    try:
        conn = _connect(path or board_path(str(board)))
    except (ImportError, OSError, sqlite3.Error) as exc:
        return {"state": "unavailable", "error": str(exc), "cards": {}}
    try:
        cards: dict[str, Any] = {}
        for card_id in dict.fromkeys(card_ids):
            row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (card_id,)).fetchone()
            if row is None:
                continue
            comment = conn.execute(
                "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (card_id,),
            ).fetchone()
            cards[card_id] = {
                "status": row["status"],
                "block_kind": row["block_kind"],
                "author": comment["author"] if comment else None,
                "body": comment["body"] if comment else None,
                "created_at": _iso(comment["created_at"]) if comment else None,
            }
    except sqlite3.Error as exc:
        return {"state": "unavailable", "error": f"Kanban schema not readable: {exc}", "cards": {}}
    finally:
        conn.close()
    return {"state": "fresh", "error": None, "cards": cards}


def _clip(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n\n… (truncated)"


def read_card_detail(board: str | None, card_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """One card for the read-only card view: fields, runs, comments, links. ``None`` when the card does not exist."""
    if not board and path is None:
        raise FileNotFoundError("no Kanban board configured")
    conn = _connect(path or board_path(str(board)))
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return None
        runs = conn.execute(
            "SELECT id, profile, status, outcome, started_at, ended_at, summary, metadata, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC",
            (card_id,),
        ).fetchall()
        comments = conn.execute(
            "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (card_id, COMMENT_LIMIT),
        ).fetchall()
        parents = [r[0] for r in conn.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (card_id,))]
        children = [r[0] for r in conn.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (card_id,))]
    finally:
        conn.close()
    fields = row.keys()
    summary = next((run["summary"] for run in runs if run["summary"]), None)
    return {
        "id": card_id,
        "board": board,
        "title": row["title"],
        "status": row["status"],
        "assignee": row["assignee"],
        "priority": row["priority"] if "priority" in fields else None,
        "branch": row["branch_name"] if "branch_name" in fields else None,
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "completed_at": _iso(row["completed_at"]),
        "body": _clip(row["body"]),
        "result": _clip(row["result"]),
        "latest_summary": _clip(summary),
        "pr": pr_info(row["completion_contract"] if "completion_contract" in fields else None, runs),
        "runs": [
            {
                "id": run["id"],
                "profile": run["profile"],
                "status": run["status"],
                "outcome": run["outcome"],
                "started_at": _iso(run["started_at"]),
                "ended_at": _iso(run["ended_at"]),
                "error": _clip(run["error"], 2_000),
            }
            for run in runs
        ],
        "comments": [
            {"author": c["author"], "body": _clip(c["body"], 8_000), "created_at": _iso(c["created_at"])} for c in comments
        ],
        "parents": parents,
        "children": children,
    }
