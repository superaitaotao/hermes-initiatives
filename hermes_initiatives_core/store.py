"""Load one initiative root into a snapshot: initiatives, cycles, observation logs, ideas, and bugs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .docs import Document, DocumentError, ParseCache, parse_jsonl, parse_markdown


@dataclass
class ObservationLog:
    path: Path
    rows: list[dict[str, Any]]
    errors: list[str]
    active: list[dict[str, Any]] | None = None


@dataclass
class Snapshot:
    root: Path
    initiatives: dict[str, Document] = field(default_factory=dict)
    cycles: dict[str, Document] = field(default_factory=dict)
    cycle_initiative: dict[str, str] = field(default_factory=dict)
    observations: dict[str, ObservationLog] = field(default_factory=dict)
    ideas: dict[str, Document] = field(default_factory=dict)
    bugs: dict[str, Document] = field(default_factory=dict)
    tasks: dict[str, Document] = field(default_factory=dict)
    parse_errors: dict[Path, str] = field(default_factory=dict)
    duplicate_ids: list[tuple[str, Path, Path]] = field(default_factory=list)
    other_folders: list[str] = field(default_factory=list)


_CACHE = ParseCache()


def _parse_md(text: str, path: Path) -> Document:
    return parse_markdown(text, path)


def _parse_log(text: str, path: Path) -> ObservationLog:
    rows, errors = parse_jsonl(text)
    return ObservationLog(path=path, rows=rows, errors=errors)


def _doc(path: Path, snapshot: Snapshot, live: set[str]) -> Document | None:
    live.add(str(path))
    value = _CACHE.get(path, _parse_md)
    if isinstance(value, (DocumentError, UnicodeDecodeError)):
        snapshot.parse_errors[path] = str(value)
        return None
    return value


def _put(mapping: dict[str, Document], doc: Document, snapshot: Snapshot) -> str | None:
    doc_id = str(doc.frontmatter.get("id") or "")
    if not doc_id:
        snapshot.parse_errors[doc.path] = "frontmatter has no id"
        return None
    if doc_id in mapping:
        snapshot.duplicate_ids.append((doc_id, mapping[doc_id].path, doc.path))
        return None
    mapping[doc_id] = doc
    return doc_id


def load(root: Path) -> Snapshot:
    """Read every initiative file under ``root``; unchanged files come from the parse cache."""
    snapshot = Snapshot(root=root)
    live: set[str] = set()
    if not root.is_dir():
        return snapshot
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in {"templates", "ideas", "bugs", "tasks"}:
            continue
        brief = child / "initiative.md"
        if not brief.is_file():
            snapshot.other_folders.append(child.name)
            continue
        initiative = _doc(brief, snapshot, live)
        initiative_id = _put(snapshot.initiatives, initiative, snapshot) if initiative else None
        cycles_dir = child / "cycles"
        for cycle_path in sorted(cycles_dir.glob("*.md")) if cycles_dir.is_dir() else ():
            cycle = _doc(cycle_path, snapshot, live)
            cycle_id = _put(snapshot.cycles, cycle, snapshot) if cycle else None
            if cycle_id and initiative_id:
                snapshot.cycle_initiative[cycle_id] = initiative_id
            elif cycle_id:
                snapshot.cycle_initiative[cycle_id] = child.name
        obs_dir = child / "observations"
        for log_path in sorted(obs_dir.glob("*.jsonl")) if obs_dir.is_dir() else ():
            live.add(str(log_path))
            snapshot.observations[log_path.stem] = _CACHE.get(log_path, _parse_log)
    ideas_dir = root / "ideas"
    for idea_path in sorted(ideas_dir.glob("*.md")) if ideas_dir.is_dir() else ():
        idea = _doc(idea_path, snapshot, live)
        if idea:
            _put(snapshot.ideas, idea, snapshot)
    bugs_dir = root / "bugs"
    if not bugs_dir.is_symlink():
        for bug_path in sorted(bugs_dir.glob("*.md")):
            if bug_path.is_symlink() or not bug_path.is_file():
                continue
            bug = _doc(bug_path, snapshot, live)
            if bug:
                _put(snapshot.bugs, bug, snapshot)
    tasks_dir = root / "tasks"
    if not tasks_dir.is_symlink():
        for task_path in sorted(tasks_dir.glob("*.md")):
            if task_path.is_symlink() or not task_path.is_file():
                continue
            task = _doc(task_path, snapshot, live)
            if task:
                _put(snapshot.tasks, task, snapshot)
    _CACHE.prune(live)
    return snapshot


def cache_parse_count() -> int:
    return _CACHE.parses


def active_observations(log: ObservationLog | None) -> list[dict[str, Any]]:
    """Rows in time order, without rows that a later row ``supersedes``."""
    if log is None:
        return []
    if log.active is None:
        superseded = {str(row.get("supersedes")) for row in log.rows if row.get("supersedes")}
        rows = [row for row in log.rows if str(row.get("at")) not in superseded]
        log.active = sorted(rows, key=lambda row: str(row.get("at") or ""))
    return log.active
