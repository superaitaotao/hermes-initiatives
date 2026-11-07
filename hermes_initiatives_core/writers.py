"""The agent write paths: append an observation, propose an idea, report a bug, link cards to a cycle. All lock and validate."""

from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import kanban
from .docs import atomic_write, dump_markdown, parse_jsonl, parse_markdown
from .lifecycle import as_list
from .model import (
    ASSESSMENT_STATUSES,
    BUG_SEVERITIES,
    BUG_STATUSES,
    CARD_ID,
    CYCLE_ID,
    IDEA_SIZES,
    MAX_OPEN_IDEAS_PER_SOURCE,
    OBSERVATION_KINDS,
    OPEN_BUG_STATUSES,
    OPEN_IDEA_STATUSES,
    OPEN_TASK_STATUSES,
    TASK_PRIORITIES,
    TASK_STATUSES,
    TASK_TYPES,
)
from .store import ObservationLog, active_observations, load
from .validate import cycle_edit_errors, cycle_errors

SIMILARITY_THRESHOLD = 0.6


class WriteRefused(ValueError):
    """The write would break a rule; nothing was written."""


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def caller_identity() -> tuple[str, str]:
    """``(profile, run)`` of the calling agent, from the environment Hermes sets for tools."""
    profile = os.environ.get("HERMES_SESSION_PROFILE", "").strip()
    if not profile:
        home = os.environ.get("HERMES_HOME", "").rstrip("/")
        profile = Path(home).name if "/profiles/" in home else ("default" if home else "unknown")
    session = os.environ.get("HERMES_SESSION_ID", "").strip()
    if os.environ.get("HERMES_CRON_SESSION", "").strip() in ("1", "true") or session.startswith("cron_"):
        run = f"cron:{session}" if session else "cron"
    else:
        run = f"session:{session}" if session else "manual"
    return profile, run


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def edit_frontmatter(path: Path, root: Path, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Change one file's frontmatter under the root's write lock, then return it as read back from disk.

    A cycle file is validated before it is written: a change that adds a validator error (for example
    ``stage: measuring`` without the measurement fields) is refused and nothing is written. Errors the file
    already had do not block unrelated changes, such as appending a decision row.
    """
    with file_lock(root / ".review-write.lock"):
        doc = parse_markdown(path.read_text(encoding="utf-8"), path)
        frontmatter = copy.deepcopy(doc.frontmatter)
        change(frontmatter)
        if path.parent.name == "cycles" and CYCLE_ID.fullmatch(path.stem):
            rows = _cycle_observations(path)
            added = cycle_edit_errors(doc.frontmatter, frontmatter, rows)
            if added:
                raise WriteRefused(f"{path.stem}: refused, the change would make the cycle invalid: {'; '.join(added)}")
        atomic_write(path, dump_markdown(frontmatter, doc.body))
        return parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter


def _cycle_observations(cycle_file: Path, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    log = cycle_file.parent.parent / "observations" / f"{cycle_file.stem}.jsonl"
    rows, errors = parse_jsonl(log.read_text(encoding="utf-8")) if log.exists() else ([], [])
    if extra:
        rows = [*rows, extra]
    return active_observations(ObservationLog(path=log, rows=rows, errors=errors))


def preview_cycle_errors(cycle_file: Path, change: Callable[[dict[str, Any]], None]) -> list[str]:
    """Validator errors that ``change`` would add to the cycle; nothing is written."""
    doc = parse_markdown(cycle_file.read_text(encoding="utf-8"), cycle_file)
    frontmatter = copy.deepcopy(doc.frontmatter)
    change(frontmatter)
    rows = _cycle_observations(cycle_file)
    return cycle_edit_errors(doc.frontmatter, frontmatter, rows)


def _cycle_file(root: Path, cycle_id: str) -> Path:
    match = CYCLE_ID.fullmatch(cycle_id)
    if not match:
        raise WriteRefused(f"'{cycle_id}' is not a cycle id")
    path = root / match.group(1) / "cycles" / f"{cycle_id}.md"
    if not path.is_file():
        raise WriteRefused(f"cycle file not found: {path}")
    return path


def link_cards(root: Path, board: str | None, cycle_id: str, card_ids: list[str]) -> list[str]:
    """Append cards to a cycle's ``kanban_cards``. Never removes or reorders. Returns the ids it added."""
    path = _cycle_file(root, cycle_id)
    cards = list(dict.fromkeys(str(c) for c in card_ids))
    if not cards:
        raise WriteRefused("no card ids given")
    bad = [c for c in cards if not CARD_ID.fullmatch(c)]
    if bad:
        raise WriteRefused(f"card id must match t_xxxxxxxx: {', '.join(bad)}")
    found = kanban.read_cards(board, cards)
    if found["state"] != "fresh":
        raise WriteRefused(f"Kanban board '{board}' unreadable: {found['error']}")
    missing = [c for c in cards if found["cards"][c]["state"] != "fresh"]
    if missing:
        raise WriteRefused(f"not on board '{board}': {', '.join(missing)}")
    added: list[str] = []

    def change(fm: dict[str, Any]) -> None:
        excluded = {str(c) for c in as_list(fm.get("excluded_cards"))}
        refused = [c for c in cards if c in excluded]
        if refused:
            raise WriteRefused(f"excluded by the user in {cycle_id}: {', '.join(refused)}")
        current = [str(c) for c in as_list(fm.get("kanban_cards"))]
        added.extend(c for c in cards if c not in current)
        fm["kanban_cards"] = current + added

    if not _needs_link(path, cards):
        return []
    after = edit_frontmatter(path, root, change)
    if not added:
        return []
    listed = [str(c) for c in as_list(after.get("kanban_cards"))]
    if listed[-len(added) :] != added:
        raise WriteRefused(f"{cycle_id}: read-back shows {listed}, expected {added} at the end")
    profile, run = caller_identity()
    record_observation(root, cycle_id, {"kind": "note", "detail": f"linked {', '.join(added)} by {profile} {run}"})
    return added


def _needs_link(path: Path, cards: list[str]) -> bool:
    """True when the file lacks any of *cards* or excludes one (so the locked edit can refuse it)."""
    fm = parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter
    present = {str(c) for c in as_list(fm.get("kanban_cards"))} - {str(c) for c in as_list(fm.get("excluded_cards"))}
    return any(c not in present for c in cards)


def unlinked_cards(root: Path, board: str | None) -> dict[str, list[str]]:
    """Cards whose body names a cycle in its marker line but are neither listed nor excluded there."""
    markers = kanban.cycle_markers(board)
    if markers["state"] != "fresh":
        raise WriteRefused(f"Kanban board '{board}' unreadable: {markers['error']}")
    snapshot = load(root)
    result: dict[str, list[str]] = {}
    for cycle_id, cards in markers["markers"].items():
        doc = snapshot.cycles.get(cycle_id)
        if doc is None:
            continue
        known = {str(c) for key in ("kanban_cards", "excluded_cards") for c in as_list(doc.frontmatter.get(key))}
        todo = [c for c in cards if c not in known]
        if todo:
            result[cycle_id] = todo
    return result


def sweep_links(root: Path, board: str | None) -> dict[str, list[str]]:
    """Link every derived, unlisted, non-excluded card. Idempotent: a second sweep links nothing."""
    linked: dict[str, list[str]] = {}
    for cycle_id, cards in sorted(unlinked_cards(root, board).items()):
        added = link_cards(root, board, cycle_id, cards)
        if added:
            linked[cycle_id] = added
    return linked


def record_observation(root: Path, cycle_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    match = CYCLE_ID.fullmatch(cycle_id)
    if not match:
        raise WriteRefused(f"'{cycle_id}' is not a cycle id")
    initiative_id = match.group(1)
    cycle_file = root / initiative_id / "cycles" / f"{cycle_id}.md"
    if not cycle_file.is_file():
        raise WriteRefused(f"cycle file not found: {cycle_file}")
    kind = fields.get("kind")
    if kind not in OBSERVATION_KINDS:
        raise WriteRefused(f"kind must be one of {', '.join(OBSERVATION_KINDS)}")
    if kind in ("release_verified", "metric") and not fields.get("evidence"):
        raise WriteRefused(f"{kind} observations need evidence")
    if kind == "metric" and (not fields.get("metric") or fields.get("value") in (None, "")):
        raise WriteRefused("metric observations need metric and value")
    if kind == "assessment":
        if fields.get("status") not in ASSESSMENT_STATUSES:
            raise WriteRefused("assessment needs a valid status")
        for name in ("epoch", "evidence", "proposal", "detail"):
            if not isinstance(fields.get(name), str) or not fields[name].strip():
                raise WriteRefused(f"assessment needs {name}")
        import hashlib

        fields = {**fields, "artifact_sha256": {}}
        for name in ("evidence", "proposal"):
            artifact = Path(fields[name]).expanduser()
            if not artifact.is_absolute() or not artifact.is_file():
                raise WriteRefused(f"assessment {name} must be an existing absolute report path")
            try:
                content = artifact.read_bytes()
                if not content.decode("utf-8").strip():
                    raise WriteRefused(f"assessment {name} is empty")
            except (OSError, UnicodeError) as exc:
                raise WriteRefused(f"assessment {name} unreadable: {exc}") from exc
            fields[name] = str(artifact)
            fields["artifact_sha256"][name] = hashlib.sha256(content).hexdigest()
    profile, run = caller_identity()
    row = {"at": _now(), "by": profile, "run": run, "kind": kind}
    for key in (
        "metric",
        "value",
        "evidence_date",
        "environment",
        "evidence",
        "detail",
        "supersedes",
        "epoch",
        "status",
        "proposal",
        "artifact_sha256",
    ):
        if fields.get(key) not in (None, ""):
            row[key] = fields[key]
    log = root / initiative_id / "observations" / f"{cycle_id}.jsonl"
    with file_lock(log.with_suffix(".lock")):
        if row.get("supersedes"):
            existing = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            if not any(json.loads(line).get("at") == row["supersedes"] for line in existing if line.strip()):
                raise WriteRefused(f"supersedes '{row['supersedes']}' matches no earlier observation")
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    # Diagnostics must never prevent retention of an observation, including on a malformed cycle file.
    try:
        fm = parse_markdown(cycle_file.read_text(encoding="utf-8"), cycle_file).frontmatter
        warnings = cycle_errors(fm, _cycle_observations(cycle_file))
    except (ValueError, OSError, TypeError, AttributeError):
        warnings = ["Observation saved; cycle diagnostics unavailable. Repair the cycle document and validate it."]
    return {**row, "cycle_warnings": warnings} if warnings else row


def _slug(title: str, fallback: str = "idea") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    # Keep the whole filename under NAME_MAX even for a title with one very long token.
    return ("-".join(slug.split("-")[:6])[:60].strip("-")) or fallback


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def similar_ideas(ideas: dict[str, Any], title: str, problem: str = "", field: str = "problem") -> list[str]:
    probe = _norm(f"{title} {problem}")
    scored = []
    for idea_id, doc in ideas.items():
        fm = doc.frontmatter
        other = _norm(f"{fm.get('title', '')} {fm.get(field, '')}")
        score = SequenceMatcher(None, probe, other).ratio()
        if score >= SIMILARITY_THRESHOLD:
            scored.append((score, idea_id))
    return [idea_id for _, idea_id in sorted(scored, reverse=True)[:5]]


def propose_idea(root: Path, fields: dict[str, Any], today: dt.date | None = None) -> dict[str, Any]:
    title = str(fields.get("title") or "").strip()
    if not title:
        raise WriteRefused("an idea needs a title")
    evidence = [str(e) for e in (fields.get("evidence") or []) if str(e).strip()]
    if not evidence:
        raise WriteRefused("an idea needs at least one evidence link")
    size = str(fields.get("size") or "cycle")
    if size not in IDEA_SIZES:
        raise WriteRefused(f"size must be one of {', '.join(IDEA_SIZES)}")
    profile, run = caller_identity()
    source = str(fields.get("source") or f"{profile}/{run}")
    ideas_dir = root / "ideas"
    with file_lock(ideas_dir / ".lock"):
        snapshot = load(root)
        related = fields.get("related_initiative")
        if related and str(related) not in snapshot.initiatives:
            raise WriteRefused(f"related_initiative '{related}' is not a known initiative")
        source_key = source.split("/", 1)[0]
        open_count = sum(
            1
            for doc in snapshot.ideas.values()
            if str(doc.frontmatter.get("status")) in OPEN_IDEA_STATUSES
            and str(doc.frontmatter.get("source") or "").split("/", 1)[0] == source_key
        )
        if open_count >= MAX_OPEN_IDEAS_PER_SOURCE:
            raise WriteRefused(f"{source_key} already has {open_count} open ideas; the limit is {MAX_OPEN_IDEAS_PER_SOURCE}")
        day = (today or dt.date.today()).strftime("%Y%m%d")
        base = f"idea-{day}-{_slug(title)}"
        idea_id, suffix = base, 2
        while idea_id in snapshot.ideas or (ideas_dir / f"{idea_id}.md").exists():
            idea_id, suffix = f"{base}-{suffix}", suffix + 1
        frontmatter = {
            "id": idea_id,
            "title": title,
            "status": "proposed",
            "size": size,
            "related_initiative": related or None,
            "problem": fields.get("problem") or None,
            "expected_outcome": fields.get("expected_outcome") or None,
            "evidence": evidence,
            "source": source,
            "similar_to": similar_ideas(snapshot.ideas, title, str(fields.get("problem") or "")),
            "created": (today or dt.date.today()).isoformat(),
            "promoted_to": None,
            "merged_into": None,
            "review_on": None,
            "reason": None,
        }
        cards = [c for c in fields.get("kanban_cards") or [] if CARD_ID.fullmatch(str(c))]
        if cards:
            frontmatter["kanban_cards"] = cards
        text = dump_markdown(frontmatter, str(fields.get("body") or "").strip() + "\n")
        ideas_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(ideas_dir / f"{idea_id}.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    return frontmatter


def update_task(
    root: Path, task_id: str, *, status: str | None = None, card_id: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Change task vocabulary, translating only when editing a legacy bug note."""
    from .model import LEGACY_TASK_STATUSES

    snapshot = load(root)
    if any(key == task_id for key, _, _ in snapshot.duplicate_ids) or (task_id in snapshot.tasks and task_id in snapshot.bugs):
        raise WriteRefused(f"ambiguous task id '{task_id}'")
    legacy = task_id in snapshot.bugs
    doc = snapshot.bugs.get(task_id) if legacy else snapshot.tasks.get(task_id)
    if doc is None:
        raise WriteRefused(f"unknown task '{task_id}'")
    if status is not None and status not in TASK_STATUSES:
        raise WriteRefused("invalid task status")
    if card_id is not None and not CARD_ID.fullmatch(card_id):
        raise WriteRefused("card_id must match t_xxxxxxxx")
    fields = {}
    if status is not None:
        fields["status"] = {v: k for k, v in LEGACY_TASK_STATUSES.items()}.get(status, status) if legacy else status
    if card_id is not None:
        fields["fix_card" if legacy else "card_id"] = card_id
    if reason is not None:
        fields["reason"] = reason.strip() or None
    if status == "open" and reason is None:
        # Reopening ends the cancellation/completion reason; a later completion needs its own card or reason.
        fields["reason"] = None
    if not fields:
        raise WriteRefused("nothing to change")

    def change(fm: dict[str, Any]) -> None:
        after = {**fm, **fields}
        final = LEGACY_TASK_STATUSES.get(str(after.get("status")), after.get("status")) if legacy else after.get("status")
        linked = after.get("fix_card" if legacy else "card_id")
        if final == "in_progress" and not linked:
            raise WriteRefused("in_progress needs a card")
        if final == "cancelled" and not after.get("reason"):
            raise WriteRefused("cancelled needs a reason")
        if final == "done" and not (linked or after.get("reason")):
            raise WriteRefused("done needs a card or a reason")
        fm.update(fields)

    after = edit_frontmatter(doc.path, root, change)
    if any(after.get(k) != v for k, v in fields.items()):
        raise WriteRefused(f"{task_id}: read-back mismatch")
    return after


def report_task(root: Path, fields: dict[str, Any], today: dt.date | None = None) -> dict[str, Any]:
    """Create a canonical task note; existing notes are never overwritten."""
    title = str(fields.get("title") or "").strip()
    description = str(fields.get("description") or "").strip()
    if not title or not description:
        raise WriteRefused("a task needs title and description")
    task_type = fields.get("type", "task")
    priority = fields.get("priority", "medium")
    if task_type not in TASK_TYPES:
        raise WriteRefused("type must be task or bug")
    if priority not in TASK_PRIORITIES:
        raise WriteRefused("priority must be high, medium, or low")
    if task_type == "bug" and fields.get("severity") is not None and fields["severity"] not in TASK_PRIORITIES:
        raise WriteRefused("severity must be high, medium, or low")
    tasks_dir = root / "tasks"
    if tasks_dir.is_symlink():
        raise WriteRefused("tasks directory cannot be a symlink")
    with file_lock(tasks_dir / ".lock"):
        snapshot = load(root)
        related = fields.get("related_initiative")
        if related and related not in snapshot.initiatives:
            raise WriteRefused(f"related_initiative '{related}' is not a known initiative")
        day = today or dt.date.today()
        base = f"task-{day:%Y%m%d}-{_slug(title, 'task')}"
        task_id, suffix = base, 2
        while task_id in snapshot.tasks or (tasks_dir / f"{task_id}.md").exists():
            task_id, suffix = f"{base}-{suffix}", suffix + 1
        profile, run = caller_identity()
        fm = {
            "id": task_id,
            "type": task_type,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "open",
            "card_id": None,
            "reason": None,
            "created": day.isoformat(),
            "source": fields.get("source") or f"{profile}/{run}",
            "related_initiative": fields.get("related_initiative") or None,
            "evidence": fields.get("evidence") or [],
            "similar_to": [],
        }
        open_tasks = {k: d for k, d in snapshot.tasks.items() if d.frontmatter.get("status") in TASK_STATUSES[:3]}
        similar = similar_ideas(open_tasks, title, description, field="description")
        open_bugs = {k: d for k, d in snapshot.bugs.items() if str(d.frontmatter.get("status")) in OPEN_BUG_STATUSES}
        similar += [bug_id for bug_id in similar_ideas(open_bugs, title, description, field="symptom") if bug_id not in similar]
        fm["similar_to"] = similar
        if task_type == "bug":
            fm.update(
                symptom=str(fields.get("symptom") or "").strip() or description,
                severity=fields.get("severity") or priority,
                **{k: fields.get(k) for k in ("expected", "repro", "where")},
            )
        path = tasks_dir / f"{task_id}.md"
        with path.open("x", encoding="utf-8") as handle:
            handle.write(dump_markdown(fm, str(fields.get("body") or "").strip() + "\n"))
        return fm


def report_bug(root: Path, fields: dict[str, Any], today: dt.date | None = None) -> dict[str, Any]:
    """File one bug as ``bugs/bug-YYYYMMDD-<slug>.md`` with status ``open``. Never edits an existing bug."""
    title = str(fields.get("title") or "").strip()
    if not title:
        raise WriteRefused("a bug needs a title")
    symptom = str(fields.get("symptom") or "").strip()
    if not symptom:
        raise WriteRefused("a bug needs a symptom: what goes wrong")
    severity = str(fields.get("severity") or "medium")
    if severity not in BUG_SEVERITIES:
        raise WriteRefused(f"severity must be one of {', '.join(BUG_SEVERITIES)}")
    profile, run = caller_identity()
    source = str(fields.get("source") or f"{profile}/{run}")
    bugs_dir = root / "bugs"
    with file_lock(bugs_dir / ".lock"):
        snapshot = load(root)
        related = fields.get("related_initiative")
        if related and str(related) not in snapshot.initiatives:
            raise WriteRefused(f"related_initiative '{related}' is not a known initiative")
        day = (today or dt.date.today()).strftime("%Y%m%d")
        base = f"bug-{day}-{_slug(title, 'bug')}"
        bug_id, suffix = base, 2
        while bug_id in snapshot.bugs or (bugs_dir / f"{bug_id}.md").exists():
            bug_id, suffix = f"{base}-{suffix}", suffix + 1
        open_bugs = {k: d for k, d in snapshot.bugs.items() if str(d.frontmatter.get("status")) in OPEN_BUG_STATUSES}
        similar = similar_ideas(open_bugs, title, symptom, field="symptom")
        open_tasks = {k: d for k, d in snapshot.tasks.items() if d.frontmatter.get("status") in OPEN_TASK_STATUSES}
        similar += [
            task_id for task_id in similar_ideas(open_tasks, title, symptom, field="description") if task_id not in similar
        ]
        frontmatter = {
            "id": bug_id,
            "title": title,
            "status": "open",
            "severity": severity,
            "related_initiative": related or None,
            "symptom": symptom,
            "expected": fields.get("expected") or None,
            "repro": fields.get("repro") or None,
            "where": fields.get("where") or None,
            "evidence": [str(e) for e in (fields.get("evidence") or []) if str(e).strip()],
            "source": source,
            "similar_to": similar,
            "created": (today or dt.date.today()).isoformat(),
            "fix_card": None,
            "reason": None,
        }
        text = dump_markdown(frontmatter, str(fields.get("body") or "").strip() + "\n")
        bugs_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(bugs_dir / f"{bug_id}.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    return frontmatter


def update_bug(
    root: Path, bug_id: str, *, status: str | None = None, fix_card: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Set a bug's status, fix card, or reason in one locked write; refuse a combination the validator rejects."""
    doc = load(root).bugs.get(bug_id)
    if doc is None:
        raise WriteRefused(f"unknown bug '{bug_id}'")
    if status is not None and status not in BUG_STATUSES:
        raise WriteRefused(f"status must be one of {', '.join(BUG_STATUSES)}")
    if fix_card is not None and not CARD_ID.fullmatch(fix_card):
        raise WriteRefused(f"fix_card '{fix_card}' must match t_xxxxxxxx")
    fields: dict[str, Any] = {}
    if status is not None:
        fields["status"] = status
    if fix_card is not None:
        fields["fix_card"] = fix_card
    if reason is not None:
        fields["reason"] = reason.strip() or None
    if not fields:
        raise WriteRefused("nothing to change")
    after_fm = {**doc.frontmatter, **fields}
    final = str(after_fm.get("status"))
    if final == "fixing" and not after_fm.get("fix_card"):
        raise WriteRefused("status 'fixing' needs a fix card")
    if final == "wontfix" and not after_fm.get("reason"):
        raise WriteRefused("status 'wontfix' needs a reason")
    if final == "fixed" and not (after_fm.get("fix_card") or after_fm.get("reason")):
        raise WriteRefused("status 'fixed' needs a fix card or a reason")
    after = edit_frontmatter(doc.path, root, lambda fm: fm.update(fields))
    wrong = {k: after.get(k) for k, v in fields.items() if after.get(k) != v}
    if wrong:
        raise WriteRefused(f"{bug_id} read back with {wrong}")
    return after
