"""Shared, profile-safe configuration for the bundled cron scripts."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Hermes' git-install venv is not always installed as an editable package. The
# interpreter selected at setup lives at <Hermes source>/venv/bin/python.
_source = Path(sys.executable).absolute().parents[2]
if (_source / "hermes_cli").is_dir() and str(_source) not in sys.path:
    sys.path.append(str(_source))

from hermes_initiatives_core.settings import Settings, hermes_root, load_settings  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def profile_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or hermes_root()).expanduser().resolve()


def board_path(board: str) -> Path:
    from hermes_initiatives_core.kanban import board_path as resolve

    return resolve(board)


@dataclass(frozen=True)
class Config:
    settings: Settings
    board: str
    repo: str | None
    workers: set[str]
    project: str | None


def resolve_config(*, project=None, board=None, repo=None, workers=None, settings=None) -> Config:
    if settings is None:
        settings, error = load_settings()
        if error:
            raise ValueError(error)
    selected = settings.project(project) if project else None
    if project and selected is None:
        raise ValueError(f"unknown project {project!r}")
    slug = board or (selected.kanban_board if selected else None)
    if not slug:
        raise ValueError("provide --board or --project with kanban_board configured")
    if Path(slug).name != slug or slug in (".", ".."):
        raise ValueError("board must be a slug, not a path")
    return Config(settings, slug, repo if repo is not None else (selected.github_repo if selected else None),
                  set(workers if workers is not None else (selected.worker_profiles if selected else [])), project)


def add_config_arguments(parser, *, github=False):
    parser.add_argument("--project", help="project id in initiatives settings.json")
    parser.add_argument("--board", help="Kanban board slug (overrides the project)")
    if github:
        parser.add_argument("--repo", help="GitHub owner/repo (overrides the project)")
        parser.add_argument("--worker", action="append", default=None, help="worker profile (repeatable)")


def job_id(value: str, home: Path | None = None) -> str:
    """Resolve name:NAME at runtime, after Hermes assigned the job id during creation."""
    if not value.startswith("name:"):
        if not value or Path(value).name != value or value in (".", ".."):
            raise ValueError("invalid job id")
        return value
    path = (home or profile_home()) / "cron/jobs.json"
    data = __import__("json").loads(path.read_text())
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    found = [str(j["id"]) for j in jobs if j.get("name") == value[5:]]
    if len(found) != 1:
        raise ValueError(f"expected exactly one cron job named {value[5:]!r}, got {len(found)}")
    return job_id(found[0])
