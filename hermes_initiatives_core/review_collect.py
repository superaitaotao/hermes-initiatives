"""Collector for the daily review: live human gates become review items. Deterministic, no LLM, read-only.

Sources, per configured project:

- Kanban: exactly the cards on the recovery tick's gate list (``gates.json`` from ``kanban_human_gates_watch.py``,
  written after the recovery pass of the same tick). The collector does not classify cards itself: one rule
  decides what needs a human. Card details are read ``mode=ro``. The basis is the card status, block kind, and
  the newest lifecycle event id; comments do not change the basis, so a cron that comments every tick does not
  reopen a deferred item. Without a gate list for a board, that board is not read and nothing on it resolves.
- Cycle files: an ``exploring`` cycle, ``evaluation_due``, ``reevaluation_due`` (a measuring cycle answered with
  ``await_evidence`` whose window has ended), ``verdict_due`` (a verified spot-check cycle with criteria, no
  observation window, and no verdict), ``disposition_due`` (a measuring cycle with a verdict but no disposition,
  once its window has ended), ``no_next_action``, a decided/shipping cycle with no
  linked cards, or one whose cards are all done (``cards_done``: at once when decided, after the release grace week
  when shipping). Missing success criteria become one item per initiative; it closes only when
  ``measurement.success_criteria`` is set, not when a decision row is recorded.
- Ideas in ``proposed`` or ``reviewing``.

``sync`` admits Kanban candidates from the gate list, but closes existing Kanban items only with fresh
terminal or confirmed missing-card proof. List omission retains unfinished work. Other collected sources
close when their gate is gone. A source that cannot be read resolves nothing. Items filed through
``request_decision.py`` are never resolved here; their owner closes them.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any

from . import kanban
from .lifecycle import RELEASE_GRACE_DAYS, as_list, cards_done
from .model import OPEN_IDEA_STATUSES
from .reviews import ReviewStore, basis_hash
from .settings import ProjectSettings
from .store import active_observations, load
from .validate import measurement_problems
from .views import build_detail, cited_docs

GATE_EVENT_KINDS = (
    "created",
    "blocked",
    "unblocked",
    "scheduled",
    "block_loop_detected",
    "status",
    "promoted",
    "completed",
    "archived",
    "specified",
    "review_requested",
    "changes_requested",
    "assigned",
    "reassigned",
)
UPDATE_EVENT_KINDS = (
    "created",
    "blocked",
    "unblocked",
    "scheduled",
    "block_loop_detected",
    "completed",
    "archived",
    "review_requested",
    "changes_requested",
    "gave_up",
    "crashed",
    "timed_out",
)
DONE_STATUSES = ("done", "archived")
COLLECTED_SOURCES = ("kanban", "cycle", "criteria", "idea")
CONTEXT_LIMIT = 1500
UPDATE_LIMIT = 300
GATE_LIST_MAX_AGE = dt.timedelta(minutes=15)

# {board: {card id: gate-list entry}}. The entry has at least ``id``; ``waiting`` is the ask, ``signals`` the reason.
GateList = dict[str, dict[str, dict[str, Any]]]


class SourceProof(Enum):
    TERMINAL = "terminal"
    MISSING_CARD = "missing_card"
    NON_TERMINAL = "non_terminal"
    UNAVAILABLE = "unavailable"


def kanban_source_proof(board: str | None, card: str | None) -> tuple[SourceProof, str | None]:
    if not board or not card:
        return SourceProof.UNAVAILABLE, "stored basis names no board or card"
    source = kanban.read_cards(board, [card])
    if source["state"] != "fresh":
        return SourceProof.UNAVAILABLE, source["error"]
    row = source["cards"][card]
    if row["state"] == "not_found":
        return SourceProof.MISSING_CARD, None
    if row["status"] in DONE_STATUSES:
        return SourceProof.TERMINAL, None
    return SourceProof.NON_TERMINAL, None


class GateListError(ValueError):
    """The recovery tick's gate list is missing, stale, or malformed; the Kanban source counts as unreadable."""


def load_gate_list(path: Path, *, now: dt.datetime | None = None) -> GateList:
    """Read ``gates.json`` written by the recovery tick. Refuses a file older than 15 minutes."""
    now = now or dt.datetime.now(dt.UTC)
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateListError(f"gate list missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GateListError(f"gate list unreadable: {exc}") from exc
    board = body.get("board") if isinstance(body, dict) else None
    if not board:
        raise GateListError("gate list names no board")
    try:
        generated = dt.datetime.fromisoformat(str(body.get("generated_at")))
    except ValueError as exc:
        raise GateListError("gate list has no valid generated_at") from exc
    generated = generated if generated.tzinfo else generated.replace(tzinfo=dt.UTC)
    if now - generated > GATE_LIST_MAX_AGE:
        raise GateListError(f"gate list is too old ({generated.isoformat()}); the recovery step did not run this tick")
    cards = body.get("cards")
    if not isinstance(cards, list) or not all(isinstance(c, dict) and c.get("id") for c in cards):
        raise GateListError("gate list cards must be a list of objects with an id")
    return {str(board): {str(c["id"]): c for c in cards}}


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.UTC).replace(microsecond=0).isoformat()
    return str(value)


def _epoch(value: str | None) -> float:
    if not value:
        return 0.0
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)).timestamp()


def _reason(payload: str | None) -> str:
    import json

    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return ""
    return str(data.get("reason") or "") if isinstance(data, dict) else ""


def _card_cycles(snapshot) -> dict[str, tuple[str, str]]:
    """card id -> (initiative, cycle) for every delivery or assessment card linked from a cycle file.

    A delivery link wins over an assessment link when a card appears in both places.
    """
    owners: dict[str, tuple[str, str]] = {}
    for field in ("kanban_cards", "assessment_cards"):
        for cycle_id, doc in snapshot.cycles.items():
            initiative = snapshot.cycle_initiative.get(cycle_id)
            for card in as_list(doc.frontmatter.get(field)):
                owners.setdefault(str(card), (initiative, cycle_id))
    return owners


def _kanban_gates(
    project: ProjectSettings, owners: dict[str, tuple[str, str]], listed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """One candidate per card on the gate list that is still on the board and not finished."""
    board = str(project.kanban_board)
    conn = kanban._connect(kanban.board_path(board))
    try:
        marks = ",".join("?" * len(listed))
        rows = (
            conn.execute(
                "SELECT id, title, body, status, assignee, block_kind, block_recurrences, created_at FROM tasks "
                f"WHERE id IN ({marks}) AND status NOT IN ({','.join('?' * len(DONE_STATUSES))})",
                (*listed, *DONE_STATUSES),
            ).fetchall()
            if listed
            else []
        )
        marks = ",".join("?" * len(GATE_EVENT_KINDS))
        candidates = []
        for row in rows:
            card = row["id"]
            newest = conn.execute(
                f"SELECT id, created_at FROM task_events WHERE task_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
                (card, *GATE_EVENT_KINDS),
            ).fetchone()
            why = conn.execute(
                "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? "
                "AND kind IN ('blocked', 'scheduled', 'block_loop_detected', 'status') ORDER BY id DESC LIMIT 1",
                (card,),
            ).fetchone()
            children = [
                r[0]
                for r in conn.execute(
                    "SELECT l.child_id FROM task_links l JOIN tasks t ON t.id = l.child_id "
                    f"WHERE l.parent_id = ? AND t.status NOT IN ({','.join('?' * len(DONE_STATUSES))}) ORDER BY l.child_id",
                    (card, *DONE_STATUSES),
                )
            ]
            runs = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 20", (card,)).fetchall()
            contract = conn.execute("SELECT completion_contract FROM tasks WHERE id = ?", (card,)).fetchone()[0]
            pr = kanban.pr_info(contract, runs)
            initiative, cycle = owners.get(card, (None, None))
            entry = listed.get(card) or {}
            reason = str(entry.get("waiting") or "") or (_reason(why["payload"]) if why else "")
            signals = ", ".join(str(s) for s in entry.get("signals") or [])
            comment = conn.execute(
                "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (card,),
            ).fetchone()
            latest = f"\nLatest comment by {comment['author']}: {comment['body']}" if comment else ""
            links = [{"kind": "card", "board": board, "id": card}]
            if pr["url"]:
                links.append({"kind": "pr", "url": pr["url"], "state": pr["state"]})
            # The documents the ask cites (a plan to approve, a report), so the Review tab can preview them.
            texts = [comment["body"] if comment else "", reason, row["body"] or ""]
            links.extend({"kind": "doc", "path": d["path"]} for d in cited_docs(project, texts))
            candidates.append(
                {
                    "key": f"kanban:{board}:{card}:gate",
                    "source": "kanban",
                    "title": f"{card} {row['title']}"[:300],
                    "basis": {
                        "board": board,
                        "card": card,
                        "status": row["status"],
                        "block_kind": row["block_kind"],
                        "event": newest["id"] if newest else None,
                    },
                    "blocks": children,
                    "links": links,
                    "opened_at": _iso(why["created_at"] if why else row["created_at"]),
                    "project": project.id,
                    "initiative": initiative,
                    "cycle": cycle,
                    "owner_profile": row["assignee"],
                    "context": (
                        f"Card {card} ({row['status']}, block kind {row['block_kind'] or 'none'}, "
                        f"assignee {row['assignee'] or 'none'}" + (f"; {signals}" if signals else "") + f").\n{reason}{latest}"
                    )[:CONTEXT_LIMIT],
                }
            )
        return candidates
    finally:
        conn.close()


def _outcome_gate(project: ProjectSettings, initiative: str, cycle: dict[str, Any]) -> dict[str, Any] | None:
    loop = cycle["outcome_loop"]
    state = loop["state"]
    if state not in ("review_ready", "assessment_failed", "source_unavailable"):
        return None
    receipt = loop.get("assessment") or {}
    links = [{"kind": "note", "path": cycle["path"]}]
    for name in ("evidence", "proposal"):
        if receipt.get(name):
            links.append({"kind": "doc", "path": receipt[name]})
    context = (
        f"Outcome assessment: {state}; epoch {loop['epoch']}; due {loop.get('due_date') or 'not agreed'}. "
        f"Contract gaps: {', '.join(loop.get('contract_gaps', [])) or 'none'}. "
        f"Finding (not a user verdict): {receipt.get('status') or 'no usable receipt'}. "
        f"{receipt.get('detail') or loop.get('next_action', '')}. "
        f"Assessment card: {loop.get('assessment_card') or 'none'}. "
        "Read the evidence and bounded proposal before offering a verdict, disposition or new plan. "
        "Do not ask the user to approve routine measurement work. Do not treat missing evidence as success."
    )
    return {
        "key": f"cycle:{project.id}:{cycle['id']}:gate",
        "source": "cycle",
        "title": f"{cycle['id']} outcome review",
        "basis": {
            "cycle": cycle["id"],
            "stage": cycle["stage"],
            "reasons": ["outcome_" + state],
            "epoch": loop["epoch"],
            "receipt": receipt.get("at"),
            "finding": receipt.get("status"),
            "artifacts": receipt.get("artifact_sha256"),
            "verdict": (cycle.get("result") or {}).get("verdict"),
            "disposition": (cycle.get("disposition") or {}).get("action"),
        },
        "blocks": [],
        "links": links,
        "opened_at": None,
        "project": project.id,
        "initiative": initiative,
        "cycle": cycle["id"],
        "owner_profile": None,
        "deadline": loop.get("due_date"),
        "context": context[:CONTEXT_LIMIT],
    }


def _measurement_ask(cycle: dict[str, Any], errors: list[str]) -> str:
    """What the cycle review item tells the user to record, at admission or after a release."""
    release_state = (cycle.get("release") or {}).get("state")
    admitted = release_state != "verified" and cycle["stage"] != "measuring"
    moment = "Admission needs a measurement plan" if admitted else "The release needs a measurement plan"
    return (
        f"{moment}; missing: "
        + "; ".join(errors)
        + ". Answer with set_cycle_fields naming the fields (observation_window as "
        "{starts, length_days}; none needed for a spot-check with success_criteria). "
    )


def _admission_gate(
    project: ProjectSettings, initiative_id: str, cycle: dict[str, Any], *, criteria_gate_covers: bool
) -> dict[str, Any] | None:
    """Ask for the measurement contract before delivery starts, as its own item.

    Card gates keep exact ``reasons`` (``["no_cards"]``, ``["cards_done"]``, ...) because settle rules match them
    strictly, so admission cannot ride on them: it needs its own key and its own single-reason basis. A cycle that
    already lacks ``success_criteria`` is left to the initiative's criteria gate first, so one gap keeps one item.
    """
    stage = cycle["stage"]
    if stage == "measuring" or (cycle.get("release") or {}).get("state") == "verified":
        return None
    if stage not in ("decided", "shipping"):
        return None
    if criteria_gate_covers and "criteria_missing" in set(cycle.get("flags") or []):
        return None
    errors = list(measurement_problems(cycle.get("measurement")).values())
    if not errors:
        return None
    return {
        "key": f"cycle:{project.id}:{cycle['id']}:measure",
        "source": "cycle",
        "title": f"{cycle['id']} {cycle.get('title') or ''}".strip()[:300],
        "basis": {
            "cycle": cycle["id"],
            "stage": stage,
            "reasons": ["measurement_missing"],
            "verdict": (cycle.get("result") or {}).get("verdict"),
            "disposition": (cycle.get("disposition") or {}).get("action"),
            "measurement_errors": errors,
        },
        "blocks": [],
        "links": [{"kind": "note", "path": cycle["path"]}],
        "opened_at": None,
        "project": project.id,
        "initiative": initiative_id,
        "cycle": cycle["id"],
        "owner_profile": None,
        "context": (
            f"Cycle {cycle['id']} in {initiative_id}: stage {stage}; needs measurement_missing. "
            + _measurement_ask(cycle, errors)
            + f"Next action: {cycle.get('next_action') or '-'}. Question: {cycle.get('question') or '-'}. "
            f"Hypothesis: {cycle.get('hypothesis') or '-'}."
        )[:CONTEXT_LIMIT],
    }


def _cycle_gates(project: ProjectSettings, snapshot, today: dt.date) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for initiative_id, doc in snapshot.initiatives.items():
        if str(doc.frontmatter.get("status", "active")) == "closed":
            continue
        detail = build_detail(project, initiative_id, today=today) or {"cycles": []}
        missing: list[str] = []
        for cycle in detail["cycles"]:
            flags = set(cycle["flags"])
            stage = cycle["stage"]
            loop = cycle.get("outcome_loop") or {}
            if loop.get("enabled") and stage != "exploring":
                candidate = _outcome_gate(project, initiative_id, cycle)
                if candidate:
                    candidates.append(candidate)
                    continue
                # The outcome review has nothing to ask yet; admission still does.
                admission = _admission_gate(project, initiative_id, cycle, criteria_gate_covers=False)
                if admission:
                    candidates.append(admission)
                continue
            if "criteria_missing" in flags:
                missing.append(cycle["id"])
            reasons = []
            if stage == "exploring":
                cards = cycle["cards"]
                if any(c.get("status") not in DONE_STATUSES for c in cards if c.get("status")):
                    # An agent is planning or analysing this cycle; it returns when that work finishes.
                    continue
                reasons.append("exploring")
                done = sorted(c["id"] for c in cards if c.get("status") in DONE_STATUSES)
                if done:
                    reasons.append("plan_ready")
            reasons.extend(
                sorted(flags & {"evaluation_due", "no_next_action", "reevaluation_due", "verdict_due", "disposition_due"})
            )

            # A verified release with no measurement plan is a release nobody can judge: that rides on the card gate.
            # Admission is asked separately (``:measure``) so these bases stay exact for the settle rules.
            verified = stage == "measuring" or cycle["release"]["state"] == "verified"
            measurement_errors = list(measurement_problems(cycle.get("measurement")).values()) if verified else []
            if measurement_errors:
                reasons.append("measurement_missing")
            admission = _admission_gate(project, initiative_id, cycle, criteria_gate_covers=True)
            if admission:
                candidates.append(admission)
            finished: list[str] = []
            if stage in ("decided", "shipping"):
                done, last = cards_done(cycle["cards"])
                if not cycle["cards"]:
                    reasons.append("no_cards")
                elif done and (stage == "decided" or (last and (today - last).days > RELEASE_GRACE_DAYS)):
                    # Every card finished and nothing moved the cycle on: a decided cycle needs its next card or a
                    # stage change now; a shipping cycle gets the release grace week first (as release_unverified).
                    reasons.append("cards_done")
                    finished = sorted(c["id"] for c in cycle["cards"])
            if not reasons:
                continue
            basis = {
                "cycle": cycle["id"],
                "stage": stage,
                "reasons": reasons,
                "verdict": (cycle.get("result") or {}).get("verdict"),
                "disposition": (cycle.get("disposition") or {}).get("action"),
            }
            if measurement_errors:
                basis["measurement_errors"] = measurement_errors
            if finished:
                basis["cards_done"] = finished
            if "reevaluation_due" in reasons or "disposition_due" in reasons:
                # Answering "keep waiting" sets a new window; when that one ends the item must ask again, so the
                # date is part of the basis (only here, so other cycle items keep their applied hashes).
                basis["evaluation_date"] = cycle.get("evaluation_date")
            candidates.append(
                {
                    "key": f"cycle:{project.id}:{cycle['id']}:gate",
                    "source": "cycle",
                    "title": f"{cycle['id']} {cycle.get('title') or ''}".strip()[:300],
                    "basis": basis,
                    "blocks": [c["id"] for c in cycle["cards"] if c.get("status") not in DONE_STATUSES],
                    "links": [{"kind": "note", "path": cycle["path"]}],
                    "opened_at": None,
                    "project": project.id,
                    "initiative": initiative_id,
                    "cycle": cycle["id"],
                    "owner_profile": None,
                    "context": (
                        f"Cycle {cycle['id']} in {initiative_id}: stage {stage}; needs {', '.join(reasons)}. "
                        + (_measurement_ask(cycle, measurement_errors) if "measurement_missing" in reasons else "")
                        + (
                            f"All {len(finished)} cards are done ({', '.join(finished)}); release "
                            f"{cycle['release']['state']}; flags {', '.join(sorted(flags)) or 'none'}. "
                            if finished
                            else ""
                        )
                        + f"Next action: {cycle['next_action']}. Question: {cycle.get('question') or '-'}. "
                        f"Hypothesis: {cycle.get('hypothesis') or '-'}."
                    )[:CONTEXT_LIMIT],
                }
            )
        if missing:
            candidates.append(
                {
                    "key": f"criteria:{project.id}:{initiative_id}",
                    "source": "criteria",
                    "title": f"Success criteria missing in {initiative_id} ({len(missing)} cycles)",
                    # "field" names what closes the gate. It also changed the basis hash, so criteria answers that were
                    # recorded only as decision rows (and dismissed) come back once.
                    "basis": {"initiative": initiative_id, "cycles": sorted(missing), "field": "measurement.success_criteria"},
                    "blocks": [],
                    "links": [{"kind": "note", "path": detail.get("path")}],
                    "opened_at": None,
                    "project": project.id,
                    "initiative": initiative_id,
                    "cycle": None,
                    "owner_profile": None,
                    "context": (
                        f"These shipping or measuring cycles have no success criteria, so no verdict is possible: "
                        f"{', '.join(sorted(missing))}. The gate closes only when measurement.success_criteria is set "
                        f"(set_cycle_fields with fields.success_criteria); a decision row alone does not close it. "
                        f"Goal: {detail.get('goal') or '-'}."
                    )[:CONTEXT_LIMIT],
                }
            )
    return candidates


def _idea_gates(project: ProjectSettings, snapshot) -> list[dict[str, Any]]:
    candidates = []
    for idea_id, doc in snapshot.ideas.items():
        fm = doc.frontmatter
        if str(fm.get("status")) not in OPEN_IDEA_STATUSES:
            continue
        candidates.append(
            {
                "key": f"idea:{project.id}:{idea_id}:triage",
                "source": "idea",
                "title": f"Idea: {fm.get('title') or idea_id}"[:300],
                "basis": {"idea": idea_id, "status": fm.get("status"), "similar_to": as_list(fm.get("similar_to"))},
                "blocks": [],
                "links": [{"kind": "note", "path": str(doc.path)}],
                "opened_at": _iso(str(fm.get("created"))) if fm.get("created") else None,
                "project": project.id,
                "initiative": fm.get("related_initiative"),
                "cycle": None,
                "owner_profile": None,
                "context": (
                    f"Idea {idea_id} ({fm.get('size') or 'cycle'}) from {fm.get('source') or 'unknown'}. "
                    f"Problem: {fm.get('problem') or '-'}. Expected outcome: {fm.get('expected_outcome') or '-'}. "
                    f"Evidence: {', '.join(map(str, as_list(fm.get('evidence')))) or '-'}. "
                    f"Similar: {', '.join(map(str, as_list(fm.get('similar_to')))) or 'none'}."
                )[:CONTEXT_LIMIT],
            }
        )
    return candidates


def collect_project(project: ProjectSettings, today: dt.date | None = None, gates: GateList | None = None) -> dict[str, Any]:
    """Every gate for one project. ``ok`` names the sources that were read completely.

    ``gates`` is the recovery tick's gate list. A board with no entry in it is not read: its items neither open
    nor resolve, and the error says why."""
    today = today or dt.date.today()
    snapshot = load(project.root)
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    ok: set[str] = set()
    if project.kanban_board:
        listed = (gates or {}).get(project.kanban_board)
        if listed is None:
            errors.append(f"{project.id}: no current gate list for board '{project.kanban_board}'; Kanban items unchanged")
        else:
            try:
                candidates.extend(_kanban_gates(project, _card_cycles(snapshot), listed))
                ok.add("kanban")
            except (ImportError, OSError, sqlite3.Error) as exc:
                errors.append(f"{project.id}: Kanban board '{project.kanban_board}' unreadable: {exc}")
    candidates.extend(_cycle_gates(project, snapshot, today))
    candidates.extend(_idea_gates(project, snapshot))
    ok.update({"cycle", "criteria", "idea"})
    if snapshot.parse_errors:
        errors.extend(f"{project.id}: {path}: {err}" for path, err in snapshot.parse_errors.items())
    return {"project": project.id, "candidates": candidates, "errors": errors, "ok": sorted(ok)}


def sync(
    store: ReviewStore, projects: list[ProjectSettings], today: dt.date | None = None, gates: GateList | None = None
) -> dict[str, Any]:
    """Bring the review store in line with the live gates. Idempotent."""
    today = today or dt.date.today()
    result: dict[str, Any] = {"opened": [], "updated": [], "resolved": [], "reopened": [], "errors": [], "candidates": 0}
    seen: dict[str, dict[str, Any]] = {}
    scopes: set[tuple[str, str]] = set()
    for project in projects:
        collected = collect_project(project, today=today, gates=gates)
        result["errors"].extend(collected["errors"])
        scopes.update((project.id, source) for source in collected["ok"])
        for candidate in collected["candidates"]:
            prior = seen.get(candidate["key"])
            if prior is None or (not prior.get("initiative") and candidate.get("initiative")):
                seen[candidate["key"]] = candidate
    result["candidates"] = len(seen)
    hashes = {key: basis_hash(c["basis"]) for key, c in seen.items()}
    for key, c in seen.items():
        if store.get_by_key(key) is None and store.is_dismissed(key, hashes[key]):
            # Answered and applied with this exact basis; it returns only when the source changes.
            continue
        item, created = store.upsert_item(
            key,
            section="gated",
            source=c["source"],
            title=c["title"],
            basis=c["basis"],
            blocks=c["blocks"],
            links=c["links"],
            opened_at=c["opened_at"],
            project=c["project"],
            initiative=c["initiative"],
            cycle=c["cycle"],
            owner_profile=c["owner_profile"],
            evidence_hash=basis_hash(c["context"]),
            deadline=c.get("deadline"),
        )
        store.set_context(item["short_id"], c["context"])
        (result["opened"] if created else result["updated"]).append(key)
    # ``failed`` and ``awaiting_confirm`` hold a stale answer for the user; when the source closes they close too
    # (D135/D107/D211 stayed open for weeks after their cards finished).
    for item in store.items(statuses=("open", "deferred", "watching", "auto", "failed", "awaiting_confirm")):
        if item["section"] != "gated" or item["source"] not in COLLECTED_SOURCES or item["key"] in seen:
            continue
        if (item["project"], item["source"]) not in scopes:
            continue
        if item["source"] == "kanban":
            board, card = item["basis"].get("board"), item["basis"].get("card")
            if board not in (gates or {}):
                result["errors"].append(f"{item['short_id']}: no current gate list for stored board {board!r}")
                continue
            proof, error = kanban_source_proof(board, card)
            if proof is SourceProof.UNAVAILABLE:
                result["errors"].append(f"{item['short_id']}: Kanban source unavailable: {error}")
                continue
            if proof is SourceProof.NON_TERMINAL:
                project = next(p for p in projects if p.id == item["project"])
                try:
                    retained = _kanban_gates(project.model_copy(update={"kanban_board": board}), {}, {card: {"id": card}})
                except (ImportError, OSError, sqlite3.Error) as exc:
                    result["errors"].append(f"{item['short_id']}: Kanban source unreadable: {exc}")
                    continue
                if retained:
                    c = retained[0]
                    hashes[item["key"]] = basis_hash(c["basis"])
                    store.refresh_collected_source(
                        item["short_id"],
                        c["basis"],
                        "Gate-list omission does not prove source completion.\n" + c["context"],
                        c["owner_profile"],
                    )
                    result["updated"].append(item["key"])
                continue
        if store.close_item(item["short_id"], "resolved_elsewhere", note="The gate closed in its source system."):
            result["resolved"].append(item["key"])
    for short_id in store.due_deferrals(today=today, basis_hashes=hashes):
        if store.reopen(short_id):
            result["reopened"].append(short_id)
    # A park whose own condition is not the item's basis (``defer_behind_focus_cycle``) returns here: its wait is
    # the initiative's focus cycle, which the collector basis does not carry. Imported here to avoid an import cycle.
    from .review_settle import return_focus_waits

    result["reopened"].extend(return_focus_waits(store, projects))
    result["reopened"].extend(store.reopen_changed_watches())
    result["reopened"].extend(reopen_finished_owner_watches(store, projects, result["errors"]))
    result["reopened"].extend(reopen_parked_cards_with_finished_parents(store, projects, result["errors"]))
    return result


WATCH_CARD = re.compile(r"\bt_[0-9a-f]{8}\b")
OWNER_GROUNDS = ("agent_owned:", "waiting_on:")


def reopen_finished_owner_watches(store: ReviewStore, projects: list[ProjectSettings], errors: list[str]) -> list[str]:
    """Reopen a watch whose ground named owner cards that have all finished.

    ``agent_owned:`` and ``waiting_on:`` watches name the card that does the next step or must change first. The
    item's basis tracks only its own gate card, so when that owner card finishes and the gate card does not move,
    nothing would bring the item back. Every named card other than the item's own must exist on the item's board
    and be done or archived. A card that is missing or unreadable keeps the watch."""
    reopened: list[str] = []
    boards = {str(p.kanban_board) for p in projects if p.kanban_board}
    for item in store.items(statuses=("watching",)):
        reason = str(item.get("watch_reason") or "")
        if not reason.startswith(OWNER_GROUNDS):
            continue
        board = (item.get("basis") or {}).get("board") or next(
            (str(p.kanban_board) for p in projects if p.id == item.get("project") and p.kanban_board), None
        )
        if board not in boards:
            continue
        named = sorted(set(WATCH_CARD.findall(reason)) - {(item.get("basis") or {}).get("card")})
        if not named:
            continue
        source = kanban.read_cards(board, named)
        if source["state"] != "fresh":
            errors.append(f"{item['short_id']}: watch owner cards unreadable: {source['error']}")
            continue
        cards = source["cards"]
        if any(cards[c].get("state") != "fresh" or cards[c].get("status") not in DONE_STATUSES for c in named):
            continue
        version = (item.get("version") or {}).get("n")
        finished = ", ".join(f"{c} {cards[c]['status']}" for c in named)
        if store.reopen_watch(item["short_id"], version, f"watch ground no longer holds: {finished}"):
            reopened.append(item["short_id"])
    return reopened


PARKED_STATUSES = ("blocked", "scheduled")


def reopen_parked_cards_with_finished_parents(
    store: ReviewStore, projects: list[ProjectSettings], errors: list[str]
) -> list[str]:
    """Reopen a watch on a parked card once every parent card has finished.

    A card created parked (``initial_status=blocked``) does not move when its parents finish, so its basis never
    changes and its watch never returns. The step it holds ("file the consent package when prerequisites exist")
    then waits for nobody. Each set of finished parents reopens the item once; a re-watch for the same set stays."""
    reopened: list[str] = []
    boards = {str(p.kanban_board) for p in projects if p.kanban_board}
    for item in store.items(statuses=("watching",)):
        basis = item.get("basis") or {}
        board, card = basis.get("board"), basis.get("card")
        if item.get("source") != "kanban" or board not in boards or not card:
            continue
        try:
            conn = kanban._connect(kanban.board_path(str(board)))
            try:
                own = conn.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
                parents = conn.execute(
                    "SELECT t.id, t.status, t.completed_at FROM task_links l JOIN tasks t ON t.id = l.parent_id "
                    "WHERE l.child_id = ? ORDER BY t.id",
                    (card,),
                ).fetchall()
            finally:
                conn.close()
        except (ImportError, OSError, sqlite3.Error) as exc:
            errors.append(f"{item['short_id']}: parent cards unreadable: {exc}")
            continue
        if own is None or own["status"] not in PARKED_STATUSES or not parents:
            continue
        if any(p["status"] not in DONE_STATUSES for p in parents):
            continue
        finished = [f"{p['id']}@{p['completed_at'] or p['status']}" for p in parents]
        seen = [json.loads(a.get("args_json") or "{}") for a in store.actions(item["short_id"])]
        if any(a.get("from") == "parents_done" and a.get("parents") == finished for a in seen):
            continue
        version = (item.get("version") or {}).get("n")
        reason = f"all parent cards finished ({', '.join(p['id'] for p in parents)}); {card} is still {own['status']}"
        if store.set_status(item["short_id"], "open", expect=("watching",)):
            store.log_action(
                None,
                item["short_id"],
                "reopen_deferral",
                {"version": version, "from": "parents_done", "parents": finished},
                "reopened",
                receipt=reason,
            )
            reopened.append(item["short_id"])
    return reopened


def finished_owner_cards(item: dict[str, Any], reason: str, board: str | None) -> list[str] | None:
    """For a new ``agent_owned:``/``waiting_on:`` watch: the named owner cards when every one is already finished
    (the watch would reopen at once), else None. Unreadable sources return None; the sync check fails closed."""
    if not reason.startswith(OWNER_GROUNDS) or not board:
        return None
    named = sorted(set(WATCH_CARD.findall(reason)) - {(item.get("basis") or {}).get("card")})
    if not named:
        return None
    source = kanban.read_cards(board, named)
    if source["state"] != "fresh":
        return None
    cards = source["cards"]
    if all(cards[c].get("state") == "fresh" and cards[c].get("status") in DONE_STATUSES for c in named):
        return named
    return None


def updates_since(project: ProjectSettings, since: str | None) -> dict[str, Any]:
    """Material changes since *since* (ISO time): card lifecycle events, observations, and recorded decisions."""
    since_epoch = _epoch(since)
    since_day = (since or "")[:10]
    snapshot = load(project.root)
    owners = _card_cycles(snapshot)
    by_initiative: dict[str, list[dict[str, Any]]] = {}
    unlinked: list[dict[str, Any]] = []
    errors: list[str] = []
    if project.kanban_board:
        try:
            conn = kanban._connect(kanban.board_path(str(project.kanban_board)))
            try:
                marks = ",".join("?" * len(UPDATE_EVENT_KINDS))
                rows = conn.execute(
                    f"SELECT e.task_id, e.kind, e.payload, e.created_at, t.title, t.status FROM task_events e "
                    f"JOIN tasks t ON t.id = e.task_id WHERE e.kind IN ({marks}) AND e.created_at >= ? "
                    f"ORDER BY e.id DESC LIMIT ?",
                    (*UPDATE_EVENT_KINDS, since_epoch, UPDATE_LIMIT),
                ).fetchall()
            finally:
                conn.close()
            for row in reversed(rows):
                entry = {
                    "card": row["task_id"],
                    "title": row["title"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "at": _iso(row["created_at"]),
                    "detail": _reason(row["payload"])[:300],
                }
                owner = owners.get(row["task_id"])
                if owner and owner[0]:
                    entry["cycle"] = owner[1]
                    by_initiative.setdefault(owner[0], []).append(entry)
                else:
                    unlinked.append(entry)
        except (ImportError, OSError, sqlite3.Error) as exc:
            errors.append(f"Kanban unreadable: {exc}")
    for cycle_id, initiative in snapshot.cycle_initiative.items():
        for row in active_observations(snapshot.observations.get(cycle_id)):
            at = str(row.get("at") or "")
            if at and at >= (since or ""):
                by_initiative.setdefault(initiative, []).append(
                    {
                        "cycle": cycle_id,
                        "kind": f"observation:{row.get('kind')}",
                        "at": at,
                        "detail": str(row.get("detail") or row.get("value") or "")[:300],
                    }
                )
        for decision in as_list(snapshot.cycles[cycle_id].frontmatter.get("decisions")):
            if isinstance(decision, dict) and str(decision.get("date") or "") >= since_day and since_day:
                by_initiative.setdefault(initiative, []).append(
                    {
                        "cycle": cycle_id,
                        "kind": "decision",
                        "at": str(decision.get("date")),
                        "detail": str(decision.get("decision") or "")[:300],
                    }
                )
    return {"project": project.id, "by_initiative": by_initiative, "unlinked": unlinked, "errors": errors}


def summaries(project: ProjectSettings, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Facts for each active initiative's 2-3 line summary (the generator writes the prose)."""
    from .views import build_list

    listing = build_list(project, today=today)
    rows = []
    for initiative in listing["initiatives"]:
        if initiative["status"] == "closed":
            continue
        focus = next((c for c in initiative["open_cycles"] if c["id"] == initiative.get("focus_cycle")), None)
        focus = focus or (initiative["open_cycles"][0] if initiative["open_cycles"] else None)
        rows.append(
            {
                "id": initiative["id"],
                "title": initiative["title"],
                "status": initiative["status"],
                "goal": initiative["goal"],
                "flags": initiative["flags"],
                "open_cycles": len(initiative["open_cycles"]),
                "focus": {"id": focus["id"], "stage": focus["stage"], "next_action": focus["next_action"]} if focus else None,
                "next_evaluation": initiative["next_evaluation"],
            }
        )
    return rows
