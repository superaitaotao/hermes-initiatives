"""Response shapes for the dashboard: list summaries, initiative detail, ideas, and session context."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .kanban import cycle_markers, read_card_detail, read_cards
from .lifecycle import (
    DEFAULT_OBSERVE_EVERY_DAYS,
    as_list,
    cycle_flags,
    effective_stage,
    evaluation_date,
    next_action,
    release_state,
)
from .model import (
    BUG_SEVERITIES,
    BUG_STATUSES,
    LEGACY_TASK_STATUSES,
    OPEN_BUG_STATUSES,
    OPEN_IDEA_STATUSES,
    OPEN_STAGES,
    OPEN_TASK_STATUSES,
    STAGES,
    TASK_PRIORITIES,
    TASK_STATUSES,
    TASK_TYPES,
)
from .settings import ProjectSettings
from .store import Snapshot, active_observations, load
from .validate import Report, validate

CONTEXT_LIMIT = 12_000
DOC_MAX_BYTES = 1_000_000
CITED_DOC_LIMIT = 6
_ABS_MD_PATH = re.compile(r"(?<![\w/.~-])/[^\s`'\"()<>\[\]{}|*]+?\.md\b")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _rel(path: Path, project: ProjectSettings) -> str:
    base = project.vault_root or project.root
    try:
        return str(path.relative_to(base))
    except ValueError:
        pass
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def _observe_days(initiative_fm: dict[str, Any]) -> int:
    value = initiative_fm.get("observe_every_days")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else DEFAULT_OBSERVE_EVERY_DAYS


def _cycle_view(
    snapshot: Snapshot,
    report: Report,
    cycle_id: str,
    cards: dict[str, Any],
    kanban_state: str,
    derived: dict[str, list[str]],
    observe_days: int,
    project: ProjectSettings,
    today: dt.date,
    full: bool,
) -> dict[str, Any]:
    doc = snapshot.cycles[cycle_id]
    fm = doc.frontmatter
    rows = active_observations(snapshot.observations.get(cycle_id))
    release = release_state(rows)
    stage = effective_stage(fm, release)
    eval_on = evaluation_date(fm, release)
    card_rows = [
        {**cards.get(card, {"id": card, "state": kanban_state}), "link": link}
        for card, link in _cycle_cards(fm, derived.get(cycle_id, []))
    ]
    from .outcome_loop import derive

    assessment_rows = [cards.get(c, {"id": c, "state": kanban_state}) for c in as_list(fm.get("assessment_cards"))]
    outcome = derive(fm, rows, card_rows, assessment_rows, today)
    errors = report.errors.get(cycle_id, []) + report.errors.get(f"observations/{cycle_id}.jsonl", [])
    view = {
        "id": cycle_id,
        "title": fm.get("title"),
        "stage": stage,
        "recorded_stage": fm.get("stage"),
        "next_action": next_action(fm, stage, release, eval_on, card_rows, today),
        "flags": cycle_flags(fm, stage, release, eval_on, rows, card_rows, observe_days, bool(errors), today),
        "evaluation_date": eval_on.isoformat() if eval_on else None,
        "release": {"state": release["state"], "date": release["date"]},
        "cards": card_rows,
        "assessment_cards": assessment_rows,
        "outcome_loop": outcome,
        "path": _rel(doc.path, project),
        "errors": errors,
        "warnings": report.warnings.get(cycle_id, []),
    }
    if full:
        view.update(
            {
                "outcome_review": fm.get("outcome_review") if isinstance(fm.get("outcome_review"), dict) else {},
                "question": fm.get("question"),
                "hypothesis": fm.get("hypothesis"),
                "delivery_plans": as_list(fm.get("delivery_plans")),
                "change_register": as_list(fm.get("change_register")),
                "decisions": as_list(fm.get("decisions")),
                "measurement": fm.get("measurement") or {},
                "result": fm.get("result") or {},
                "disposition": fm.get("disposition") or {},
                "sources": as_list(fm.get("sources")),
                "observations": rows,
            }
        )
    if outcome["enabled"]:
        view["next_action"] = outcome["next_action"]
        view["flags"].extend(outcome.get("reasons", []))
    return _jsonable(view)


def _cycle_cards(fm: dict[str, Any], derived: list[str]) -> list[tuple[str, str]]:
    """Listed cards in file order, then cards whose body names this cycle and that are neither listed nor excluded."""
    listed = list(dict.fromkeys(str(c) for c in as_list(fm.get("kanban_cards"))))
    skip = set(listed) | {str(c) for c in as_list(fm.get("excluded_cards"))}
    return [(c, "listed") for c in listed] + [(c, "derived") for c in derived if c not in skip]


def _all_card_ids(snapshot: Snapshot, cycle_ids: list[str], derived: dict[str, list[str]]) -> list[str]:
    seen: dict[str, None] = {}
    for cycle_id in cycle_ids:
        fm = snapshot.cycles[cycle_id].frontmatter
        for card, _ in _cycle_cards(fm, derived.get(cycle_id, [])):
            seen[card] = None
        for card in as_list(fm.get("assessment_cards")):
            seen[str(card)] = None
    return list(seen)


def _kanban(project: ProjectSettings, snapshot: Snapshot, cycle_ids: list[str]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    markers = cycle_markers(project.kanban_board) if project.kanban_board else {"state": "fresh", "markers": {}}
    derived = markers["markers"]
    kanban = read_cards(project.kanban_board, _all_card_ids(snapshot, cycle_ids, derived))
    if markers["state"] != "fresh" and kanban["state"] == "fresh":
        kanban = {**kanban, "state": markers["state"], "error": markers.get("error")}
    return kanban, derived


def _cycles_of(snapshot: Snapshot, initiative_id: str) -> list[str]:
    return sorted(c for c, owner in snapshot.cycle_initiative.items() if owner == initiative_id)


def _order(cycles: list[dict[str, Any]], focus: str | None) -> list[dict[str, Any]]:
    def key(cycle: dict[str, Any]):
        return (
            cycle["id"] != focus,
            cycle["stage"] not in OPEN_STAGES,
            -STAGES.index(cycle["stage"]) if cycle["stage"] in OPEN_STAGES else 0,
            cycle["id"],
        )

    return sorted(cycles, key=key)


def build_list(project: ProjectSettings, today: dt.date | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    today = today or dt.date.today()
    snapshot = load(project.root)
    report = validate(snapshot, project.vault_root)
    open_ids = [c for c, d in snapshot.cycles.items() if str(d.frontmatter.get("stage")) in OPEN_STAGES]
    kanban, derived = _kanban(project, snapshot, open_ids)
    initiatives = []
    for initiative_id, doc in snapshot.initiatives.items():
        fm = doc.frontmatter
        observe = _observe_days(fm)
        cycles = [
            _cycle_view(snapshot, report, cid, kanban["cards"], kanban["state"], derived, observe, project, today, False)
            for cid in _cycles_of(snapshot, initiative_id)
            if cid in open_ids
        ]
        dates = [c["evaluation_date"] for c in cycles if c["evaluation_date"]]
        initiatives.append(
            _jsonable(
                {
                    "id": initiative_id,
                    "title": fm.get("title"),
                    "status": fm.get("status", "active"),
                    "rank": fm.get("rank"),
                    "pilot": bool(fm.get("pilot")),
                    "goal": fm.get("goal"),
                    "focus_cycle": fm.get("focus_cycle"),
                    "open_cycles": _order(cycles, fm.get("focus_cycle")),
                    "flags": sorted(
                        {f for c in cycles for f in c["flags"]} | ({"invalid"} if report.errors.get(initiative_id) else set())
                    ),
                    "next_evaluation": min(dates) if dates else None,
                    "errors": report.errors.get(initiative_id, []),
                }
            )
        )
    initiatives.sort(
        key=lambda i: (i["status"] != "active", i["rank"] if isinstance(i["rank"], int) else 10_000, i["title"] or "")
    )
    return {
        "project": project.id,
        "initiatives": initiatives,
        "open_ideas": sum(1 for d in snapshot.ideas.values() if str(d.frontmatter.get("status")) in OPEN_IDEA_STATUSES),
        "other_folders": snapshot.other_folders,
        "validation": {
            "ok": report.ok,
            "error_count": sum(len(v) for v in report.errors.values()),
            "unattached": {
                k: v
                for k, v in report.errors.items()
                if k not in snapshot.initiatives and k not in snapshot.cycles and not k.startswith("observations/") and v
            },
        },
        "freshness": {"files": "fresh", "kanban": kanban["state"], "kanban_error": kanban["error"]},
        "read_at": dt.datetime.now(dt.UTC).isoformat(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def build_detail(project: ProjectSettings, initiative_id: str, today: dt.date | None = None) -> dict[str, Any] | None:
    started = time.perf_counter()
    today = today or dt.date.today()
    snapshot = load(project.root)
    doc = snapshot.initiatives.get(initiative_id)
    if doc is None:
        return None
    report = validate(snapshot, project.vault_root)
    fm = doc.frontmatter
    cycle_ids = _cycles_of(snapshot, initiative_id)
    kanban, derived = _kanban(project, snapshot, cycle_ids)
    observe = _observe_days(fm)
    cycles = [
        _cycle_view(snapshot, report, cid, kanban["cards"], kanban["state"], derived, observe, project, today, True)
        for cid in cycle_ids
    ]
    ideas = [
        {"id": i, "title": d.frontmatter.get("title"), "status": d.frontmatter.get("status")}
        for i, d in snapshot.ideas.items()
        if d.frontmatter.get("related_initiative") == initiative_id
    ]
    return _jsonable(
        {
            "project": project.id,
            "id": initiative_id,
            "title": fm.get("title"),
            "status": fm.get("status", "active"),
            "rank": fm.get("rank"),
            "pilot": bool(fm.get("pilot")),
            "goal": fm.get("goal"),
            "focus_cycle": fm.get("focus_cycle"),
            "observe_every_days": observe,
            "watchers": as_list(fm.get("watchers")),
            "decisions": as_list(fm.get("decisions")),
            "sources": as_list(fm.get("sources")),
            "body": doc.body.strip()[:4000],
            "path": _rel(doc.path, project),
            "cycles": _order(cycles, fm.get("focus_cycle")),
            "ideas": ideas,
            "errors": report.errors.get(initiative_id, []),
            "warnings": report.warnings.get(initiative_id, []),
            "freshness": {"files": "fresh", "kanban": kanban["state"], "kanban_error": kanban["error"]},
            "read_at": dt.datetime.now(dt.UTC).isoformat(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    )


def build_ideas(project: ProjectSettings) -> dict[str, Any]:
    snapshot = load(project.root)
    report = validate(snapshot, project.vault_root)
    ideas = []
    for idea_id, doc in snapshot.ideas.items():
        fm = doc.frontmatter
        ideas.append(
            _jsonable(
                {
                    **{
                        k: fm.get(k)
                        for k in (
                            "title",
                            "status",
                            "size",
                            "related_initiative",
                            "problem",
                            "expected_outcome",
                            "evidence",
                            "source",
                            "similar_to",
                            "created",
                            "promoted_to",
                            "merged_into",
                            "review_on",
                            "reason",
                        )
                    },
                    "id": idea_id,
                    "path": _rel(doc.path, project),
                    "errors": report.errors.get(idea_id, []),
                }
            )
        )
    rank = {s: i for i, s in enumerate(("proposed", "reviewing", "parked", "promoted", "merged", "rejected"))}
    ideas.sort(key=lambda i: (rank.get(str(i["status"]), 9), str(i["created"] or "")), reverse=False)
    return {"project": project.id, "ideas": ideas}


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _bounded(data: Any, limit: int, trims: list) -> str:
    """Serialize ``data``; apply each trim in order until it fits. Always valid JSON."""
    text = _dumps(data)
    for trim in trims:
        if len(text) <= limit:
            break
        trim(data)
        text = _dumps(data)
    if len(text) > limit:
        text = _dumps({"truncated": True, "note": "context over limit; read the files", "summary": str(text[: limit // 2])})
    return text


def _drop_cycle_field(field: str):
    def trim(data: dict[str, Any]) -> None:
        for cycle in data.get("cycles", []):
            cycle.pop(field, None)

    return trim


def discussion_context(project: ProjectSettings, initiative_id: str, today: dt.date | None = None) -> dict[str, Any] | None:
    detail = build_detail(project, initiative_id, today)
    if detail is None:
        return None
    trimmed = {k: detail[k] for k in ("id", "title", "status", "rank", "goal", "focus_cycle", "decisions", "path")}
    trimmed["cycles"] = [
        {
            **{
                k: c[k]
                for k in (
                    "id",
                    "title",
                    "stage",
                    "next_action",
                    "flags",
                    "evaluation_date",
                    "release",
                    "path",
                    "decisions",
                    "measurement",
                    "result",
                    "disposition",
                    "outcome_review",
                    "outcome_loop",
                )
            },
            "cards": [{k: card.get(k) for k in ("id", "title", "status", "pr")} for card in c["cards"]],
            "assessment_cards": [
                {k: card.get(k) for k in ("id", "title", "status", "assignee", "pr", "state")} for card in c["assessment_cards"]
            ],
            "recent_observations": c["observations"][-5:],
        }
        for c in detail["cycles"]
        if c["stage"] in OPEN_STAGES or c["id"] == detail["focus_cycle"]
    ]
    trimmed["concluded_cycles"] = [
        {k: c[k] for k in ("id", "title", "stage", "path")} for c in detail["cycles"] if c["stage"] not in OPEN_STAGES
    ]
    return {
        "context": _bounded(
            trimmed,
            CONTEXT_LIMIT,
            [
                _drop_cycle_field("recent_observations"),
                lambda d: [
                    c["outcome_review"].pop("collector", None) for c in d["cycles"] if isinstance(c.get("outcome_review"), dict)
                ],
                _drop_cycle_field("decisions"),
                _drop_cycle_field("measurement"),
                lambda d: d.pop("concluded_cycles", None),
            ],
        ),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        "title": detail["title"],
    }


CYCLE_CONTEXT_FIELDS = (
    "id",
    "title",
    "stage",
    "next_action",
    "flags",
    "evaluation_date",
    "release",
    "path",
    "question",
    "hypothesis",
    "decisions",
    "measurement",
    "result",
    "disposition",
    "delivery_plans",
    "outcome_review",
    "outcome_loop",
    "assessment_cards",
    "change_register",
    "sources",
)


def _initiative_header(detail: dict[str, Any]) -> dict[str, Any]:
    return {k: detail[k] for k in ("id", "title", "status", "goal", "path")}


def _cycle_brief(cycle: dict[str, Any]) -> dict[str, Any]:
    return {k: cycle[k] for k in ("id", "title", "stage", "next_action", "path")}


def subject_context(
    project: ProjectSettings, initiative_id: str, kind: str, subject: str, today: dt.date | None = None
) -> dict[str, Any] | None:
    """Context for a session about one cycle or one Kanban card of an initiative. ``None`` when either is unknown.

    A delivery or assessment card must belong to a cycle, so a launch cannot pull an unrelated card.
    """
    detail = build_detail(project, initiative_id, today)
    if detail is None:
        return None
    cycles = detail["cycles"]
    if kind == "cycle":
        cycle = next((c for c in cycles if c["id"] == subject), None)
        if cycle is None:
            return None
        data = {
            "initiative": _initiative_header(detail),
            "cycle": {
                **{k: cycle.get(k) for k in CYCLE_CONTEXT_FIELDS},
                "cards": [
                    {k: card.get(k) for k in ("id", "title", "status", "assignee", "pr", "state")} for card in cycle["cards"]
                ],
                "observations": cycle["observations"][-20:],
            },
            "other_cycles": [_cycle_brief(c) for c in cycles if c["id"] != subject],
        }
        trims = [
            lambda d: d.pop("other_cycles", None),
            lambda d: d["cycle"].__setitem__("observations", d["cycle"]["observations"][-5:]),
            lambda d: [d["cycle"].pop(k, None) for k in ("sources", "change_register", "delivery_plans")],
            lambda d: d["cycle"].pop("observations", None),
            lambda d: d["cycle"]["outcome_review"].pop("collector", None),
        ]
        title = f"{cycle['title']}"
    elif kind == "card":
        owners = [c for c in cycles if any(card.get("id") == subject for card in [*c["cards"], *c["assessment_cards"]])]
        if not owners:
            return None
        card = read_card_detail(project.kanban_board, subject)
        if card is None:
            return None
        card["comments"] = card["comments"][:10]
        card["runs"] = card["runs"][:10]
        data = {
            "initiative": _initiative_header(detail),
            "cycles": [
                {
                    k: c.get(k)
                    for k in (
                        "id",
                        "title",
                        "stage",
                        "next_action",
                        "path",
                        "question",
                        "measurement",
                        "outcome_review",
                        "outcome_loop",
                        "assessment_cards",
                    )
                }
                for c in owners
            ],
            "card": card,
        }
        trims = [
            lambda d: [c.pop(k, None) for c in d["cycles"] for k in ("measurement", "question")],
            lambda d: [c["outcome_review"].pop("collector", None) for c in d["cycles"]],
            lambda d: d["card"].__setitem__("comments", d["card"]["comments"][:3]),
            lambda d: d["card"].__setitem__("runs", d["card"]["runs"][:3]),
            lambda d: d["card"].__setitem__("body", (d["card"].get("body") or "")[:3000] or None),
            lambda d: d["card"].__setitem__("latest_summary", (d["card"].get("latest_summary") or "")[:2000] or None),
            lambda d: d["card"].__setitem__("result", None),
            lambda d: d["card"].__setitem__("comments", []),
        ]
        title = f"{subject} {card['title']}"
    else:
        return None
    return {
        "context": _bounded(data, CONTEXT_LIMIT, trims),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        "title": title,
    }


def attend_context(project: ProjectSettings, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Context for a session that walks the user through a step a card waits on. ``entry`` is the card's
    ``review_attention`` row (the ``You:`` step, the parked Review item and his last answer)."""
    card = read_card_detail(project.kanban_board, entry["card"])
    if card is None:
        return None
    card["comments"] = card["comments"][:8]
    card["runs"] = card["runs"][:5]
    data = {"waiting_on_you": entry, "card": card}
    trims = [
        lambda d: d["card"].__setitem__("runs", d["card"]["runs"][:2]),
        lambda d: d["card"].__setitem__("comments", d["card"]["comments"][:4]),
        lambda d: d["card"].__setitem__("body", (d["card"].get("body") or "")[:4000] or None),
        lambda d: d["card"].__setitem__("result", None),
        lambda d: d["card"].__setitem__("comments", d["card"]["comments"][:1]),
    ]
    return {
        "context": _bounded(data, CONTEXT_LIMIT, trims),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        # Session title. A card with a Review item leads with the item's id (D90), as the Review tab shows it.
        "title": " ".join(filter(None, [(entry.get("item") or {}).get("short_id"), "Attend:", entry["card"], card["title"]])),
    }


def blocker_context(
    project: ProjectSettings, item: dict[str, Any], answers: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Context for a session that explains one Review item: the question, options and recommendation, the
    collected context, the user's past answers, what was done, and, for a Kanban gate, the live card."""
    version = item.get("version") or {}
    basis = item.get("basis") or {}
    card = None
    if item.get("source") == "kanban" and basis.get("card"):
        card = read_card_detail(basis.get("board") or project.kanban_board, basis["card"])
        if card is not None:
            card["comments"] = card["comments"][:8]
            card["runs"] = card["runs"][:5]
    data = {
        "review_item": {
            k: item.get(k)
            for k in ("short_id", "status", "source", "title", "initiative", "cycle", "opened_at", "blocks", "links")
        }
        | {"deferral": item.get("deferral"), "watch_reason": item.get("watch_reason")},
        "question": version.get("question"),
        "options": version.get("options") or [],
        "recommend": version.get("recommend"),
        "why": version.get("why"),
        "collected_context": item.get("context"),
        "context_shown_with_options": version.get("context"),
        "my_past_answers": answers[-5:],
        "actions_taken": actions[-8:],
        "card": card,
        "documents": cited_docs(project, _item_texts(item, card)),
    }
    trims = [
        lambda d: d.__setitem__("actions_taken", d["actions_taken"][-3:]),
        lambda d: d["card"] and d["card"].__setitem__("runs", d["card"]["runs"][:2]),
        lambda d: d["card"] and d["card"].__setitem__("comments", d["card"]["comments"][:4]),
        lambda d: d["card"] and d["card"].__setitem__("body", (d["card"].get("body") or "")[:4000] or None),
        lambda d: d["card"] and d["card"].__setitem__("result", None),
        lambda d: d.__setitem__("collected_context", (d["collected_context"] or "")[:4000] or None),
        lambda d: d.__setitem__("context_shown_with_options", (d["context_shown_with_options"] or "")[:2000] or None),
        lambda d: d["card"] and d["card"].__setitem__("comments", d["card"]["comments"][:1]),
        lambda d: d["card"] and d["card"].__setitem__("body", (d["card"].get("body") or "")[:1500] or None),
        lambda d: d["card"] and d["card"].__setitem__("runs", []),
        lambda d: d.__setitem__("options", [{k: o.get(k) for k in ("id", "label", "action")} for o in d["options"]]),
    ]
    return {
        "context": _bounded(data, CONTEXT_LIMIT, trims),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        "title": f"{item['short_id']} Blocker: {version.get('question') or item['title']}"[:200],
    }


def _doc_roots(project: ProjectSettings) -> list[Path]:
    return [p.resolve() for p in dict.fromkeys(filter(None, (project.vault_root, project.root)))]


def _inside_roots(project: ProjectSettings, raw: str | Path) -> Path | None:
    """The resolved path of a Markdown file under the project's vault or root, else ``None``."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (project.vault_root or project.root) / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.suffix.lower() != ".md" or not any(resolved.is_relative_to(root) for root in _doc_roots(project)):
        return None
    return resolved


def _item_texts(item: dict[str, Any], card: dict[str, Any] | None) -> list[str]:
    """Where a Review item cites its documents, newest first: the card's comments, summary and body, the
    options' context, the collected context, and the item's note links."""
    version = item.get("version") or {}
    texts: list[str] = []
    if card:
        texts.extend(c.get("body") or "" for c in card.get("comments") or [])
        texts.extend([card.get("latest_summary") or "", card.get("body") or ""])
    texts.extend([version.get("context") or "", item.get("context") or ""])
    texts.extend(str(link.get("path")) for link in item.get("links") or [] if link.get("kind") == "note" and link.get("path"))
    return texts


def cited_docs(project: ProjectSettings, texts: list[str]) -> list[dict[str, Any]]:
    """Markdown files that ``texts`` name and that exist under the project's vault or root, in order of first
    mention. A text that is itself a path (a note link) counts too; prose paths must be absolute."""
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for text in texts:
        candidates = [text] if text.endswith(".md") and "\n" not in text and " " not in text else _ABS_MD_PATH.findall(text)
        for raw in candidates:
            path = _inside_roots(project, raw.rstrip(".,;:"))
            if path is None or path in seen or not path.is_file():
                continue
            seen.add(path)
            found.append({"path": str(path), "name": path.name, "bytes": path.stat().st_size})
            if len(found) >= CITED_DOC_LIMIT:
                return found
    return found


def read_doc(project: ProjectSettings, raw: str) -> dict[str, Any] | None:
    """One Markdown file under the project's vault or root, for the in-page preview. ``None`` when the path is
    outside those roots, not Markdown, missing, or over ``DOC_MAX_BYTES``."""
    path = _inside_roots(project, raw)
    if path is None or not path.is_file() or path.stat().st_size > DOC_MAX_BYTES:
        return None
    data = path.read_bytes()
    return {
        "path": str(path),
        "name": path.name,
        "rel": _rel(path, project),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "text": data.decode("utf-8", errors="replace"),
    }


BUG_FIELDS = (
    "title",
    "status",
    "severity",
    "related_initiative",
    "symptom",
    "expected",
    "repro",
    "where",
    "evidence",
    "source",
    "similar_to",
    "created",
    "fix_card",
    "reason",
)


def build_tasks(project: ProjectSettings) -> dict[str, Any]:
    """Task notes (including legacy bug notes) with live card state."""
    snapshot = load(project.root)
    report = validate(snapshot, project.vault_root)
    documents = {**snapshot.bugs, **snapshot.tasks}
    normalized = {}
    for task_id, doc in documents.items():
        fm: dict[str, Any] = {k: None for k in ("card_id", "reason", "created", "source", "related_initiative")}
        fm.update(type="task", status="open", priority="medium", evidence=[], similar_to=[])
        fm.update(doc.frontmatter)
        if task_id in snapshot.bugs:
            fm.update(
                type="bug",
                description=fm.get("symptom"),
                priority=fm.get("severity"),
                status=LEGACY_TASK_STATUSES.get(str(fm.get("status")), fm.get("status")),
                card_id=fm.get("fix_card"),
            )
        # Hand-edited or partial notes read like the validator's defaults, never like a missing field in the UI.
        fm["type"] = fm.get("type") or "task"
        fm["status"] = fm.get("status") or "open"
        fm["priority"] = fm.get("priority") or "medium"
        fm["title"] = str(fm.get("title") or task_id)
        fm["description"] = str(fm.get("description") or "")
        normalized[task_id] = fm
    card_ids = [str(fm["card_id"]) for fm in normalized.values() if fm.get("card_id")]
    found = read_cards(project.kanban_board, card_ids) if card_ids else {"state": "fresh", "error": None, "cards": {}}
    rows = []
    for task_id, doc in documents.items():
        fm = normalized[task_id]
        rows.append(
            _jsonable(
                {
                    **fm,
                    "id": task_id,
                    "path": _rel(doc.path, project),
                    "abs_path": str(doc.path),
                    "body": doc.body.strip(),
                    "errors": report.errors.get(task_id, []),
                    "warnings": report.warnings.get(task_id, []),
                    "card": found["cards"].get(str(fm.get("card_id"))) if fm.get("card_id") else None,
                }
            )
        )
    status_rank = {s: i for i, s in enumerate(TASK_STATUSES)}
    priority_rank = {p: i for i, p in enumerate(TASK_PRIORITIES)}
    rows.sort(
        key=lambda r: (
            status_rank.get(str(r.get("status")), 9),
            priority_rank.get(str(r.get("priority")), 1) if r.get("status") in OPEN_TASK_STATUSES else 0,
            str(r.get("created") or ""),
        )
    )
    return {
        "project": project.id,
        "tasks": rows,
        "statuses": list(TASK_STATUSES),
        "open_statuses": list(OPEN_TASK_STATUSES),
        "types": list(TASK_TYPES),
        "kanban": {"state": found["state"], "error": found["error"]},
    }


def build_bugs(project: ProjectSettings) -> dict[str, Any]:
    """Every bug file, open ones first (by severity, then oldest), each with its fix card's live state."""
    snapshot = load(project.root)
    report = validate(snapshot, project.vault_root)
    cards = [str(d.frontmatter["fix_card"]) for d in snapshot.bugs.values() if d.frontmatter.get("fix_card")]
    found = read_cards(project.kanban_board, cards) if cards else {"state": "fresh", "error": None, "cards": {}}
    bugs = []
    for bug_id, doc in snapshot.bugs.items():
        fm = doc.frontmatter
        card = found["cards"].get(str(fm.get("fix_card"))) if fm.get("fix_card") else None
        bugs.append(
            _jsonable(
                {
                    **{k: fm.get(k) for k in BUG_FIELDS},
                    "id": bug_id,
                    "path": _rel(doc.path, project),
                    "abs_path": str(doc.path),
                    "body": doc.body.strip(),
                    "card": card,
                    "errors": report.errors.get(bug_id, []),
                    "warnings": report.warnings.get(bug_id, []),
                }
            )
        )
    status_rank = {s: i for i, s in enumerate(BUG_STATUSES)}
    severity_rank = {s: i for i, s in enumerate(BUG_SEVERITIES)}
    bugs.sort(
        key=lambda b: (
            status_rank.get(str(b["status"]), 9),
            severity_rank.get(str(b["severity"]), 1) if b["status"] in OPEN_BUG_STATUSES else 0,
            str(b["created"] or ""),
        )
    )
    return {
        "project": project.id,
        "bugs": bugs,
        "statuses": list(BUG_STATUSES),
        "open_statuses": list(OPEN_BUG_STATUSES),
        "kanban": {"state": found["state"], "error": found["error"]},
    }


def task_context(project: ProjectSettings, task_id: str) -> dict[str, Any] | None:
    row = next((r for r in build_tasks(project)["tasks"] if r["id"] == task_id), None)
    if row is None:
        return None
    detail = read_card_detail(project.kanban_board, row["card_id"]) if row.get("card_id") and project.kanban_board else None
    data: dict[str, Any] = {"task": row}
    if detail:
        data["card"] = {k: detail.get(k) for k in ("id", "title", "status", "assignee", "pr", "latest_summary", "result")}
    title = row.get("title")
    return {
        "context": _bounded(
            data,
            CONTEXT_LIMIT,
            [
                lambda d: d.get("card", {}).pop("result", None),
                lambda d: d.get("card", {}).pop("latest_summary", None),
                lambda d: d["task"].update(body=str(d["task"].get("body") or "")[:2000]),
            ],
        ),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        "title": task_id if not title or title == task_id else f"{task_id} {title}",
    }


def bug_context(project: ProjectSettings, bug_id: str) -> dict[str, Any] | None:
    bug = next((b for b in build_bugs(project)["bugs"] if b["id"] == bug_id), None)
    if bug is None:
        return None
    detail = read_card_detail(project.kanban_board, bug["fix_card"]) if bug.get("fix_card") and project.kanban_board else None
    data: dict[str, Any] = {"bug": bug}
    if detail:
        data["fix_card"] = {k: detail.get(k) for k in ("id", "title", "status", "assignee", "pr", "latest_summary", "result")}
    return {
        "context": _bounded(
            data,
            CONTEXT_LIMIT,
            [
                lambda d: d.get("fix_card", {}).pop("result", None),
                lambda d: d.get("fix_card", {}).pop("latest_summary", None),
                lambda d: d["bug"].update(body=str(d["bug"].get("body") or "")[:2000]),
            ],
        ),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        "title": f"{bug_id} {bug['title']}",
    }


def triage_context(project: ProjectSettings) -> dict[str, Any]:
    every = build_ideas(project)["ideas"]
    ideas = [i for i in every if i["status"] in OPEN_IDEA_STATUSES]
    rejected = [{k: i[k] for k in ("id", "title", "reason")} for i in every if i["status"] == "rejected"][-20:]
    return {
        "context": _bounded(
            {"open_ideas": ideas, "recently_rejected": rejected},
            CONTEXT_LIMIT,
            [
                lambda d: d.pop("recently_rejected", None),
                lambda d: [i.pop(k, None) for i in d["open_ideas"] for k in ("problem", "expected_outcome", "similar_to")],
            ],
        ),
        "root": str(project.root),
        "cwd": str(project.cwd or project.root),
        "title": project.label,
    }
