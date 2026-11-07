"""Backend of the initiatives plugin, mounted at /api/plugins/initiatives/.

Reads initiative files and Kanban (read-only). Writes are explicit and narrow: new canonical task notes and
user-requested task status/card links under the project root through locked, read-back edits; Review state,
launches and session defaults in the plugin data directory; and new Kanban cards only through the hermes CLI.
It never edits cycles, initiatives, ideas, cron jobs, or sessions.
The /review routes read the daily review store (reviews.db). POST /review/items/{id}/answer, /confirm, /defer,
/auto and /manual run the review executor (review_exec), which writes the answer back to its source: Kanban
through the hermes CLI, cycle and idea files through locked, read-back edits of user-owned fields.
The /projects/{id}/tasks routes are the Tasks tab: both types (task and bug) live in one list, legacy bug notes
are normalized read-only, PATCH moves a task's status, and POST .../card creates and links its Kanban card.
The /projects/{id}/bugs routes remain for backward compatibility with the former Bugs tab.
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from functools import wraps
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hermes_initiatives_core import (  # noqa: E402
    kanban,
    launches,
    metrics,
    review_attention,
    review_digest,
    review_exec,
    review_learned,
    reviews,
    settings,
    summaries,
    views,
    writers,
)
from hermes_initiatives_core.model import BUG_ID, CARD_ID, TASK_ID  # noqa: E402
from hermes_initiatives_core.store import load as load_root  # noqa: E402

router = APIRouter()
_STORE: launches.LaunchStore | None = None


def _store() -> launches.LaunchStore:
    global _STORE
    if _STORE is None or not _STORE.path.parent.exists():
        _STORE = launches.LaunchStore()
    return _STORE


def _project(project_id: str) -> settings.ProjectSettings:
    config, error = settings.load_settings()
    if error:
        raise HTTPException(status_code=500, detail=f"settings file is invalid: {error}")
    project = config.project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"unknown project '{project_id}'")
    return project


def _project_for_initiative(initiative_id: str, project_id: str | None) -> settings.ProjectSettings:
    if project_id:
        return _project(project_id)
    config, error = settings.load_settings()
    if error:
        raise HTTPException(status_code=500, detail=f"settings file is invalid: {error}")
    for project in config.projects:
        if (project.root / initiative_id / "initiative.md").is_file():
            return project
    raise HTTPException(status_code=404, detail=f"unknown initiative '{initiative_id}'")


def _reviews() -> reviews.ReviewStore:
    return reviews.ReviewStore()


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in ("basis", "basis_hash", "watch_hash", "evidence_hash")}


@router.get("/review")
async def review_overview(project: str | None = None) -> dict[str, Any]:
    """The latest review and the live queue. The queue is read now, not frozen at generation time.

    ``?project=<id>`` scopes the response to one configured project: its items (plus items written before the
    project column, attributed by the initiative they name), its review prose, and its result ids. The global
    diagnostics stay: review id and times, urgent-push counters, coverage gaps, learned rules.
    """
    project = (project or "").strip() or None
    config, error = settings.load_settings()
    if error:  # before the store is opened: a broken settings file must not create or read reviews.db
        raise HTTPException(status_code=500, detail=f"settings file is invalid: {error}")
    owner = config.project(project) if project is not None else None
    if project is not None and owner is None:
        raise HTTPException(status_code=404, detail=f"unknown project '{project}'")
    store = _reviews()
    latest = store.latest_review()
    titles, owned = _initiative_index(config.projects)
    own_ids = owned.get(project, set()) if project is not None else set().union(*owned.values())
    titles = {i: t for i, t in titles.items() if i in own_ids}
    foreign = sorted(set().union(*(ids for pid, ids in owned.items() if pid != project)) - own_ids) if owner else []
    scoped = owner is not None
    attention = review_attention.collect(store, [owner] if owner is not None else config.projects)

    queue = store.queue(project=project, exclude_initiatives=foreign)
    current = [i for i in queue if reviews.current(i)]
    pending = [i for i in queue if not reviews.current(i)]
    watching = store.items(statuses=("watching",), project=project, exclude_initiatives=foreign)
    deferred = store.items(statuses=("deferred",), project=project, exclude_initiatives=foreign)
    auto = store.items(statuses=("auto",), project=project, exclude_initiatives=foreign)
    in_progress = store.items(
        statuses=("awaiting_confirm", "applying", "failed", "ready_for_owner"), project=project, exclude_initiatives=foreign
    )
    recent = store.recent_closed(limit=20, project=project, exclude_initiatives=foreign)
    settled = store.recent_settled(limit=20, project=project, exclude_initiatives=foreign)
    suggested = [i for i in store.queue(section="suggested", project=project, exclude_initiatives=foreign) if i["version"]]
    # Every item in scope, in any status: what the review's stored prose, groups, and result ids may name.
    visible_ids = (
        {i["short_id"] for i in store.items(project=project, exclude_initiatives=foreign, limit=5000)} if scoped else set()
    )
    review = None if latest is None else {k: v for k, v in latest.items() if k != "telegram"}
    if review is not None and scoped:
        review = _scoped_review(review, own_ids, visible_ids)
    legacy_groups = review_digest.groups(store, latest["id"]) if latest else []
    if scoped:
        legacy_groups = _scoped_groups(legacy_groups, visible_ids, own_ids)
    return {
        "review": review,
        "last_completed_at": store.last_completed_at(),
        "queue": [_public(i) for i in current],
        "pending": [_public(i) | {"outdated": bool(i["version"])} for i in pending],
        "suggested": [_public(i) for i in suggested],
        "watching": [_public(i) for i in watching],
        "deferred": [_public(i) for i in deferred],
        "auto": [_public(i) for i in auto],
        "learned_rules": review_learned.all_rules(store),
        "in_progress": [_public(i) for i in in_progress],
        "recent": [_public(i) | {"actions": store.actions(i["short_id"])[-1:]} for i in recent],
        "settled": [
            _public(i)
            | {
                "actions": store.actions(i["short_id"])[-1:],
                "overruled": any(a["action"] == "overrule" for a in store.actions(i["short_id"])),
            }
            for i in settled
        ],
        "rule_states": store.rule_states(),
        # Cards whose newest handoff asks the user to act, with no open decision for them (parked, watched, or
        # never collected). Shown first, so a parked approval cannot hide a step only he can do.
        "waiting_on_you": attention["items"],
        "waiting_errors": attention["errors"],
        "groups": [
            {
                **{k: g[k] for k in ("initiative", "summary", "update")},
                "title": titles.get(g["initiative"]),
                **{k: [i["short_id"] for i in g[k]] for k in _GROUP_LISTS},
            }
            for g in legacy_groups
        ],
        # The same split by the initiative each item names NOW. Newer items, moved items, and unknown references
        # are grouped where they belong today; the unassigned group ("") is always present.
        "initiative_groups": _initiative_groups(
            latest,
            titles,
            {
                "decisions": current,
                "pending": pending,
                "watching": watching,
                "deferred": deferred,
                "auto": auto,
                "in_progress": in_progress,
                "recent": recent,
                "settled": settled,
                "suggested": suggested,
            },
            attention["items"],
            scoped=scoped,
        ),
    }


_INITIATIVE_LISTS = ("decisions", "pending", "watching", "deferred", "auto", "in_progress", "recent", "settled", "suggested")
# The collections a stored review itself groups (``review_digest.groups``).
_GROUP_LISTS = ("decisions", "watching", "deferred", "auto")


def _initiative_index(projects: list[settings.ProjectSettings]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """``({initiative id: title}, {project id: initiative ids})`` from every configured root; unreadable roots skipped.

    The titles are what ``review_digest.initiative_titles`` gives. The owned ids are what attributes an item
    written before the store recorded its project: the root that defines the initiative the item names owns it.
    """
    titles: dict[str, str] = {}
    owned: dict[str, set[str]] = {}
    for project in projects:
        ids = owned.setdefault(project.id, set())
        try:
            snapshot = load_root(project.root)
        except OSError:
            continue
        for initiative_id, doc in snapshot.initiatives.items():
            ids.add(initiative_id)
            title = str(doc.frontmatter.get("title") or "").strip()
            titles.setdefault(initiative_id, title or initiative_id)
    return titles, owned


def _scoped_review(review: dict[str, Any], own_ids: set[str], visible_ids: set[str]) -> dict[str, Any]:
    """One project's review: its prose and result ids, with the global diagnostics left in place."""
    scoped = dict(review)
    prose = dict(review.get("summaries") or {})
    for name in ("summaries", "updates"):
        if name in prose:
            prose[name] = {i: text for i, text in (prose.get(name) or {}).items() if i in own_ids}
    if prose:
        scoped["summaries"] = prose
    facts = dict(review.get("facts") or {})
    if "unchanged" in facts:
        facts["unchanged"] = [i for i in facts.get("unchanged") or [] if i in own_ids]
    if "results" in facts:
        facts["results"] = [r for r in facts.get("results") or [] if r.get("short_id") in visible_ids]
    if facts:
        scoped["facts"] = facts
    scoped["items"] = [s for s in review.get("items") or [] if s in visible_ids]
    return scoped


def _scoped_groups(
    groups: list[dict[str, Any]], visible_ids: set[str], own_ids: set[str]
) -> list[dict[str, Any]]:
    """The stored review's groups cut to one project: its items, its prose, and the unowned group."""
    scoped: list[dict[str, Any]] = []
    for group in groups:
        entry = {k: v for k, v in group.items() if k not in _GROUP_LISTS}
        entry.update({name: [i for i in group[name] if i["short_id"] in visible_ids] for name in _GROUP_LISTS})
        if not any(entry[name] for name in _GROUP_LISTS) and not entry["summary"] and not entry["update"]:
            continue
        if entry["initiative"] and entry["initiative"] not in own_ids:
            # Visible unknown-reference items still belong to this project; only foreign prose is dropped.
            if not any(entry[name] for name in _GROUP_LISTS):
                continue
            entry["summary"] = ""
            entry["update"] = ""
        scoped.append(entry)
    return scoped


def _initiative_groups(
    latest: dict[str, Any] | None,
    titles: dict[str, str],
    items: dict[str, list[dict[str, Any]]],
    waiting: list[dict[str, Any]],
    *,
    scoped: bool = False,
) -> list[dict[str, Any]]:
    """Review items split by their current ``initiative``, with the review's prose attached where it names one.

    Every collection keeps its order. ``""`` is the unassigned group and is always present, empty or not. An
    initiative id that no configured root defines keeps its group with ``known: false``, so nothing disappears.
    """
    groups: dict[str, dict[str, Any]] = {}

    def group(initiative: str | None) -> dict[str, Any]:
        key = initiative or ""
        if key not in groups:
            groups[key] = {
                "initiative": key,
                "title": titles.get(key),
                "known": not key or key in titles,
                **{name: [] for name in _INITIATIVE_LISTS},
                "waiting": [],
                "summary": "",
                "update": "",
                "rank": None,
            }
        return groups[key]

    group("")
    for name in _INITIATIVE_LISTS:
        for rank, item in enumerate(items.get(name) or []):
            entry = group(item.get("initiative"))
            entry[name].append(item["short_id"])
            if name == "decisions" and entry["rank"] is None:
                entry["rank"] = rank
    for entry in waiting:
        item = entry.get("item") or {}
        group(item.get("initiative"))["waiting"].append(entry["card"])

    review = latest or {}
    prose = review.get("summaries") or {}
    summaries, updates = prose.get("summaries") or {}, prose.get("updates") or {}
    unchanged = set((review.get("facts") or {}).get("unchanged") or [])
    for initiative in set(summaries) | set(updates) | unchanged:
        key = initiative or ""
        if scoped and key and key not in titles:
            continue  # another project's review prose
        entry = group(key)
        entry["summary"] = str(summaries.get(initiative) or "")
        entry["update"] = str(updates.get(initiative) or (review_digest.UNCHANGED if initiative in unchanged else ""))

    def order(entry: dict[str, Any]) -> tuple:
        quiet = not entry["decisions"] and entry["update"] in ("", review_digest.UNCHANGED)
        return (
            entry["initiative"] == "",  # unassigned always last
            not entry["decisions"],
            quiet,
            entry["rank"] if entry["rank"] is not None else 0,
            entry["initiative"],
        )

    return sorted(groups.values(), key=order)


@router.get("/review/items/{short_id}")
async def review_item(short_id: str) -> dict[str, Any]:
    store = _reviews()
    item = store.get(short_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"unknown review item '{short_id}'")
    return {
        **_public(item),
        "versions": store.versions(short_id),
        "answers": store.answers(short_id),
        "actions": store.actions(short_id),
    }


class ReviewAnswer(BaseModel):
    option: str = Field(min_length=1, max_length=1)
    note: str = Field(default="", max_length=4000)


class ReviewDeferral(BaseModel):
    kind: str = Field(pattern="^(date|state_change|indefinite)$")
    until: str | None = None
    reason: str = Field(default="", max_length=1000)


def _executed(result: dict[str, str]) -> dict[str, Any]:
    store = _reviews()
    item = store.get(result["short_id"])
    return {"result": result, "item": item and _public(item)}


@router.post("/review/items/{short_id}/answer")
def review_answer(short_id: str, body: ReviewAnswer) -> dict[str, Any]:
    """Record the answer (first one wins) and apply it. Irreversible options stop at awaiting_confirm."""
    executor = review_exec.Executor(_reviews())
    return _executed(executor.submit(short_id, body.option, note=body.note, surface="tab"))


@router.post("/review/items/{short_id}/confirm")
def review_confirm(short_id: str) -> dict[str, Any]:
    return _executed(review_exec.Executor(_reviews()).confirm(short_id))


@router.post("/review/items/{short_id}/defer")
def review_defer(short_id: str, body: ReviewDeferral) -> dict[str, Any]:
    command = review_exec.Command("defer", short_id.upper(), defer_kind=body.kind, until=body.until, note=body.reason)
    return _executed(review_exec.Executor(_reviews()).defer(command))


class ReviewAuto(BaseModel):
    note: str = Field(default="", max_length=2000)


@router.post("/review/items/{short_id}/auto")
def review_auto(short_id: str, body: ReviewAuto) -> dict[str, Any]:
    """The user hands this item to the agents. The next recovery tick applies, resolves, or returns it."""
    return _executed(review_exec.Executor(_reviews()).mark_auto(short_id, body.note, surface="tab"))


@router.post("/review/items/{short_id}/manual")
def review_manual(short_id: str) -> dict[str, Any]:
    """The user takes an auto item back before the agents handle it."""
    return _executed(review_exec.Executor(_reviews()).unmark_auto(short_id, surface="tab"))


class RuleState(BaseModel):
    state: str = Field(pattern="^(active|suspended)$")
    reason: str = Field(min_length=5, max_length=1000)


@router.post("/review/rules/{rule}")
def review_rule_state(rule: str, body: RuleState) -> dict[str, Any]:
    """The user suspends or restores one rule the agents learned from an auto mark."""
    store = _reviews()
    if review_learned.get(store, rule) is None:
        raise HTTPException(status_code=404, detail=f"unknown learned rule {rule}")
    changed = store.set_rule_state(rule, body.state, body.reason)
    word = "suspended" if body.state == "suspended" else "restored"
    message = f"rule {rule} {word}" + ("" if changed else " (unchanged)")
    return {"result": {"rule": rule, "state": body.state, "message": message}}


class ReviewOverrule(BaseModel):
    reason: str = Field(min_length=10, max_length=1000)


@router.post("/review/items/{short_id}/overrule")
def review_overrule(short_id: str, body: ReviewOverrule) -> dict[str, Any]:
    """The user says an agent settlement was wrong: its rule is suspended and a follow-up decision is filed."""
    from hermes_initiatives_core import review_learn

    try:
        done = review_learn.overrule(_reviews(), settings.load_settings()[0].projects, short_id, body.reason)
    except reviews.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    message = f"rule {done['rule']} suspended; follow-up {done['follow_up']}"
    return {"result": {"short_id": done["short_id"], "state": "overruled", "message": message}}


@router.get("/projects")
async def list_projects() -> dict[str, Any]:
    config, error = settings.load_settings()
    return {
        "settings_path": str(settings.settings_path()),
        "settings_error": error,
        "projects": [
            {
                "id": p.id,
                "label": p.label,
                "root": str(p.root),
                "cwd": str(p.cwd or p.root),
                "kanban_board": p.kanban_board,
                "vault_name": p.vault_name,
                "root_exists": p.root.is_dir(),
            }
            for p in config.projects
        ],
    }


@router.get("/projects/{project_id}/initiatives")
async def list_initiatives(project_id: str) -> dict[str, Any]:
    return views.build_list(_project(project_id))


@router.get("/projects/{project_id}/summaries")
async def list_summaries(project_id: str) -> dict[str, Any]:
    """Agent-written status summary per initiative, with its age and a stale flag. Read-only."""
    _project(project_id)
    return {
        "project": project_id,
        "stale_after_minutes": summaries.STALE_AFTER_MIN,
        "summaries": summaries.public_view(project_id, summaries.SummaryStore()),
    }


@router.get("/initiatives/{initiative_id}")
async def initiative_detail(initiative_id: str, project: str | None = None) -> dict[str, Any]:
    detail = views.build_detail(_project_for_initiative(initiative_id, project), initiative_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown initiative '{initiative_id}'")
    return detail


@router.get("/projects/{project_id}/cards/{card_id}")
async def card_detail(project_id: str, card_id: str) -> dict[str, Any]:
    if not CARD_ID.match(card_id):
        raise HTTPException(status_code=400, detail=f"invalid card id '{card_id}'")
    project = _project(project_id)
    try:
        card = kanban.read_card_detail(project.kanban_board, card_id)
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=f"Kanban unavailable: {exc}") from exc
    if card is None:
        raise HTTPException(status_code=404, detail=f"card '{card_id}' not found on board '{project.kanban_board}'")
    return card


@router.get("/projects/{project_id}/metrics")
def project_metrics(project_id: str, days: int = 30, bucket: str | None = None) -> dict[str, Any]:
    """Kanban health for the project's board, bucketed in time. Records at most one board snapshot per hour."""
    project = _project(project_id)
    try:
        return metrics.board_metrics(project.kanban_board, days, bucket, snapshots=metrics.SnapshotStore())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=f"Kanban unavailable: {exc}") from exc


@router.get("/projects/{project_id}/doc")
async def project_doc(project_id: str, path: str) -> dict[str, Any]:
    """One Markdown file under the project's vault or root, for the in-page preview. Read-only."""
    doc = views.read_doc(project := _project(project_id), path)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no Markdown file '{path}' in this project's vault")
    return doc | {"vault_name": project.vault_name}


@router.get("/projects/{project_id}/ideas")
async def list_ideas(project_id: str) -> dict[str, Any]:
    return views.build_ideas(_project(project_id))


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20_000)
    type: str = "task"
    priority: str = "medium"
    symptom: str | None = Field(default=None, max_length=10_000)
    severity: str | None = None
    expected: str | None = Field(default=None, max_length=10_000)
    repro: str | None = Field(default=None, max_length=10_000)
    where: str | None = Field(default=None, max_length=10_000)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    related_initiative: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=50_000)


@router.post("/projects/{project_id}/tasks")
def create_task(project_id: str, body: TaskCreate) -> dict[str, Any]:
    project = _project(project_id)
    try:
        task = writers.report_task(project.root, body.model_dump())
    except writers.WriteRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": next(row for row in views.build_tasks(project)["tasks"] if row["id"] == task["id"])}


@router.get("/projects/{project_id}/tasks")
async def list_tasks(project_id: str, type: str | None = None) -> dict[str, Any]:
    if type is not None and type not in ("task", "bug"):
        raise HTTPException(status_code=400, detail="type must be task or bug")
    data = views.build_tasks(_project(project_id))
    if type is not None:
        data["tasks"] = [row for row in data["tasks"] if row["type"] == type]
    return data


def _task_row(project: settings.ProjectSettings, task_id: str) -> dict[str, Any]:
    if not (TASK_ID.fullmatch(task_id) or BUG_ID.fullmatch(task_id)):
        raise HTTPException(status_code=400, detail="invalid task id")
    snapshot = load_root(project.root)
    if any(key == task_id for key, _, _ in snapshot.duplicate_ids) or (task_id in snapshot.tasks and task_id in snapshot.bugs):
        raise HTTPException(status_code=409, detail=f"ambiguous task id '{task_id}'")
    row = next((r for r in views.build_tasks(project)["tasks"] if r["id"] == task_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown task '{task_id}'")
    return row


@router.get("/projects/{project_id}/tasks/{task_id}/session-context")
async def task_context(project_id: str, task_id: str) -> dict[str, Any]:
    project = _project(project_id)
    _task_row(project, task_id)
    context = views.task_context(project, task_id)
    if context is None:
        raise HTTPException(status_code=404, detail=f"unknown task '{task_id}'")
    return context


class TaskUpdate(BaseModel):
    status: str | None = None
    reason: str | None = Field(default=None, max_length=2000)


def _task_card_serialized(function):
    """Share one source-record lock with scheduled intake and both API aliases."""

    @wraps(function)
    def locked(*args, **kwargs):
        bound = inspect.signature(function).bind(*args, **kwargs).arguments
        project_id = bound["project_id"]
        task_id = bound.get("task_id") or bound["bug_id"]
        if not (TASK_ID.fullmatch(task_id) or BUG_ID.fullmatch(task_id)):
            raise HTTPException(status_code=400, detail="invalid task id")
        project = _project(project_id)
        with writers.file_lock(project.root / ".task-card-locks" / (task_id + ".lock")):
            return function(*args, **kwargs)

    return locked


@router.patch("/projects/{project_id}/tasks/{task_id}")
@_task_card_serialized
def update_task(project_id: str, task_id: str, body: TaskUpdate) -> dict[str, Any]:
    project = _project(project_id)
    _task_row(project, task_id)
    if "status" in body.model_fields_set and body.status is None:
        raise HTTPException(status_code=400, detail="status cannot be null")
    try:
        writers.update_task(project.root, task_id, status=body.status, reason=body.reason)
    except writers.WriteRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": _task_row(project, task_id)}


@router.get("/projects/{project_id}/bugs")
async def list_bugs(project_id: str) -> dict[str, Any]:
    return views.build_bugs(_project(project_id))


def _bug_id(bug_id: str) -> str:
    if not BUG_ID.fullmatch(bug_id):
        raise HTTPException(status_code=400, detail=f"invalid bug id '{bug_id}'")
    return bug_id


@router.get("/projects/{project_id}/bugs/{bug_id}/session-context")
async def bug_context(project_id: str, bug_id: str) -> dict[str, Any]:
    context = views.bug_context(_project(project_id), _bug_id(bug_id))
    if context is None:
        raise HTTPException(status_code=404, detail=f"unknown bug '{bug_id}'")
    return context


class BugUpdate(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=2000)


@router.patch("/projects/{project_id}/bugs/{bug_id}")
@_task_card_serialized
def update_bug(project_id: str, bug_id: str, body: BugUpdate) -> dict[str, Any]:
    """The user moves a bug: triaged, fixed, wontfix (needs a reason), or back to open."""
    project = _project(project_id)
    if _bug_id(bug_id) not in load_root(project.root).bugs:
        raise HTTPException(status_code=404, detail=f"unknown bug '{bug_id}'")
    try:
        after = writers.update_bug(project.root, bug_id, status=body.status, reason=body.reason)
    except writers.WriteRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bug": after}


class FixCardRequest(BaseModel):
    assignee: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=4000)


@router.post("/projects/{project_id}/tasks/{task_id}/card")
@_task_card_serialized
def create_task_card(project_id: str, task_id: str, body: FixCardRequest) -> dict[str, Any]:
    project = _project(project_id)
    row = _task_row(project, task_id)
    if row.get("card_id"):
        raise HTTPException(status_code=409, detail=f"{task_id} already has card {row['card_id']}")
    if row.get("status") in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail=f"{task_id} is closed; reopen it first")
    assignee = (body.assignee or project.intake_assignee or "").strip()
    if not project.kanban_board or not assignee:
        raise HTTPException(status_code=400, detail="a card needs kanban_board and an assignee (or intake_assignee)")
    if row["type"] == "bug":
        card_body = _fix_card_body(row, Path(row["abs_path"]), body.note or "")
    else:
        card_body = "\n\n".join(
            [
                f"Task `{task_id}` ({row['priority']} priority): {row['abs_path']}",
                row["description"],
                f"Details:\n{row.get('body') or '-'}",
                "Evidence:\n" + ("\n".join(f"- {e}" for e in row.get("evidence") or []) or "-"),
                f"Initiative: {row.get('related_initiative') or '-'}",
                f"Note from the user:\n{(body.note or '').strip()}",
                "Implement the requested task, verify the outcome, and route through the normal lifecycle for this board.",
            ]
        )
    legacy = task_id in load_root(project.root).bugs
    try:
        card = review_exec.Kanban(review_exec.default_runner).create(
            project.kanban_board,
            title=f"{'Fix' if row['type'] == 'bug' else 'Task'}: {row['title']}"[:200],
            assignee=assignee,
            body=card_body,
            key=f"{'bug' if legacy else 'task'}:{project.id}:{task_id}",
        )
    except review_exec.ActionFailed as exc:
        raise HTTPException(status_code=502, detail=f"Kanban create failed: {exc}") from exc
    try:
        writers.update_task(project.root, task_id, status="in_progress", card_id=card)
    except writers.WriteRefused as exc:
        raise HTTPException(status_code=500, detail=f"created {card} but could not link it: {exc}") from exc
    return {"card": card, "assignee": assignee, "task": _task_row(project, task_id)}


def _fix_card_body(fm: dict[str, Any], path: Path, note: str) -> str:
    def line(label: str, key: str) -> str:
        return f"{label}: {fm.get(key) or '-'}"

    evidence = "\n".join(f"- {e}" for e in (fm.get("evidence") or [])) or "-"
    parts = [
        f"Fix bug `{fm.get('id')}` ({fm.get('severity') or 'medium'} severity): {path}",
        "\n".join([line("Symptom", "symptom"), line("Expected", "expected"), line("Where", "where")]),
        f"Repro:\n{fm.get('repro') or '-'}",
        f"Evidence:\n{evidence}",
    ]
    if fm.get("related_initiative"):
        parts.append(f"Initiative: {fm['related_initiative']}")
    if note.strip():
        parts.append(f"Note from the user:\n{note.strip()}")
    parts.append(
        "Find the root cause, add the smallest regression test that fails first, then fix. "
        "Route through the normal lifecycle for this board."
    )
    return "\n\n".join(parts)


@router.post("/projects/{project_id}/bugs/{bug_id}/fix-card")
@_task_card_serialized
def create_fix_card(project_id: str, bug_id: str, body: FixCardRequest) -> dict[str, Any]:
    """Create the bug's Kanban fix card (idempotent per bug), link it in the bug file, and set status fixing."""
    project = _project(project_id)
    doc = load_root(project.root).bugs.get(_bug_id(bug_id))
    if doc is None:
        raise HTTPException(status_code=404, detail=f"unknown bug '{bug_id}'")
    fm = doc.frontmatter
    if fm.get("fix_card"):
        raise HTTPException(status_code=409, detail=f"{bug_id} already has fix card {fm['fix_card']}")
    if str(fm.get("status")) in ("fixed", "wontfix"):
        raise HTTPException(status_code=409, detail=f"{bug_id} is {fm.get('status')}; reopen it first")
    assignee = (body.assignee or project.intake_assignee or "").strip()
    if not project.kanban_board or not assignee:
        raise HTTPException(
            status_code=400, detail="a fix card needs the project's kanban_board and an assignee (or intake_assignee)"
        )
    try:
        card = review_exec.Kanban(review_exec.default_runner).create(
            project.kanban_board,
            title=f"Fix: {fm.get('title') or bug_id}"[:200],
            assignee=assignee,
            body=_fix_card_body(fm, doc.path, body.note or ""),
            key=f"bug:{project.id}:{bug_id}",
        )
    except review_exec.ActionFailed as exc:
        raise HTTPException(status_code=502, detail=f"Kanban create failed: {exc}") from exc
    try:
        after = writers.update_bug(project.root, bug_id, status="fixing", fix_card=card)
    except writers.WriteRefused as exc:
        raise HTTPException(status_code=500, detail=f"created {card} but could not link it: {exc}") from exc
    return {"card": card, "assignee": assignee, "bug": after}


@router.get("/initiatives/{initiative_id}/session-context")
async def session_context(initiative_id: str, project: str | None = None) -> dict[str, Any]:
    context = views.discussion_context(_project_for_initiative(initiative_id, project), initiative_id)
    if context is None:
        raise HTTPException(status_code=404, detail=f"unknown initiative '{initiative_id}'")
    return context


@router.get("/initiatives/{initiative_id}/subject-context")
async def subject_context(initiative_id: str, kind: str, subject: str, project: str | None = None) -> dict[str, Any]:
    return _subject_context(_project_for_initiative(initiative_id, project), initiative_id, kind, subject)


def _subject_context(project: settings.ProjectSettings, initiative_id: str, kind: str, subject: str) -> dict[str, Any]:
    if kind not in launches.SUBJECT_KINDS:
        raise HTTPException(status_code=400, detail="kind must be cycle or card")
    if kind == "card" and not CARD_ID.match(subject):
        raise HTTPException(status_code=400, detail=f"invalid card id '{subject}'")
    try:
        context = views.subject_context(project, initiative_id, kind, subject)
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=f"Kanban unavailable: {exc}") from exc
    if context is None:
        raise HTTPException(status_code=404, detail=f"{kind} '{subject}' is not part of initiative '{initiative_id}'")
    return context


@router.get("/projects/{project_id}/attend-context")
async def attend_context(project_id: str, card: str) -> dict[str, Any]:
    return _attend_context(_project(project_id), card)


def _attend_context(project: settings.ProjectSettings, card: str) -> dict[str, Any]:
    if not CARD_ID.match(card):
        raise HTTPException(status_code=400, detail=f"invalid card id '{card}'")
    try:
        entry = review_attention.find(_reviews(), project, card)
        context = views.attend_context(project, entry) if entry else None
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=f"Kanban unavailable: {exc}") from exc
    if context is None:
        raise HTTPException(status_code=404, detail=f"card '{card}' is not waiting on you")
    return context


@router.get("/review/items/{short_id}/session-context")
async def blocker_context(short_id: str) -> dict[str, Any]:
    return _blocker_context(short_id)


def _blocker_context(short_id: str) -> dict[str, Any]:
    store = _reviews()
    item = store.get(short_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"unknown review item '{short_id}'")
    if not item.get("project"):
        raise HTTPException(status_code=400, detail=f"{item['short_id']} belongs to no project")
    try:
        return views.blocker_context(
            _project(item["project"]), item, store.answers(item["short_id"]), store.actions(item["short_id"])
        )
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail=f"Kanban unavailable: {exc}") from exc


@router.get("/projects/{project_id}/triage-context")
async def triage_context(project_id: str) -> dict[str, Any]:
    return views.triage_context(_project(project_id))


class LaunchRequest(BaseModel):
    project: str = Field(min_length=1, max_length=64)
    initiative: str | None = Field(default=None, max_length=120)
    kind: str
    subject: str | None = Field(default=None, max_length=120)
    connection_id: str = Field(min_length=1, max_length=200)
    profile: str = Field(min_length=1, max_length=200)
    target_profile: str = Field(min_length=1, max_length=200)


class LaunchUpdate(BaseModel):
    state: str
    stored_session_id: str | None = Field(default=None, max_length=256)
    runtime_session_id: str | None = Field(default=None, max_length=256)
    error: str | None = Field(default=None, max_length=2000)


@router.post("/launches")
async def create_launch(body: LaunchRequest) -> dict[str, Any]:
    project = _project(body.project)
    if body.kind in launches.SUBJECT_KINDS:
        if not body.initiative or not body.subject:
            raise HTTPException(status_code=400, detail=f"a {body.kind} launch needs an initiative and a subject")
        context = _subject_context(project, body.initiative, body.kind, body.subject)
        prefix = f"{'Cycle' if body.kind == 'cycle' else 'Card'}: {context['title']}"
    elif body.kind == "attend":
        if not body.subject:
            raise HTTPException(status_code=400, detail="an attend launch needs the card id as subject")
        prefix = _attend_context(project, body.subject)["title"]
    elif body.kind == "blocker":
        if not body.subject:
            raise HTTPException(status_code=400, detail="a blocker launch needs the Review item id as subject")
        item = _reviews().get(body.subject)
        if item is None or item.get("project") != project.id:
            raise HTTPException(status_code=404, detail=f"review item '{body.subject}' is not in project '{project.id}'")
        body.subject = item["short_id"]
        prefix = _blocker_context(item["short_id"])["title"]
    elif body.kind == "task":
        if not body.subject:
            raise HTTPException(status_code=400, detail="a task launch needs the task id as subject")
        _task_row(project, body.subject)
        context = views.task_context(project, body.subject)
        if context is None:
            raise HTTPException(status_code=404, detail=f"unknown task '{body.subject}'")
        prefix = f"Task: {context['title']}"
    elif body.kind == "bug":
        if not body.subject:
            raise HTTPException(status_code=400, detail="a bug launch needs the bug id as subject")
        context = views.bug_context(project, _bug_id(body.subject))
        if context is None:
            raise HTTPException(status_code=404, detail=f"unknown bug '{body.subject}'")
        prefix = f"Bug: {context['title']}"
    elif body.kind == "discuss":
        if not body.initiative:
            raise HTTPException(status_code=400, detail="a discuss launch needs an initiative")
        detail = views.build_detail(project, body.initiative)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown initiative '{body.initiative}'")
        prefix = f"Initiative: {detail['title']}"
    else:
        prefix = f"Initiative triage: {project.label}"
    try:
        return _store().create(
            project=project.id,
            initiative=body.initiative,
            kind=body.kind,
            subject=body.subject if body.kind in launches.SUBJECTED_KINDS else None,
            connection_id=body.connection_id,
            profile=body.profile,
            target_profile=body.target_profile,
            title_prefix=prefix,
        )
    except launches.LaunchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/launches/{launch_id}")
async def update_launch(launch_id: str, body: LaunchUpdate) -> dict[str, Any]:
    try:
        return _store().transition(
            launch_id,
            body.state,
            stored_session_id=body.stored_session_id,
            runtime_session_id=body.runtime_session_id,
            error=body.error,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown launch '{launch_id}'") from exc
    except launches.LaunchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/launches/{launch_id}")
async def delete_launch(launch_id: str) -> dict[str, Any]:
    if not _store().delete(launch_id):
        raise HTTPException(status_code=404, detail=f"unknown launch '{launch_id}'")
    return {"deleted": launch_id}


@router.get("/initiatives/{initiative_id}/sessions")
async def initiative_sessions(initiative_id: str, project: str | None = None) -> dict[str, Any]:
    owner = _project_for_initiative(initiative_id, project)
    return {"launches": _store().list(project=owner.id, initiative=initiative_id)}


@router.get("/review/items/{short_id}/sessions")
async def review_item_sessions(short_id: str) -> dict[str, Any]:
    """Sessions started by Discuss on one Review item, newest first."""
    item = _reviews().get(short_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"unknown review item '{short_id}'")
    if not item.get("project"):
        return {"launches": []}
    return {"launches": _store().list(project=item["project"], kind="blocker", subject=item["short_id"])}


@router.get("/projects/{project_id}/sessions")
async def project_sessions(project_id: str, kind: str | None = None) -> dict[str, Any]:
    return {"launches": _store().list(project=_project(project_id).id, kind=kind)}


@router.get("/session-defaults")
async def get_session_defaults() -> dict[str, Any]:
    return settings.load_session_defaults().model_dump()


@router.put("/session-defaults")
async def put_session_defaults(body: settings.SessionDefaults) -> dict[str, Any]:
    return settings.save_session_defaults(body).model_dump()


@router.get("/health")
async def health() -> dict[str, Any]:
    config, error = settings.load_settings()
    projects = []
    for project in config.projects:
        board: dict[str, Any] = {"state": "not_configured"}
        if project.kanban_board:
            probe = kanban.read_cards(project.kanban_board, ["t_00000000"])
            board = {"state": probe["state"], "error": probe["error"]}
        projects.append({"id": project.id, "root_readable": project.root.is_dir(), "kanban": board})
    compat = "ok"  # boards are read by the documented file layout; no private Hermes module is required
    return {"settings_error": error, "projects": projects, "hermes_compat": compat, "launch_store": str(_store().path)}
