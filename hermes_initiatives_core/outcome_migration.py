"""Rerunnable migration that adds the closed-loop ``outcome_review`` contract to open cycles.

Dry run by default. ``migrate(...)`` reports before/after and writes nothing unless ``apply=True``.
An applied edit goes through :func:`hermes_initiatives_core.writers.edit_frontmatter`, so it happens
under the root write lock; the callback checks the file it just read still matches the snapshot the
plan was built from and refuses to merge over a concurrent edit. Every user-owned field, the body,
prior decisions, measurement, result, disposition, stage, rank and card links are preserved. Missing
metric, source, criteria, guardrail or window fields become explicit contract gaps, never invented
values. A second apply is a no-op: files already carrying the contract are not rewritten, and the
adoption decision is matched by its text before it is appended.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import writers
from .docs import parse_markdown
from .lifecycle import as_date, as_list
from .model import CLOSED_STAGES, OPEN_STAGES
from .store import active_observations, load

SURFACES = ("production", "main_ci", "runtime", "manual")

# One initiative-level row, written once. Matched by text, so a rerun from any session is a no-op.
ADOPTION_DECISION = (
    "Adopt the closed-loop outcome review workflow (plans/closed-loop-outcomes.md): every open cycle gets "
    "an outcome_review contract assessed in a separate assessment card, and goals, targets, criteria, "
    "measurement windows, verdicts and dispositions stay user-owned."
)

# The fields a collector can actually act on. Absent or unusable ones are reported, never filled.
MEASUREMENT_FIELDS = ("primary_metric", "source", "baseline", "success_criteria", "guardrails")

SURFACE_LABEL = {
    "production": "live production deployment that serves users",
    "main_ci": "main CI workflow run for the exact pushed commit",
    "runtime": "running scheduler, worker, daemon or agent runtime",
    "manual": "named manual check that the assessment must describe",
}

ENVIRONMENT_SURFACES = {
    "production": "production",
    "prod": "production",
    "live": "production",
    "main ci": "main_ci",
    "main_ci": "main_ci",
    "main-ci": "main_ci",
    "ci": "main_ci",
    "ci.yml": "main_ci",
    "github actions": "main_ci",
    "runtime": "runtime",
    "worker": "runtime",
    "daemon": "runtime",
    "scheduler": "runtime",
    "cron": "runtime",
}

PRODUCTION_TERMS = re.compile(r"\b(?:production|prod|cloud run|deploy(?:ed|ment|s)?|promot(?:e|ed|ion)|live|gke|cutover)\b", re.I)
MAIN_CI_TERMS = re.compile(r"\b(?:ci|ci\.ya?ml|github actions|workflow run|browser-tests|main run)\b", re.I)
RUNTIME_TERMS = re.compile(r"\b(?:runtime|worker|daemon|cron|scheduler|background job|agent session|bench)\b", re.I)
AMBIGUOUS_TERMS = re.compile(
    r"\b(?:staging|shadow|canary|pre-?prod(?:uction)?|integration-vm|private (?:node|host|server))\b", re.I
)
# A repository-relative script path: any directory chain (``scripts/x.py``, ``backend/tools/x.py``,
# ``project/agents/scripts/x.py``, ``origin/main:backend/x.py``) ending in a script, or a virtualenv binary.
# URL paths and absolute paths are not matched.
SCRIPT_COMMAND = re.compile(
    r"(?<![\w/.-])(?<!://)((?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)+[A-Za-z0-9_.-]+?"
    r"\.(?:py|sh|ts|tsx|js|mjs)|\.venv/bin/[a-z][A-Za-z0-9_.-]*)(?![\w/-])"
)

GENERATED_KEYS = ("outcome_review", "assessment_cards")


class MigrationRefused(ValueError):
    """The migration cannot run safely; nothing more was written."""


class _Conflict(MigrationRefused):
    """The file changed since the plan was built; the locked edit refused to merge over it."""


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MigrationRefused(f"{name} is required")
    return text


def _surfaces(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MigrationRefused("surfaces must be a mapping of cycle id to release surface")
    result: dict[str, str] = {}
    for cycle_id, surface in value.items():
        name = str(surface or "").strip()
        if name not in SURFACES:
            raise MigrationRefused(f"surface for '{cycle_id}' must be one of {', '.join(SURFACES)}")
        result[str(cycle_id)] = name
    return result


def _flat(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list | tuple)):
        return "; ".join(part for part in (_flat(item) for item in value) if part)
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_flat(item)}" for key, item in value.items() if _flat(item))
    return " ".join(str(value).split())


def _jsonable(value: Any) -> Any:
    """Dates a YAML file may hold become ISO strings, so the report always serialises."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list | tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _measurement(frontmatter: dict[str, Any]) -> dict[str, Any]:
    value = frontmatter.get("measurement")
    return value if isinstance(value, dict) else {}


def _window(frontmatter: dict[str, Any]) -> dict[str, Any]:
    value = _measurement(frontmatter).get("observation_window")
    return value if isinstance(value, dict) else {}


def contract_gaps(frontmatter: dict[str, Any]) -> list[str]:
    """Names of measurement fields an assessment cannot work from as recorded."""
    measurement = frontmatter.get("measurement")
    if not isinstance(measurement, dict) or not measurement:
        return ["measurement"]
    gaps = [name for name in MEASUREMENT_FIELDS if _flat(measurement.get(name)) == ""]
    window = measurement.get("observation_window")
    if not isinstance(window, dict) or not window:
        gaps.append("observation_window")
    else:
        if window.get("starts") in (None, ""):
            gaps.append("observation_window.starts")
        length = window.get("length_days")
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            gaps.append("observation_window.length_days")
    return gaps


def _evidence_text(frontmatter: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Only recorded cycle fields and observation metadata, so a surface claim is traceable."""
    parts = [frontmatter.get(name) for name in ("title", "question", "hypothesis")]
    measurement = _measurement(frontmatter)
    parts += [measurement.get("source"), measurement.get("primary_metric")]
    for name in ("delivery_plans", "sources", "change_register"):
        parts += as_list(frontmatter.get(name))
    for row in rows:
        parts += [row.get("environment"), row.get("evidence"), row.get("metric")]
    return _flat(parts)


def classify_surface(
    frontmatter: dict[str, Any], rows: list[dict[str, Any]], cycle_id: str, explicit: Mapping[str, str]
) -> tuple[str, list[str]]:
    """Release surface and the evidence that chose it. Unknown evidence stays ``manual``."""
    if cycle_id in explicit:
        return explicit[cycle_id], [f"explicit surface mapping for {cycle_id}"]
    for row in reversed(rows):
        environment = _flat(row.get("environment")).lower()
        if not environment:
            continue
        surface = ENVIRONMENT_SURFACES.get(environment)
        if surface is None:
            return "manual", [f"recorded environment '{environment}' is not a recognised release surface"]
        return surface, [f"recorded environment '{environment}' in an observation"]
    text = _evidence_text(frontmatter, rows)
    if not text:
        return "manual", ["no release evidence recorded; the surface must be identified first"]
    production = {match.group(0).lower() for match in PRODUCTION_TERMS.finditer(text)}
    ci = {match.group(0).lower() for match in MAIN_CI_TERMS.finditer(text)}
    runtime = {match.group(0).lower() for match in RUNTIME_TERMS.finditer(text)}
    ambiguous = {match.group(0).lower() for match in AMBIGUOUS_TERMS.finditer(text)}
    if ci and not (production or runtime or ambiguous):
        return "main_ci", ["evidence names main CI: " + ", ".join(sorted(ci))]
    if production and not (runtime or ci or ambiguous):
        return "production", ["evidence names production: " + ", ".join(sorted(production))]
    if runtime and not (production or ci or ambiguous):
        return "runtime", ["evidence names a runtime surface: " + ", ".join(sorted(runtime))]
    if production and runtime:
        # A runtime process observed in production is verified on the runtime surface, never claimed as
        # a production deployment on the strength of prose alone.
        return "runtime", ["evidence names both production and a runtime: " + ", ".join(sorted(production | runtime))]
    if ambiguous:
        return "manual", ["evidence names a non-production surface: " + ", ".join(sorted(ambiguous))]
    if production or ci or runtime:
        return "manual", ["conflicting surface evidence: " + ", ".join(sorted(production | ci | runtime))]
    return "manual", ["no production, main CI or runtime evidence recorded; manual check required"]


def _commands(frontmatter: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    text = _flat([frontmatter, _measurement(frontmatter), rows])
    found = {match.group(1) for match in SCRIPT_COMMAND.finditer(text)}
    return sorted(found)[:6]


def _value_or_gap(measurement: dict[str, Any], name: str) -> str:
    value = _flat(measurement.get(name))
    return value if value else f"NOT RECORDED ({name})"


def _window_text(frontmatter: dict[str, Any]) -> str:
    window = _window(frontmatter)
    if not window:
        return "NOT RECORDED (observation_window)"
    starts = window.get("starts")
    length = window.get("length_days")
    if starts in (None, "") or isinstance(length, bool) or not isinstance(length, int):
        return f"INCOMPLETE (starts: {_flat(starts) or 'missing'}, length_days: {_flat(length) or 'missing'})"
    return f"starts {starts}, length {length} days"


def _collector(
    cycle_id: str,
    surface: str,
    frontmatter: dict[str, Any],
    rows: list[dict[str, Any]],
    gaps: list[str],
) -> str:
    measurement = _measurement(frontmatter)
    sources = ", ".join(_flat(item) for item in as_list(frontmatter.get("sources"))) or "the cycle's plan"
    commands = _commands(frontmatter, rows)
    lines = [
        f"Assess cycle {cycle_id} on the {surface} surface ({SURFACE_LABEL[surface]}). Delivery is not outcome proof.",
        "Recorded contract: "
        f"primary metric: {_value_or_gap(measurement, 'primary_metric')}; "
        f"source: {_value_or_gap(measurement, 'source')}; "
        f"baseline: {_value_or_gap(measurement, 'baseline')}; "
        f"observation window: {_window_text(frontmatter)}; "
        f"success criteria: {_value_or_gap(measurement, 'success_criteria')}; "
        f"guardrails: {_value_or_gap(measurement, 'guardrails')}.",
        "The recorded observation window controls sample maturity: do not evaluate before it ends unless the "
        "cycle records a spot check, and do not treat a merged or completed delivery card as the outcome.",
        f"Verify the surface before collecting: {surface} means {SURFACE_LABEL[surface]}. If the recorded "
        "evidence points somewhere else, report the mismatch instead of broadening the claim.",
    ]
    if commands:
        lines.append(
            "Commands recorded in this cycle's evidence: "
            + ", ".join(commands)
            + f". Use only commands you verify exist; discover any others from {sources}."
        )
    else:
        lines.append(
            f"No command is recorded in this cycle's evidence. Discover the exact commands from {sources} "
            "before collecting; never invent a command."
        )
    if gaps:
        lines.append(
            "Known contract gaps: " + ", ".join(gaps) + ". Report them with the assessment and propose the "
            "missing contract; do not fill them with a guessed value."
        )
    lines += [
        "Record real metric and guardrail evidence through the initiatives skill: "
        f"record_observation.py --cycle {cycle_id} --kind metric|guardrail with --metric, --value, "
        "--evidence-date and an --evidence report path.",
        "Compare every approved criterion and guardrail. Missing criteria or evidence require a diagnostic "
        "assessment (insufficient_data or measurement_failed), never an invented commitment. For an approved "
        "one-off spot check, judge that check from real evidence: empty optional metric, baseline, guardrail or "
        "window fields alone do not prevent met or missed. State the gaps and limitations separately; do not "
        "add a window or threshold merely to complete the template. Quote the exact approved criterion and "
        "its source before judging it.",
        "Finish with an evidence report, a bounded follow-up proposal and a receipt: "
        f"record_assessment.py --cycle {cycle_id} --status met|missed|insufficient_data|measurement_failed.",
        "Do not set a verdict, stage, disposition, target date, review date or successor cycle, and do not "
        "implement the proposal; those stay with the user.",
    ]
    return "\n".join(lines)


def _outcome_review(owner: str, surface: str, collector: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "owner": owner,
        "release_surface": surface,
        "collector": collector,
        "review_by": None,
        "target_date": None,
    }


def _state(frontmatter: dict[str, Any]) -> dict[str, Any]:
    result = frontmatter.get("result") if isinstance(frontmatter.get("result"), dict) else {}
    disposition = frontmatter.get("disposition") if isinstance(frontmatter.get("disposition"), dict) else {}
    return {
        "outcome_review": frontmatter.get("outcome_review"),
        "assessment_cards": as_list(frontmatter.get("assessment_cards")) if "assessment_cards" in frontmatter else None,
        "stage": frontmatter.get("stage"),
        "verdict": result.get("verdict"),
        "disposition": disposition.get("action"),
        "measurement": frontmatter.get("measurement"),
        "decisions": len(as_list(frontmatter.get("decisions"))),
        "kanban_cards": len(as_list(frontmatter.get("kanban_cards"))),
    }


def _planned(frontmatter: dict[str, Any], outcome_review: dict[str, Any]) -> dict[str, Any]:
    after = copy.deepcopy(frontmatter)
    after["outcome_review"] = outcome_review
    if "assessment_cards" not in after:
        after["assessment_cards"] = []
    return after


def _check_preserved(before: dict[str, Any], after: dict[str, Any], cycle_id: str) -> None:
    for key, value in before.items():
        if key in GENERATED_KEYS:
            continue
        if after.get(key) != value:
            raise MigrationRefused(f"{cycle_id}: planning would change preserved field '{key}'")


def _check_unchanged(
    current: dict[str, Any], expected: dict[str, Any], generated: tuple[str, ...], tolerated: tuple[str, ...] = ()
) -> None:
    for key, value in expected.items():
        if key in generated:
            continue
        if current.get(key) != value:
            raise _Conflict(f"{key} changed on disk since the plan was built; re-run the migration")
    extra = sorted(set(current) - set(expected) - set(generated) - set(tolerated))
    if extra:
        raise _Conflict("unexpected new fields on disk: " + ", ".join(extra))


def _write_cycle(
    path: Path,
    root: Path,
    before: dict[str, Any],
    before_body: str,
    outcome_review: dict[str, Any],
    cycle_id: str,
) -> None:
    """Add the contract in one locked edit, refusing to merge over a file that moved since the plan."""

    def change(frontmatter: dict[str, Any]) -> None:
        _check_unchanged(frontmatter, before, ("outcome_review",), ("assessment_cards",))
        if "outcome_review" in frontmatter:
            raise _Conflict(f"{cycle_id} already has outcome_review on disk; nothing to merge")
        frontmatter["outcome_review"] = copy.deepcopy(outcome_review)
        if "assessment_cards" not in frontmatter:
            frontmatter["assessment_cards"] = []

    after = writers.edit_frontmatter(path, root, change)
    if after.get("outcome_review") != outcome_review:
        raise MigrationRefused(f"{cycle_id}: read-back shows a different outcome_review")
    if "assessment_cards" not in after:
        raise MigrationRefused(f"{cycle_id}: read-back has no assessment_cards")
    if "assessment_cards" in before and as_list(after.get("assessment_cards")) != as_list(before["assessment_cards"]):
        raise MigrationRefused(f"{cycle_id}: read-back changed assessment_cards")
    for key, value in before.items():
        if key in GENERATED_KEYS:
            continue
        if after.get(key) != value:
            raise MigrationRefused(f"{cycle_id}: read-back changed preserved field '{key}'")
    extra = sorted(set(after) - set(before) - set(GENERATED_KEYS))
    if extra:
        raise MigrationRefused(f"{cycle_id}: read-back has unexpected fields: {', '.join(extra)}")
    after_body = parse_markdown(path.read_text(encoding="utf-8"), path).body
    if after_body != before_body:
        raise MigrationRefused(f"{cycle_id}: body changed")


def _adoption_present(rows: list[Any]) -> bool:
    return any(isinstance(row, dict) and row.get("decision") == ADOPTION_DECISION for row in rows)


def _adoption_row(today: Any, session: str) -> dict[str, Any]:
    return {"date": today.isoformat(), "by": "user", "session": session, "decision": ADOPTION_DECISION}


def _policy_row(owner: str, session: str) -> dict[str, Any]:
    """The initiative-level adoption record: enabled, the assessment owner, and the approving session."""
    return {"enabled": True, "owner": owner, "session": session}


def _approval_session(rows: list[Any]) -> str:
    """The session that approved the adoption, so a repaired policy keeps the original authorization."""
    for row in rows:
        if isinstance(row, dict) and row.get("decision") == ADOPTION_DECISION and str(row.get("session") or "").strip():
            return str(row["session"]).strip()
    return ""


def _policy(frontmatter: dict[str, Any]) -> dict[str, Any] | None:
    value = frontmatter.get("outcome_policy")
    return value if isinstance(value, dict) else None


def _adopted(frontmatter: dict[str, Any]) -> bool:
    policy = _policy(frontmatter)
    return bool(policy and policy.get("enabled") is True)


def _initiative_owner(frontmatter: dict[str, Any], fallback: str) -> str:
    """The policy owner is the configured plan lane; a later run never changes an adopted owner."""
    policy = _policy(frontmatter)
    if policy and str(policy.get("owner") or "").strip():
        return str(policy["owner"]).strip()
    return fallback


def _write_adoption(
    path: Path,
    root: Path,
    before: dict[str, Any],
    row: dict[str, Any] | None,
    policy: dict[str, Any] | None,
) -> None:
    """One locked edit: append the adoption decision if absent, add the policy if absent, change nothing else."""
    expected = as_list(before.get("decisions"))
    expected_policy = before.get("outcome_policy")

    def change(frontmatter: dict[str, Any]) -> None:
        for key, value in before.items():
            if key in ("decisions", "outcome_policy"):
                continue
            if frontmatter.get(key) != value:
                raise _Conflict(f"{key} changed on disk since the plan was built; re-run the migration")
        extra = sorted(set(frontmatter) - set(before) - {"decisions", "outcome_policy"})
        if extra:
            raise _Conflict("unexpected new fields on disk: " + ", ".join(extra))
        current = as_list(frontmatter.get("decisions"))
        if current[: len(expected)] != expected:
            raise _Conflict("decisions changed on disk since the plan was built; re-run the migration")
        if frontmatter.get("outcome_policy") != expected_policy:
            raise _Conflict("outcome_policy changed on disk since the plan was built; re-run the migration")
        if row is not None and not _adoption_present(current):
            frontmatter["decisions"] = list(current) + [row]
        if policy is not None and frontmatter.get("outcome_policy") != policy:
            frontmatter["outcome_policy"] = copy.deepcopy(policy)

    after = writers.edit_frontmatter(path, root, change)
    rows = as_list(after.get("decisions"))
    if row is not None and not _adoption_present(rows):
        raise MigrationRefused("read-back does not show the adoption decision")
    if rows[: len(expected)] != expected:
        raise MigrationRefused("read-back changed an earlier decision")
    if policy is not None and after.get("outcome_policy") != policy:
        raise MigrationRefused("read-back shows a different outcome_policy")
    for key, value in before.items():
        if key in ("decisions", "outcome_policy"):
            continue
        if after.get(key) != value:
            raise MigrationRefused(f"read-back changed preserved field '{key}'")


def migrate(
    root: Path | str,
    owner: str = "",
    session: str = "",
    today: Any = None,
    apply: bool = False,
    surfaces: Mapping[str, str] | None = None,
    *,
    adopted_only: bool = False,
) -> dict[str, Any]:
    """Plan, and with ``apply`` write, the outcome_review contract for active/open cycles.

    Active initiatives only; ``paused``, ``closed``, ``concluded`` and ``abandoned`` records are never
    touched. Returns a JSON-serialisable report with per-cycle before/after, the evidence behind each
    release surface, contract gaps, the initiative policy and the adoption decision. An initial adoption
    writes one user decision row (dated ``today``, in ``session``) and the initiative ``outcome_policy``
    ``{enabled: true, owner, session}``: the owner is the assessment lane, and the session is the
    authorization every later run reuses instead of a new decision.

    ``adopted_only`` restricts the run to initiatives already carrying an enabled ``outcome_policy``;
    their policy owner and approval session are the authorization, so no owner, session or new
    decision row is needed and the initiative file is never rewritten. That is the daily-tick mode
    (:func:`maintain`).
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise MigrationRefused(f"initiative root not found: {base}")
    owner = str(owner or "").strip()
    session = str(session or "").strip()
    if not adopted_only:
        owner = _require_text(owner, "owner")
        session = _require_text(session, "session")
    day = as_date(today if today is not None else dt.date.today())
    if day is None:
        raise MigrationRefused("today must be an ISO date")
    explicit = _surfaces(surfaces)
    snapshot = load(base)

    active = {
        initiative_id: doc
        for initiative_id, doc in snapshot.initiatives.items()
        if str(doc.frontmatter.get("status") or "active") == "active"
    }
    owners = {initiative_id: _initiative_owner(doc.frontmatter, owner) for initiative_id, doc in active.items()}
    usable = {
        initiative_id: initiative_id
        for initiative_id in active
        if (_policy(active[initiative_id].frontmatter) is None or _adopted(active[initiative_id].frontmatter))
        and (not adopted_only or (_adopted(active[initiative_id].frontmatter) and owners[initiative_id]))
    }
    scoped: dict[str, list[str]] = {initiative_id: [] for initiative_id in active}
    skipped: list[dict[str, str]] = []
    cycles: list[dict[str, Any]] = []
    gaps: dict[str, list[str]] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for cycle_id in sorted(snapshot.cycles):
        doc = snapshot.cycles[cycle_id]
        frontmatter = doc.frontmatter
        initiative_id = snapshot.cycle_initiative.get(cycle_id)
        if initiative_id is None:
            skipped.append({"id": cycle_id, "reason": "cycle is not under a loaded initiative"})
            continue
        if initiative_id not in active:
            owner_doc = snapshot.initiatives.get(initiative_id)
            label = str(owner_doc.frontmatter.get("status")) if owner_doc is not None else "unloaded"
            skipped.append({"id": cycle_id, "reason": f"initiative '{initiative_id}' is {label}, not active"})
            continue
        if initiative_id not in usable:
            reason = (
                "initiative has no enabled outcome_policy"
                if adopted_only and not _adopted(active[initiative_id].frontmatter)
                else "outcome_policy has no owner"
            )
            skipped.append({"id": cycle_id, "reason": reason})
            continue
        stage = str(frontmatter.get("stage") or "exploring")
        if stage not in OPEN_STAGES:
            reason = f"stage '{stage}' is terminal" if stage in CLOSED_STAGES else f"stage '{stage}' is not open"
            skipped.append({"id": cycle_id, "reason": reason})
            continue
        scoped[initiative_id].append(cycle_id)

        rows = active_observations(snapshot.observations.get(cycle_id))
        surface, basis = classify_surface(frontmatter, rows, cycle_id, explicit)
        cycle_gaps = contract_gaps(frontmatter)
        if cycle_gaps:
            gaps[cycle_id] = cycle_gaps
        before = _state(frontmatter)
        existing = frontmatter.get("outcome_review")
        entry: dict[str, Any] = {
            "id": cycle_id,
            "initiative": initiative_id,
            "path": str(doc.path.relative_to(base)),
            "stage": stage,
            "release_surface": surface,
            "surface_basis": basis,
            "contract_gaps": cycle_gaps,
            "before": before,
            "changed": False,
        }
        if existing is not None:
            entry["status"] = "existing"
            entry["after"] = before
            if not isinstance(existing, dict):
                warnings.append(f"{cycle_id}: existing outcome_review is not a mapping; left untouched")
            else:
                missing = [
                    name
                    for name in ("enabled", "owner", "release_surface", "collector", "review_by", "target_date")
                    if name not in existing
                ]
                if missing:
                    warnings.append(f"{cycle_id}: existing outcome_review lacks {', '.join(missing)}; left untouched")
            cycles.append(entry)
            continue

        collector = _collector(cycle_id, surface, frontmatter, rows, cycle_gaps)
        outcome_review = _outcome_review(owners[initiative_id], surface, collector)
        after = _planned(frontmatter, outcome_review)
        _check_preserved(frontmatter, after, cycle_id)
        entry["after"] = _state(after)
        if not apply:
            entry["status"] = "would_adopt"
            cycles.append(entry)
            continue
        try:
            _write_cycle(doc.path, base, copy.deepcopy(frontmatter), doc.body, outcome_review, cycle_id)
        except _Conflict as exc:
            entry["status"] = "conflict"
            errors.append(f"{cycle_id}: {exc}")
            cycles.append(entry)
            continue
        entry["status"] = "adopted"
        entry["changed"] = True
        cycles.append(entry)

    initiatives: list[dict[str, Any]] = []
    for initiative_id in sorted(active):
        doc = active[initiative_id]
        frontmatter = doc.frontmatter
        rows = as_list(frontmatter.get("decisions"))
        existing_policy = _policy(frontmatter)
        entry: dict[str, Any] = {
            "id": initiative_id,
            "path": str(doc.path.relative_to(base)),
            "cycles": len(scoped[initiative_id]),
            "decision_added": False,
            "decision": None,
            "policy": existing_policy,
            "policy_added": False,
        }
        if not scoped[initiative_id]:
            entry["status"] = "no_open_cycles"
            initiatives.append(entry)
            continue
        if adopted_only:
            # maintain(): the recorded policy is the authorization, so the initiative file is never rewritten.
            entry["status"] = "existing"
            initiatives.append(entry)
            continue
        if existing_policy is not None and existing_policy.get("enabled") is True and _adoption_present(rows):
            entry["status"] = "existing"
            initiatives.append(entry)
            continue
        row = None if _adoption_present(rows) else _adoption_row(day, session)
        policy = {
            **(existing_policy if isinstance(existing_policy, dict) else {}),
            **_policy_row(_initiative_owner(frontmatter, owner), _approval_session(rows) or session),
        }
        entry["decision"] = row
        entry["policy"] = policy
        entry["policy_added"] = policy != existing_policy
        if not apply:
            entry["status"] = "would_adopt"
            initiatives.append(entry)
            continue
        blocked = [c["id"] for c in cycles if c["initiative"] == initiative_id and c["status"] == "conflict"]
        if blocked:
            entry["status"] = "blocked"
            entry["reason"] = "cycle write conflicted: " + ", ".join(blocked)
            initiatives.append(entry)
            continue
        try:
            _write_adoption(doc.path, base, copy.deepcopy(frontmatter), row, policy)
        except _Conflict as exc:
            entry["status"] = "conflict"
            errors.append(f"{initiative_id}: {exc}")
            initiatives.append(entry)
            continue
        entry["status"] = "adopted"
        entry["changed"] = True
        entry["decision_added"] = row is not None
        initiatives.append(entry)

    ignored = sorted(set(explicit) - {cycle["id"] for cycle in cycles})
    for cycle_id in ignored:
        warnings.append(f"{cycle_id}: explicit surface ignored; it is not an open cycle of an active initiative")

    def count(status: str) -> int:
        return sum(1 for cycle in cycles if cycle["status"] == status)

    report = {
        "ok": not errors,
        "root": str(base),
        "owner": owner,
        "session": session,
        "today": day.isoformat(),
        "apply": bool(apply),
        "surfaces_source": "explicit" if explicit else "evidence",
        "explicit_surfaces": dict(sorted(explicit.items())),
        "counts": {
            "initiatives_active": len(active),
            "initiatives_adopted": sum(1 for item in initiatives if item["status"] == "adopted"),
            "initiatives_unchanged": sum(1 for item in initiatives if item["status"] == "existing"),
            "cycles_open": len(cycles),
            "cycles_adopted": count("adopted"),
            "cycles_unchanged": count("existing"),
            "cycles_pending": count("would_adopt"),
            "cycles_conflicted": count("conflict"),
            "skipped": len(skipped),
            "cycles_with_contract_gaps": len(gaps),
        },
        "initiatives": initiatives,
        "cycles": cycles,
        "skipped": skipped,
        "gaps": gaps,
        "errors": errors,
        "warnings": warnings,
    }
    return _jsonable(report)


def maintain(root: Path | str, today: Any = None, apply: bool = True) -> dict[str, Any]:
    """Backfill the ``outcome_review`` contract for cycles of initiatives that already adopted the workflow.

    The daily tick calls this before dispatching assessments, so a cycle created or reopened after the
    initial migration also gets a contract. Only initiatives whose ``outcome_policy`` is enabled are
    touched; the policy's recorded owner and approval session are the authorization, so this never needs
    an owner or session argument and never appends a decision row. Unadopted or inactive initiatives and
    their cycles are reported as skipped, and a cycle already carrying a contract is left untouched, so a
    repeated run converges on the same files. ``apply=False`` reports what it would write without writing.

    Returns the same JSON-serialisable report as :func:`migrate`, plus ``"mode": "maintain"``; ``counts``
    and ``errors`` are the summary a caller should log.
    """
    report = migrate(root, today=today, apply=apply, adopted_only=True)
    report["mode"] = "maintain"
    return report
