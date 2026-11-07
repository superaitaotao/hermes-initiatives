"""Per-file and cross-file validation of an initiative root. One validator for backend and scripts."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lifecycle import as_date, as_list, release_state
from .model import (
    AGENT_RULES,
    ASSESSMENT_STATUSES,
    BUG_ID,
    BUG_SEVERITIES,
    BUG_STATUSES,
    CARD_ID,
    CHANGE_ROW_ID,
    CYCLE_ID,
    DISPOSITIONS,
    IDEA_ID,
    IDEA_SIZES,
    IDEA_STATUSES,
    INITIATIVE_ID,
    INITIATIVE_STATUSES,
    MAX_OPEN_IDEAS_PER_SOURCE,
    MAX_PRE_SHIPPING_CYCLES_PER_INITIATIVE,
    MEASUREMENT_FIELDS,
    OBSERVATION_KINDS,
    OPEN_IDEA_STATUSES,
    STAGES,
    TASK_ID,
    TASK_PRIORITIES,
    TASK_STATUSES,
    TASK_TYPES,
    VERDICTS,
    is_agent_rule,
)
from .store import Snapshot, active_observations


@dataclass
class Report:
    errors: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    warnings: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def error(self, key: str, message: str) -> None:
        self.errors[key].append(message)

    def warn(self, key: str, message: str) -> None:
        self.warnings[key].append(message)

    @property
    def ok(self) -> bool:
        return not any(self.errors.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": {k: v for k, v in self.errors.items() if v},
            "warnings": {k: v for k, v in self.warnings.items() if v},
        }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decisions(fm: dict[str, Any], key: str, report: Report) -> list[dict[str, Any]]:
    rows = fm.get("decisions")
    if rows is None:
        return []
    if not isinstance(rows, list):
        report.error(key, "decisions must be a list")
        return []
    good = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("decision") or not row.get("date"):
            report.error(key, f"decisions[{index}] needs date and decision")
            continue
        by = row.get("by")
        if by == "backfill":
            if not row.get("evidence"):
                report.error(key, f"decisions[{index}]: a backfilled decision needs evidence")
            report.warn(key, f"decisions[{index}] is backfilled from history; confirm it as a user decision")
        elif by == "agent":
            if not is_agent_rule(row.get("rule")) or not row.get("review"):
                rules = ", ".join(AGENT_RULES) + ", or learned_<name>"
                report.error(key, f"decisions[{index}]: an agent decision needs a delegated rule ({rules}) and its review item")
            continue  # recorded for audit; never the user decision a stage needs
        elif by != "user":
            report.error(key, f"decisions[{index}].by must be 'user', 'backfill', or 'agent' with a delegated rule")
        good.append(row)
    return good


def _enum(fm: dict[str, Any], name: str, allowed: tuple[str, ...], key: str, report: Report, default: str) -> str:
    value = fm.get(name, default)
    if value is None:
        report.error(key, f"{name} is null; use one of {', '.join(allowed)}")
        return default
    if str(value) not in allowed:
        report.error(key, f"{name} '{value}' is not one of {', '.join(allowed)}")
        return default
    return str(value)


def _initiative(fm: dict[str, Any], key: str, report: Report) -> None:
    if not INITIATIVE_ID.fullmatch(str(fm.get("id"))):
        report.error(key, "id must match in-<slug>")
    if not fm.get("title"):
        report.error(key, "title is required")
    _enum(fm, "status", INITIATIVE_STATUSES, key, report, "active")
    rank = fm.get("rank")
    if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int)):
        report.error(key, "rank must be an integer")
    observe = fm.get("observe_every_days")
    if observe is not None and (isinstance(observe, bool) or not isinstance(observe, int) or observe < 1):
        report.error(key, "observe_every_days must be a positive integer")
    for index, watcher in enumerate(as_list(fm.get("watchers"))):
        if not isinstance(watcher, dict) or not watcher.get("profile") or not watcher.get("job"):
            report.error(key, f"watchers[{index}] needs profile and job")
    policy = fm.get("outcome_policy")
    if policy is not None:
        if not isinstance(policy, dict):
            report.error(key, "outcome_policy must be a mapping")
        else:
            if not isinstance(policy.get("enabled"), bool):
                report.error(key, "outcome_policy.enabled must be boolean")
            if policy.get("enabled") and not all(str(policy.get(name) or "").strip() for name in ("owner", "session")):
                report.error(key, "enabled outcome_policy needs owner and approval session")
    _decisions(fm, key, report)


def _window_ok(window: Any) -> bool:
    """Only a mapping with an integer day count yields an evaluation date (lifecycle.evaluation_date)."""
    if not isinstance(window, dict):
        return False
    length = window.get("length_days")
    starts = window.get("starts")
    return (
        isinstance(length, int)
        and not isinstance(length, bool)
        and length >= 0
        and (starts in (None, "verified_release") or as_date(starts) is not None)
    )


def measurement_problems(measurement: Any) -> dict[str, str]:
    """Field-keyed contract problems; null window with criteria means a one-off spot-check."""
    measurement = _mapping(measurement)
    problems = {
        name: f"measuring needs measurement fields: {name}" for name in MEASUREMENT_FIELDS[:3] if not measurement.get(name)
    }
    window = measurement.get("observation_window")
    if window is None:
        if not measurement.get("success_criteria"):
            problems["observation_window"] = "measuring needs measurement fields: observation_window"
    elif not _window_ok(window):
        problems["observation_window"] = "observation_window must be a mapping with integer length_days (and valid starts)"
    return problems


def cycle_errors(fm: dict[str, Any], observations: list[dict[str, Any]]) -> list[str]:
    """The per-file errors of one cycle, for writers that must not make a cycle invalid."""
    report = Report()
    _cycle(fm, "cycle", observations, report)
    return list(report.errors.get("cycle", []))


def cycle_edit_errors(before: dict[str, Any], after: dict[str, Any], observations: list[dict[str, Any]]) -> list[str]:
    """Refuse new errors; a lifecycle transition must satisfy all destination requirements.

    Legacy errors do not prevent recording facts/decisions or repairing one field at a time.
    """
    errors = cycle_errors(after, observations)
    if before.get("stage") != after.get("stage"):
        return errors
    previous = set(cycle_errors(before, observations))
    added = [e for e in errors if e not in previous]
    before_m, after_m = _mapping(before.get("measurement")), _mapping(after.get("measurement"))
    # Even a legacy-invalid field cannot be replaced with another invalid value having the same diagnostic.
    for name, problem in measurement_problems(after_m).items():
        if before_m.get(name) != after_m.get(name) and problem not in added:
            added.append(problem)
    return added


def _cycle(fm: dict[str, Any], key: str, observations: list[dict[str, Any]], report: Report) -> None:
    if not CYCLE_ID.fullmatch(str(fm.get("id"))):
        report.error(key, "id must match <initiative-id>-cNN")
    if not fm.get("title"):
        report.error(key, "title is required")
    stage = _enum(fm, "stage", STAGES, key, report, "exploring")
    decisions = _decisions(fm, key, report)
    for name in ("kanban_cards", "excluded_cards", "assessment_cards"):
        for card in as_list(fm.get(name)):
            if not CARD_ID.fullmatch(str(card)):
                report.error(key, f"{name} entry '{card}' must match t_xxxxxxxx")
    for row in as_list(fm.get("change_register")):
        if not CHANGE_ROW_ID.fullmatch(str(row)):
            report.error(key, f"change register row '{row}' must match CNN")
    result = _mapping(fm.get("result"))
    disposition = _mapping(fm.get("disposition"))
    if "result" in fm and not isinstance(fm.get("result"), dict):
        report.error(key, "result must be a mapping")
    if "disposition" in fm and not isinstance(fm.get("disposition"), dict):
        report.error(key, "disposition must be a mapping")
    verdict = _enum(result, "verdict", VERDICTS, key, report, "unset")
    action = _enum(disposition, "action", DISPOSITIONS, key, report, "unset")
    loop = fm.get("outcome_review")
    if loop is not None:
        if not isinstance(loop, dict):
            report.error(key, "outcome_review must be a mapping")
        else:
            if not isinstance(loop.get("enabled"), bool):
                report.error(key, "outcome_review.enabled must be a boolean")
            if loop.get("enabled"):
                for name in ("owner", "collector"):
                    if not isinstance(loop.get(name), str) or not loop[name].strip():
                        report.error(key, f"outcome_review.{name} is required")
                if loop.get("release_surface") not in ("production", "main_ci", "runtime", "manual"):
                    report.error(key, "outcome_review.release_surface is invalid")
                for name in ("review_by", "target_date"):
                    if loop.get(name) is not None and as_date(loop[name]) is None:
                        report.error(key, f"outcome_review.{name} must be an ISO date or null")
                if not loop.get("review_by"):
                    report.warn(key, "no agreed outcome review deadline; assessment must propose one")
    if set(as_list(fm.get("assessment_cards"))) & set(as_list(fm.get("kanban_cards"))):
        report.error(key, "assessment cards must not be delivery cards")
    measurement = _mapping(fm.get("measurement"))
    if stage != "exploring" and not decisions:
        report.error(key, f"stage '{stage}' needs at least one user decision")
    release = release_state(observations)
    if stage == "measuring" or release["state"] == "verified":
        for problem in measurement_problems(measurement).values():
            report.error(key, problem)
        criteria = [name for name in MEASUREMENT_FIELDS[4:] if not measurement.get(name)]
        if criteria:
            report.warn(key, "no agreed " + ", ".join(criteria) + " before evaluation")
    if stage == "measuring" and release["state"] != "verified":
        report.error(key, "stage 'measuring' needs a release_verified observation")
    if stage == "concluded":
        if verdict == "unset":
            report.error(key, "concluded needs a verdict other than unset")
        if action == "unset":
            report.error(key, "concluded needs a disposition action other than unset")
        if not as_list(result.get("evidence")):
            report.error(key, "concluded needs at least one evidence link in result.evidence")
    if stage == "abandoned" and not disposition.get("reason"):
        report.error(key, "abandoned needs disposition.reason")


def _observation_rows(log, key: str, report: Report) -> None:
    for message in log.errors:
        report.error(key, message)
    seen = set()
    for index, row in enumerate(log.rows):
        if row.get("kind") not in OBSERVATION_KINDS:
            report.error(key, f"row {index + 1}: kind must be one of {', '.join(OBSERVATION_KINDS)}")
        if not row.get("at") or not row.get("by"):
            report.error(key, f"row {index + 1}: at and by are required")
        if row.get("kind") == "metric" and (not row.get("metric") or "value" not in row):
            report.error(key, f"row {index + 1}: metric rows need metric and value")
        if row.get("kind") in ("release_verified", "metric") and not row.get("evidence"):
            report.error(key, f"row {index + 1}: {row.get('kind')} rows need evidence")
        if row.get("kind") == "assessment":
            for name in ("epoch", "evidence", "proposal", "detail"):
                if not row.get(name):
                    report.error(key, f"row {index + 1}: assessment needs {name}")
            if row.get("status") not in ASSESSMENT_STATUSES:
                report.error(key, f"row {index + 1}: assessment status is invalid")
        seen.add(str(row.get("at")))
    for index, row in enumerate(log.rows):
        if row.get("supersedes") and str(row["supersedes"]) not in seen:
            report.error(key, f"row {index + 1}: supersedes '{row['supersedes']}' matches no earlier row")


def _idea(fm: dict[str, Any], key: str, report: Report) -> None:
    if not IDEA_ID.fullmatch(str(fm.get("id"))):
        report.error(key, "id must match idea-YYYYMMDD-<slug>")
    if not fm.get("title"):
        report.error(key, "title is required")
    status = _enum(fm, "status", IDEA_STATUSES, key, report, "proposed")
    _enum(fm, "size", IDEA_SIZES, key, report, "cycle")
    if not as_list(fm.get("evidence")):
        report.error(key, "an idea needs at least one evidence link")
    required = {"promoted": "promoted_to", "merged": "merged_into", "parked": "review_on", "rejected": "reason"}
    if status in required and not fm.get(required[status]):
        report.error(key, f"status '{status}' needs {required[status]}")


def _task(fm: dict[str, Any], key: str, report: Report) -> None:
    if not TASK_ID.fullmatch(str(fm.get("id"))):
        report.error(key, "id must match task-YYYYMMDD-<slug>")
    for name in ("title", "description"):
        if not str(fm.get(name) or "").strip():
            report.error(key, f"{name} is required")
    _enum(fm, "type", TASK_TYPES, key, report, "task")
    _enum(fm, "priority", TASK_PRIORITIES, key, report, "medium")
    status = _enum(fm, "status", TASK_STATUSES, key, report, "open")
    if fm.get("type") == "bug" and not str(fm.get("symptom") or "").strip():
        report.error(key, "a bug needs a symptom")
    card = fm.get("card_id")
    if card and not CARD_ID.fullmatch(str(card)):
        report.error(key, "card_id must match t_xxxxxxxx")
    if status == "in_progress" and not card:
        report.error(key, "in_progress needs card_id")
    if status == "cancelled" and not fm.get("reason"):
        report.error(key, "cancelled needs reason")
    if status == "done" and not (card or fm.get("reason")):
        report.error(key, "done needs card_id or reason")


def _bug(fm: dict[str, Any], key: str, report: Report) -> None:
    if not BUG_ID.fullmatch(str(fm.get("id"))):
        report.error(key, "id must match bug-YYYYMMDD-<slug>")
    if not fm.get("title"):
        report.error(key, "title is required")
    if not fm.get("symptom"):
        report.error(key, "a bug needs a symptom: what goes wrong")
    status = _enum(fm, "status", BUG_STATUSES, key, report, "open")
    _enum(fm, "severity", BUG_SEVERITIES, key, report, "medium")
    card = fm.get("fix_card")
    if card and not CARD_ID.fullmatch(str(card)):
        report.error(key, f"fix_card '{card}' must match t_xxxxxxxx")
    if status == "fixing" and not card:
        report.error(key, "status 'fixing' needs fix_card")
    if status == "wontfix" and not fm.get("reason"):
        report.error(key, "status 'wontfix' needs reason")
    if status == "fixed" and not (card or fm.get("reason")):
        report.error(key, "status 'fixed' needs fix_card or reason")


def _resolve_source(root: Path, vault_root: Path | None, value: str) -> bool:
    if "://" in value:
        return True
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.exists()
    bases = [vault_root] if vault_root else []
    bases += [root, root.parent, root.parent.parent]
    return any((base / value).exists() for base in bases if base)


def validate(snapshot: Snapshot, vault_root: Path | None = None, check_sources: bool = True) -> Report:
    report = Report()
    root = snapshot.root
    for path, message in snapshot.parse_errors.items():
        report.error(str(path.relative_to(root)) if path.is_relative_to(root) else str(path), message)
    for doc_id, first, second in snapshot.duplicate_ids:
        report.error(doc_id, f"duplicate id in {first.name} and {second.name}")

    all_ids = Counter(
        list(snapshot.initiatives) + list(snapshot.cycles) + list(snapshot.ideas) + list(snapshot.bugs) + list(snapshot.tasks)
    )
    for doc_id, count in all_ids.items():
        if count > 1:
            report.error(doc_id, "id is used by more than one initiative, cycle, idea, task, or bug")

    for initiative_id, doc in snapshot.initiatives.items():
        _initiative(doc.frontmatter, initiative_id, report)
        if doc.path.parent.name != initiative_id:
            report.error(initiative_id, f"folder '{doc.path.parent.name}' must equal the initiative id")
        focus = doc.frontmatter.get("focus_cycle")
        if focus and snapshot.cycle_initiative.get(str(focus)) != initiative_id:
            report.error(initiative_id, f"focus_cycle '{focus}' is not a cycle of this initiative")

    pre_shipping = Counter(
        snapshot.cycle_initiative.get(cycle_id, "")
        for cycle_id, doc in snapshot.cycles.items()
        if doc.frontmatter.get("stage") in ("exploring", "decided")
    )
    for initiative_id, count in pre_shipping.items():
        if initiative_id in snapshot.initiatives and count > MAX_PRE_SHIPPING_CYCLES_PER_INITIATIVE:
            report.warn(
                initiative_id,
                f"{count} cycles are exploring or decided; the limit is {MAX_PRE_SHIPPING_CYCLES_PER_INITIATIVE}. "
                "Finish, abandon, or defer one before opening another",
            )

    change_rows: dict[str, str] = {}
    for cycle_id, doc in snapshot.cycles.items():
        log = snapshot.observations.get(cycle_id)
        _cycle(doc.frontmatter, cycle_id, active_observations(log), report)
        owner = snapshot.cycle_initiative.get(cycle_id, "")
        if owner not in snapshot.initiatives:
            report.error(cycle_id, "the folder holding this cycle has no valid initiative.md")
        elif not cycle_id.startswith(owner + "-c"):
            report.error(cycle_id, f"cycle id must start with '{owner}-c'")
        disposition = doc.frontmatter.get("disposition") or {}
        if isinstance(disposition, dict):
            for ref in as_list(disposition.get("follow_ups")):
                if str(ref) not in snapshot.cycles and str(ref) not in snapshot.ideas:
                    report.error(cycle_id, f"follow-up '{ref}' is not a known cycle or idea")
            superseded = disposition.get("superseded_by")
            if superseded and str(superseded) not in snapshot.cycles:
                report.error(cycle_id, f"superseded_by '{superseded}' is not a known cycle")
        for row in as_list(doc.frontmatter.get("change_register")):
            if str(row) in change_rows:
                report.error(cycle_id, f"change register row {row} already belongs to {change_rows[str(row)]}")
            else:
                change_rows[str(row)] = cycle_id
        if check_sources:
            evidence = as_list(_mapping(doc.frontmatter.get("result")).get("evidence"))
            for source in as_list(doc.frontmatter.get("sources")) + evidence:
                if isinstance(source, str) and not _resolve_source(root, vault_root, source):
                    report.error(cycle_id, f"source '{source}' does not exist")

    for log_id, log in snapshot.observations.items():
        if log_id not in snapshot.cycles:
            report.error(f"observations/{log_id}.jsonl", "observation log has no matching cycle")
        _observation_rows(log, log_id, report)

    open_by_source: Counter = Counter()
    for idea_id, doc in snapshot.ideas.items():
        fm = doc.frontmatter
        _idea(fm, idea_id, report)
        related = fm.get("related_initiative")
        if related and str(related) not in snapshot.initiatives:
            report.error(idea_id, f"related_initiative '{related}' is not a known initiative")
        for name in ("promoted_to", "merged_into"):
            ref = fm.get(name)
            if (
                ref
                and str(ref) not in snapshot.cycles
                and str(ref) not in snapshot.initiatives
                and str(ref) not in snapshot.ideas
            ):
                if not CARD_ID.fullmatch(str(ref)):
                    report.error(idea_id, f"{name} '{ref}' is not a known initiative, cycle, idea, or card")
        if str(fm.get("status")) in OPEN_IDEA_STATUSES:
            open_by_source[str(fm.get("source") or "unknown")] += 1
        if check_sources:
            for source in as_list(fm.get("evidence")):
                if isinstance(source, str) and not _resolve_source(root, vault_root, source):
                    report.error(idea_id, f"evidence '{source}' does not exist")
    for source, count in open_by_source.items():
        if count > MAX_OPEN_IDEAS_PER_SOURCE:
            report.error(f"ideas:{source}", f"{count} open ideas from one source; the limit is {MAX_OPEN_IDEAS_PER_SOURCE}")

    for task_id, doc in snapshot.tasks.items():
        fm = doc.frontmatter
        _task(fm, task_id, report)
        related = fm.get("related_initiative")
        if related and str(related) not in snapshot.initiatives:
            report.error(task_id, f"related_initiative '{related}' is not a known initiative")
        if check_sources:
            for source in as_list(fm.get("evidence")):
                if isinstance(source, str) and not _resolve_source(root, vault_root, source):
                    report.warn(task_id, f"evidence '{source}' does not exist")

    for bug_id, doc in snapshot.bugs.items():
        fm = doc.frontmatter
        _bug(fm, bug_id, report)
        related = fm.get("related_initiative")
        if related and str(related) not in snapshot.initiatives:
            report.error(bug_id, f"related_initiative '{related}' is not a known initiative")
        if check_sources:
            for source in as_list(fm.get("evidence")):
                if isinstance(source, str) and not _resolve_source(root, vault_root, source):
                    report.warn(bug_id, f"evidence '{source}' does not exist")
    return report
