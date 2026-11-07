"""Cards that wait on the user but have no open Review decision ("Waiting on you").

A Review item can stop showing while its card still waits on the user: an answer that parked it (an option wired
to ``defer``), a watch whose ground went stale, or a card the gate list never named. The card's newest comment is
the authoritative handoff (``**You:** <step>``); when that step is not "nothing ...", the user has work to do.

This module only reads: the Kanban board (``mode=ro``) and the review store. It never changes an item or a card.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import kanban
from .reviews import ReviewStore
from .settings import ProjectSettings

# Card statuses that can wait on the user. Done and archived cards need nothing.
WAITING_STATUSES = ("blocked", "triage", "scheduled", "todo", "ready")
# "You: nothing needed now", "You: nothing on this card", "You: none" mean the user has nothing to do.
NOTHING = re.compile(r"^\s*[`*_]*\s*(nothing|none|no action|n/a)\b", re.IGNORECASE)
# Items in these statuses are visible as decisions (the queue or "Waiting for options"); the card is not repeated.
SHOWN = ("open", "answered", "awaiting_confirm", "applying")
ACTIVE_ITEM = ("open", "deferred", "watching", "auto", "answered", "awaiting_confirm", "applying", "ready_for_owner", "failed")


def needs_user(you: str | None) -> bool:
    """True when a handoff's ``You:`` line names something for the user to do."""
    return bool(you and you.strip()) and not NOTHING.match(you or "")


def _card_items(store: ReviewStore, board: str) -> dict[str, dict[str, Any]]:
    """The newest active Review item per card on *board*, matched by basis card or gate key."""
    out: dict[str, dict[str, Any]] = {}
    for item in store.items(statuses=ACTIVE_ITEM, limit=5000):
        basis = item.get("basis") or {}
        card = basis.get("card") if basis.get("board") == board else None
        if not card:
            match = re.fullmatch(rf"kanban:{re.escape(board)}:(t_[0-9a-f]{{8}}):gate", item["key"])
            card = match.group(1) if match else None
        if card and card not in out:
            out[card] = item
    return out


def collect(store: ReviewStore, projects: list[ProjectSettings], *, include_shown: bool = False) -> dict[str, Any]:
    """``{"items": [...], "errors": [...]}``: one entry per card whose newest handoff asks the user to act and
    whose Review item (if any) is not already shown as a decision (``include_shown`` keeps those too)."""
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for project in projects:
        board = project.kanban_board
        if not board:
            continue
        try:
            conn = kanban._connect(kanban.board_path(str(board)))
        except (ImportError, OSError, sqlite3.Error) as exc:
            errors.append(f"{project.id}: Kanban unavailable: {exc}")
            continue
        try:
            rows = conn.execute(
                f"SELECT id, title, status, assignee FROM tasks WHERE status IN ({','.join('?' * len(WAITING_STATUSES))})",
                WAITING_STATUSES,
            ).fetchall()
        except sqlite3.Error as exc:
            errors.append(f"{project.id}: Kanban schema not readable: {exc}")
            continue
        finally:
            conn.close()
        cards = {r["id"]: r for r in rows}
        comments = kanban.newest_comments(str(board), list(cards))
        if comments["state"] != "fresh":
            errors.append(f"{project.id}: {comments['error']}")
            continue
        linked = _card_items(store, str(board))
        for card_id, comment in comments["cards"].items():
            handoff = kanban.parse_handoff(comment["body"])
            you = handoff["you"] or ""
            if not needs_user(you):
                continue
            item = linked.get(card_id)
            if item and item["status"] in SHOWN and not include_shown:
                continue
            row = cards[card_id]
            items.append(
                {
                    "project": project.id,
                    "board": str(board),
                    "card": card_id,
                    "title": row["title"],
                    "status": row["status"],
                    "assignee": row["assignee"],
                    "you": you[:500],
                    "header": handoff["header"],
                    "since": comment["created_at"],
                    "comment_author": comment["author"],
                    "item": _item_brief(store, item) if item else None,
                }
            )
    items.sort(key=lambda i: i["since"] or "")
    return {"items": items, "errors": errors}


def _item_brief(store: ReviewStore, item: dict[str, Any]) -> dict[str, Any]:
    """The parked item, and the user's last answer when there is one (so the row says what he already chose)."""
    answers = store.answers(item["short_id"])
    last = answers[-1] if answers else None
    chosen = None
    if last:
        versions = store.versions(item["short_id"])
        shown = versions[last["version"] - 1] if 0 < last["version"] <= len(versions) else None
        option = next((o for o in (shown or {}).get("options") or [] if o["id"] == last["option"]), None)
        chosen = {
            "option": last["option"],
            "label": option["label"] if option else None,
            "action": option["action"] if option else None,
            "note": last["note"],
            "answered_at": last["answered_at"],
        }
    return {
        "short_id": item["short_id"],
        "status": item["status"],
        "initiative": item.get("initiative"),
        "cycle": item.get("cycle"),
        "watch_reason": item.get("watch_reason"),
        "deferral": item.get("deferral"),
        "answer": chosen,
    }


def find(store: ReviewStore, project: ProjectSettings, card: str) -> dict[str, Any] | None:
    """The attention entry for one card, or None when the card does not wait on the user. A card with an open
    decision counts too: a session can help with it either way."""
    return next((i for i in collect(store, [project], include_shown=True)["items"] if i["card"] == card), None)
