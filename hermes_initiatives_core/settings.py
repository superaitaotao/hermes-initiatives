"""Plugin settings: which initiative roots exist and how they map to boards, folders, and the vault."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .docs import atomic_write

PLUGIN_NAME = "initiatives"
_PROJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Hermes' reasoning levels (hermes_constants.VALID_REASONING_EFFORTS) plus "none" (thinking off).
# "" means: use the profile's own setting.
Effort = Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class ProjectSettings(BaseModel):
    id: str
    label: str
    root: Path
    cwd: Path | None = None
    kanban_board: str | None = None
    vault_root: Path | None = None
    vault_name: str | None = None
    # Profile that admits new work on the board: a promoted idea's delivery card goes here unless the option names one.
    intake_assignee: str | None = None
    # Profile that writes plans. An agent may start planning an exploring cycle here (rule plan_exploring_cycle).
    plan_assignee: str | None = None
    plan_skills: list[str] = Field(default_factory=list)
    # The project's own policy skill (lanes, risk rubric, merge rules). Agents load it next to the bundled skills;
    # prose that cites "the project policy" means this skill. None: the bundled defaults apply.
    policy_skill: str | None = None
    # Kanban lane profiles that work this board. The recovery wake gate counts their comments as worker comments.
    worker_profiles: list[str] = Field(default_factory=list)
    # GitHub ``owner/repo`` whose PRs and main CI the recovery wake gate reads back. None: no GitHub readback.
    github_repo: str | None = None
    # Lane that blocks a passed review on a user merge approval (the merge gate filer files those). None: off.
    merge_reviewer: str | None = None

    @field_validator("id")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _PROJECT_KEY.fullmatch(value):
            raise ValueError("project id must be lowercase letters, digits, hyphens, and underscores (a Hermes Project id works)")
        return value

    @field_validator("github_repo")
    @classmethod
    def _repo(cls, value: str | None) -> str | None:
        if value and not _REPO.fullmatch(value):
            raise ValueError("github_repo must be owner/repo")
        return value or None


class AlertJob(BaseModel):
    """A cron job whose delivered output may ask the user something; the digest lists asks with no review item."""

    profile: str | None = None  # None: the default profile
    job: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    label: str = Field(max_length=200)


class Settings(BaseModel):
    projects: list[ProjectSettings] = Field(default_factory=list)
    # Where the daily review is posted and answered, as telegram:<chat>:<topic>. Answers are read only there.
    review_target: str | None = None
    # Telegram user ids allowed to answer with a button tap or a typed reply in review_target. Empty: a private
    # chat's own id (only its owner is in it); a group target then refuses every answer until you list approvers.
    review_approvers: list[str] = Field(default_factory=list)
    # How agent-written text names the person who owns the decisions ("stay with <owner_name>").
    owner_name: str = Field(default="the user", min_length=1, max_length=60)
    # IANA zone for the push window and the daily digest. Empty: the host's local zone.
    timezone: str = ""
    # Pushes go out from push_start_hour:00 until push_end_hour:00 (exclusive, 24 = midnight); later ones wait.
    push_start_hour: int = Field(default=8, ge=0, le=23)
    push_end_hour: int = Field(default=24, ge=1, le=24)
    # The daily digest is due from this local time ("HH:MM").
    digest_at: str = Field(default="08:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # Cron jobs scanned for asks that never became a review item (review gaps). Empty: no gap scan.
    alert_jobs: list[AlertJob] = Field(default_factory=list)

    # Set when review_approvers was present but unusable: answers are then refused, never handed to the default.
    approvers_invalid: bool = Field(default=False, exclude=True)

    @field_validator("review_approvers", mode="before")
    @classmethod
    def _approvers(cls, value: Any) -> list[str]:
        # Before type validation, so JSON numbers ([42]) are accepted as ids rather than failing as non-strings.
        if not isinstance(value, list):
            raise ValueError("review_approvers is a list of numeric Telegram user ids")
        cleaned = []
        for v in value:
            if isinstance(v, bool) or not isinstance(v, (str, int)):
                raise ValueError(f"review_approvers entries are numeric Telegram user ids, got {v!r}")
            item = str(v).strip()
            if not re.fullmatch(r"\d{1,20}", item):
                raise ValueError(f"review_approvers entries are numeric Telegram user ids, got {item!r}")
            cleaned.append(item)
        return cleaned

    def answer_approvers(self) -> set[str] | None:
        """Who may answer in the review target: the listed ids, else a private chat's own id. None: nobody.

        A ``review_approvers`` value that failed validation refuses everyone (fail closed): falling back to the
        chat owner would hand the decision to someone the user did not list.
        """
        if self.approvers_invalid:
            return None
        if self.review_approvers:
            return set(self.review_approvers)
        parts = (self.review_target or "").split(":")
        if len(parts) == 3 and parts[1].isdigit():  # positive id = a private chat with that user
            return {parts[1]}
        return None

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _window(self) -> Settings:
        if self.push_end_hour <= self.push_start_hour:
            raise ValueError("push_end_hour must be after push_start_hour")
        return self

    def project(self, project_id: str) -> ProjectSettings | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def zone(self) -> dt.tzinfo:
        """The review's timezone: the configured IANA zone, else the host's local zone with its DST rules."""
        return ZoneInfo(self.timezone) if self.timezone else host_zone()

    def zone_label(self) -> str:
        return self.timezone or getattr(host_zone(), "key", None) or "local time"

    def digest_time(self) -> dt.time:
        hh, mm = self.digest_at.split(":")
        return dt.time(int(hh), int(mm))

    def push_hours(self) -> range:
        return range(self.push_start_hour, self.push_end_hour)

    def push_window_label(self) -> str:
        return f"{self.push_start_hour:02d}:00-{self.push_end_hour - 1:02d}:59 {self.zone_label()}"


def host_zone() -> dt.tzinfo:
    """The host's IANA zone (``TZ``, then ``/etc/localtime``), so DST follows the date converted.

    ``datetime.now().astimezone().tzinfo`` is a fixed offset frozen at call time; it converts a summer timestamp
    with the winter offset. The fixed offset stays only as the last resort for hosts with no zone database entry.
    """
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            candidates.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    for name in candidates:
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return dt.datetime.now().astimezone().tzinfo or dt.UTC


def hermes_root() -> Path:
    """The Hermes root shared by every profile on this host (``~/.hermes``, not a profile home).

    Initiative settings and session links are host-level: every profile reads the same project roots,
    and each launch records its own profile, so nothing here is tied to one profile.
    """
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        return home.parent.parent if home.parent.name == "profiles" else home


def data_dir() -> Path:
    override = os.environ.get("HERMES_INITIATIVES_DATA_DIR", "").strip()
    root = Path(override) if override else hermes_root() / "plugin-data" / PLUGIN_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_path() -> Path:
    return data_dir() / "settings.json"


class SessionDefaults(BaseModel):
    """Profile, model and reasoning effort for every session the page starts. Empty profile = ask each time;
    empty model or effort = that profile's own setting."""

    profile: str = Field(default="", max_length=64, pattern=r"^(|[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$")
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=200)
    reasoning_effort: Effort = ""

    @model_validator(mode="after")
    def _provider_needs_model(self) -> SessionDefaults:
        self.model, self.provider, self.profile = self.model.strip(), self.provider.strip(), self.profile.strip()
        if self.provider and not self.model:
            raise ValueError("a provider needs a model")
        return self


def session_defaults_path() -> Path:
    return data_dir() / "session-defaults.json"


def load_session_defaults() -> SessionDefaults:
    path = session_defaults_path()
    if not path.exists():
        return SessionDefaults()
    try:
        return SessionDefaults.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return SessionDefaults()


def save_session_defaults(defaults: SessionDefaults) -> SessionDefaults:
    atomic_write(session_defaults_path(), defaults.model_dump_json(indent=1) + "\n")
    return defaults


# Fields added for the open-source release. A bad value here must not cost the user every project: each is dropped
# back to its default and reported, and the rest of the file still loads (an older file without them is valid).
_OPTIONAL_GLOBALS = ("review_approvers", "owner_name", "timezone", "push_start_hour", "push_end_hour", "digest_at", "alert_jobs")
_OPTIONAL_PROJECT = ("policy_skill", "worker_profiles", "github_repo", "merge_reviewer")


def _field_errors(exc: ValidationError) -> set[tuple[Any, ...]]:
    return {tuple(e.get("loc") or ()) for e in exc.errors()}


def load_settings() -> tuple[Settings, str | None]:
    path = settings_path()
    if not path.exists():
        return Settings(), None
    return read_settings_file(path)


def read_settings_file(path: Path) -> tuple[Settings, str | None]:
    """Parse one settings file without creating the data directory. Same contract as ``load_settings``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Settings(), f"{path}: {exc}"
    try:
        return Settings.model_validate(raw), None
    except ValidationError as exc:
        first = exc
    if not isinstance(raw, dict):
        return Settings(), f"{path}: {first}"
    # Drop only the optional fields that failed (a cross-field window error drops both hours), then retry once.
    cleaned = json.loads(json.dumps(raw))
    dropped: list[str] = []
    for loc in _field_errors(first):
        if loc and loc[0] in _OPTIONAL_GLOBALS:
            dropped.append(str(loc[0]))
            cleaned.pop(loc[0], None)
        elif not loc and any(k in cleaned for k in ("push_start_hour", "push_end_hour")):
            dropped += ["push_start_hour", "push_end_hour"]
            cleaned.pop("push_start_hour", None)
            cleaned.pop("push_end_hour", None)
        elif len(loc) >= 3 and loc[0] == "projects" and isinstance(loc[1], int) and loc[2] in _OPTIONAL_PROJECT:
            projects = cleaned.get("projects")
            if isinstance(projects, list) and loc[1] < len(projects) and isinstance(projects[loc[1]], dict):
                projects[loc[1]].pop(loc[2], None)
                dropped.append(f"projects[{loc[1]}].{loc[2]}")
    if dropped:
        if "review_approvers" in dropped:
            cleaned["approvers_invalid"] = True
        try:
            settings = Settings.model_validate(cleaned)
        except ValidationError:
            pass
        else:
            return settings, f"{path}: invalid {', '.join(sorted(set(dropped)))} ignored (defaults used): {first}"
    return Settings(), f"{path}: {first}"
