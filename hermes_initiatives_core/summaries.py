"""Status summaries: one short agent-written paragraph per initiative, refreshed by the 30-minute tick.

The text lives in ``<plugin data>/summaries.db``, never in the vault, so a refresh edits no user file and causes
no sync churn. Each row keeps two clocks: ``generated_at`` (when the text was written) and ``checked_at`` (when
a tick last confirmed the text still matches the facts). The page shows ``checked_at``; a row whose
``checked_at`` is older than ``STALE_AFTER_MIN`` is marked stale, so a dead cron is not read as "nothing happened".

The facts are collected here in code (cycles, cards, Review items, the newest card comment). The model only
turns them into prose. A summary is regenerated only when the hash of its facts changed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import kanban, views
from .lifecycle import DONE_CARD_STATUSES
from .model import OPEN_STAGES
from .reviews import ACTIVE, ReviewStore
from .settings import ProjectSettings, data_dir

STALE_AFTER_MIN = 70  # two 30-minute ticks plus slack
MAX_TEXT = 700
MAX_CARDS = 8  # open cards per initiative whose newest comment is read
COMMENT_CLIP = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
  project TEXT NOT NULL,
  initiative TEXT NOT NULL,
  text TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  facts_json TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  PRIMARY KEY (project, initiative)
);
"""


class SummaryError(ValueError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _age_minutes(iso: str | None, now: dt.datetime) -> float | None:
    try:
        return max(0.0, (now - dt.datetime.fromisoformat(str(iso))).total_seconds() / 60)
    except (TypeError, ValueError):
        return None


class SummaryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "summaries.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def rows(self, project: str) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            found = conn.execute("SELECT * FROM summaries WHERE project = ?", (project,)).fetchall()
        return {r["initiative"]: dict(r) for r in found}

    def put(self, project: str, initiative: str, text: str, evidence_hash: str, facts: dict[str, Any]) -> dict[str, Any]:
        clean = " ".join(str(text or "").split())
        if not clean:
            raise SummaryError("summary text is empty")
        if len(clean) > MAX_TEXT:
            raise SummaryError(f"summary is {len(clean)} characters; the limit is {MAX_TEXT}")
        stamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO summaries (project, initiative, text, evidence_hash, facts_json, generated_at, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project, initiative) DO UPDATE SET text = excluded.text, "
                "evidence_hash = excluded.evidence_hash, facts_json = excluded.facts_json, "
                "generated_at = excluded.generated_at, checked_at = excluded.checked_at",
                (project, initiative, clean, evidence_hash, json.dumps(facts, ensure_ascii=False, default=str), stamp, stamp),
            )
        return {"initiative": initiative, "text": clean, "generated_at": stamp}

    def touch(self, project: str, initiative: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE summaries SET checked_at = ? WHERE project = ? AND initiative = ?", (now_iso(), project, initiative)
            )

    def prune(self, project: str, keep: set[str]) -> int:
        with self._lock, self._connect() as conn:
            gone = [
                r[0] for r in conn.execute("SELECT initiative FROM summaries WHERE project = ?", (project,)) if r[0] not in keep
            ]
            for initiative in gone:
                conn.execute("DELETE FROM summaries WHERE project = ? AND initiative = ?", (project, initiative))
        return len(gone)


# --- facts -----------------------------------------------------------------------------------------------


def _latest_comment(board: str | None, card_id: str) -> dict[str, Any] | None:
    try:
        detail = kanban.read_card_detail(board, card_id)
    except (FileNotFoundError, OSError, sqlite3.Error, ImportError):
        return None
    comments = (detail or {}).get("comments") or []
    if not comments:
        return None
    newest = comments[0]
    return {"author": newest.get("author"), "at": newest.get("created_at"), "text": (newest.get("body") or "")[:COMMENT_CLIP]}


def _review_counts(store: ReviewStore, initiative_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in store.items(statuses=ACTIVE, limit=5000):
        if item.get("initiative") == initiative_id:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def facts_for(
    project: ProjectSettings, initiative_id: str, reviews: ReviewStore, today: dt.date | None = None
) -> dict[str, Any] | None:
    """Deterministic facts for one initiative, or ``None`` when it does not exist. No timestamps of this read."""
    detail = views.build_detail(project, initiative_id, today)
    if detail is None:
        return None
    open_cycles = [c for c in detail["cycles"] if c["stage"] in OPEN_STAGES]
    budget = MAX_CARDS
    cycles = []
    for cycle in open_cycles:
        cards, done, merged = [], 0, 0
        for card in cycle["cards"]:
            if card.get("status") in DONE_CARD_STATUSES:
                done += 1
                merged += (card.get("pr") or {}).get("state") == "merged"
                continue
            row = {k: card.get(k) for k in ("id", "title", "status", "assignee") if card.get(k) is not None}
            pr = card.get("pr") or {}
            if pr.get("state") not in (None, "none"):
                row["pr"] = {k: pr.get(k) for k in ("state", "url", "merged_at")}
            if budget > 0:
                budget -= 1
                comment = _latest_comment(project.kanban_board, card["id"])
                if comment:
                    row["newest_comment"] = comment
            cards.append(row)
        observations = [o for o in cycle.get("observations") or [] if o.get("kind") != "note"][-3:]
        cycles.append(
            {
                "id": cycle["id"],
                "title": cycle["title"],
                "stage": cycle["stage"],
                "next_action": cycle["next_action"],
                "flags": cycle["flags"],
                "evaluation_date": cycle["evaluation_date"],
                "release": cycle["release"],
                "open_cards": cards,
                "cards_done": done,
                "prs_merged": merged,
                "recent_observations": [
                    {k: o.get(k) for k in ("at", "kind", "metric", "value", "detail") if o.get(k) is not None}
                    for o in observations
                ],
            }
        )
    return {
        "id": initiative_id,
        "title": detail["title"],
        "goal": detail["goal"],
        "status": detail["status"],
        "focus_cycle": detail["focus_cycle"],
        "open_cycles": cycles,
        "review_items": _review_counts(reviews, initiative_id),
        "kanban": detail["freshness"]["kanban"],
    }


def evidence_hash(facts: dict[str, Any]) -> str:
    blob = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def active_initiative_ids(project: ProjectSettings) -> list[str]:
    listing = views.build_list(project)
    return [i["id"] for i in listing["initiatives"] if i["status"] == "active"]


def tick_input(project: ProjectSettings, store: SummaryStore, reviews: ReviewStore, *, force: bool = False) -> dict[str, Any]:
    """Facts for every active initiative whose summary is missing or out of date.

    An initiative whose facts hash is unchanged keeps its text and gets a fresh ``checked_at``.
    Summaries of initiatives that are gone or no longer active are removed.
    """
    ids = active_initiative_ids(project)
    stored = store.rows(project.id)
    store.prune(project.id, set(ids))
    due, unchanged, skipped = [], [], []
    for initiative_id in ids:
        facts = facts_for(project, initiative_id, reviews)
        if facts is None:
            skipped.append(initiative_id)
            continue
        digest = evidence_hash(facts)
        row = stored.get(initiative_id)
        if row and row["evidence_hash"] == digest and not force:
            store.touch(project.id, initiative_id)
            unchanged.append(initiative_id)
            continue
        entry: dict[str, Any] = {"initiative": initiative_id, "evidence_hash": digest, "facts": facts}
        if row:
            entry["previous"] = {"text": row["text"], "generated_at": row["generated_at"], "facts": json.loads(row["facts_json"])}
        due.append(entry)
    return {"project": project.id, "due": due, "unchanged": unchanged, "skipped": skipped, "max_chars": MAX_TEXT}


def write_summaries(
    project: ProjectSettings, store: SummaryStore, reviews: ReviewStore, payload: dict[str, Any]
) -> dict[str, Any]:
    """Store ``{"summaries": {id: {"text", "evidence_hash"}}}``. All or nothing: any bad entry writes nothing.

    The hash must be the one ``tick_input`` printed, so facts that moved after the model read them are picked
    up again by the next tick instead of being recorded as current.
    """
    entries = payload.get("summaries")
    if not isinstance(entries, dict) or not entries:
        raise SummaryError('expected {"summaries": {"in-…": {"text": "…", "evidence_hash": "…"}}}')
    known = set(active_initiative_ids(project))
    errors, clean = [], {}
    for initiative_id, entry in entries.items():
        if initiative_id not in known:
            errors.append(f"{initiative_id}: not an active initiative of project {project.id}")
            continue
        text = " ".join(str((entry or {}).get("text") or "").split()) if isinstance(entry, dict) else ""
        digest = str((entry or {}).get("evidence_hash") or "") if isinstance(entry, dict) else ""
        if not text or not digest:
            errors.append(f"{initiative_id}: needs text and the evidence_hash from the tick input")
        elif len(text) > MAX_TEXT:
            errors.append(f"{initiative_id}: {len(text)} characters; the limit is {MAX_TEXT}")
        else:
            clean[initiative_id] = (text, digest)
    if errors:
        raise SummaryError("; ".join(errors))
    written = []
    for initiative_id, (text, digest) in clean.items():
        facts = facts_for(project, initiative_id, reviews) or {}
        written.append(store.put(project.id, initiative_id, text, digest, facts))
    return {"ok": True, "written": [w["initiative"] for w in written]}


def public_view(project_id: str, store: SummaryStore, now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    """What the page needs: text, when it was written and last confirmed, and whether the tick has gone quiet."""
    now = now or dt.datetime.now(dt.UTC)
    out = {}
    for initiative_id, row in store.rows(project_id).items():
        age = _age_minutes(row["checked_at"], now)
        out[initiative_id] = {
            "text": row["text"],
            "generated_at": row["generated_at"],
            "checked_at": row["checked_at"],
            "age_minutes": round(age) if age is not None else None,
            "stale": age is None or age > STALE_AFTER_MIN,
        }
    return out
