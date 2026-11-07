"""Daily review executor: parse answers, recheck the source, and apply typed actions.

Flow for one answer::

    submit()  -> store.answer() (first answer wins)
              -> option needs confirm?  awaiting_confirm, stop until confirm()
              -> execute()

    execute() -> recheck the source (collected items): gone -> resolved_elsewhere; basis changed -> stale, reopen
              -> apply the option's action, read the source back, log the action
              -> applied | failed | ready_for_owner (handoff) | deferred

Kanban writes go through the ``hermes kanban`` CLI (the supported interface), never the board database. Cycle and
idea writes change user-owned fields only, under the same file lock the agent write paths use, and are read back.
The user's free text is copied unchanged into the comment, card body, or decision row.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import CARD_ID, CLOSED_STAGES, STAGES, USER_OWNED_CYCLE_FIELDS, VERDICTS, is_agent_rule
from .review_collect import COLLECTED_SOURCES, collect_project
from .reviews import ReviewError, ReviewStore, basis_hash
from .settings import ProjectSettings, load_settings
from .store import load
from .writers import WriteRefused, edit_frontmatter, preview_cycle_errors

# Actions the executor does not run yet. An answer that picks one is refused before it is recorded.
UNSUPPORTED = {
    "pr_merge": "Merging from the review is not available yet. Merge in GitHub, or pick another option.",
    "pr_merge_waive_review": "Merging from the review is not available yet. Merge in GitHub, or pick another option.",
}
IDEA_TARGETS = ("promoted", "parked", "merged", "reviewing")
CYCLE_FIELD_ALIASES = {
    "verdict": ("result", "verdict"),
    "disposition": ("disposition", "action"),
    "success_criteria": ("measurement", "success_criteria"),
    "primary_metric": ("measurement", "primary_metric"),
    "source": ("measurement", "source"),
    "baseline": ("measurement", "baseline"),
    "observation_window": ("measurement", "observation_window"),
}

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


# --- parsing -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    kind: str  # answer | confirm | defer | auto | manual
    short_id: str
    option: str = ""
    note: str = ""
    defer_kind: str = ""
    until: str | None = None


_ANSWER = re.compile(r"^(D\d+)\s+([A-D])(?:\s*[:\-–]\s*(.*))?$", re.IGNORECASE | re.DOTALL)
_CONFIRM = re.compile(r"^confirm\s+(D\d+)$", re.IGNORECASE)
_DEFER_DATE = re.compile(r"^defer\s+(D\d+)\s+until\s+(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
_DEFER_CHANGE = re.compile(r"^defer\s+(D\d+)\s+until\s+change$", re.IGNORECASE)
_DEFER_REASON = re.compile(r"^defer\s+(D\d+)\s*:\s*(.+)$", re.IGNORECASE)
_AUTO = re.compile(r"^auto\s+(D\d+)(?:\s*:\s*(.*))?$", re.IGNORECASE | re.DOTALL)
_MANUAL = re.compile(r"^manual\s+(D\d+)$", re.IGNORECASE)


def parse_line(line: str) -> Command | None:
    line = line.strip()
    if match := _ANSWER.fullmatch(line):
        return Command("answer", match.group(1).upper(), option=match.group(2).upper(), note=(match.group(3) or "").strip())
    if match := _CONFIRM.fullmatch(line):
        return Command("confirm", match.group(1).upper())
    if match := _DEFER_DATE.fullmatch(line):
        return Command("defer", match.group(1).upper(), defer_kind="date", until=match.group(2))
    if match := _DEFER_CHANGE.fullmatch(line):
        return Command("defer", match.group(1).upper(), defer_kind="state_change")
    if match := _DEFER_REASON.fullmatch(line):
        return Command("defer", match.group(1).upper(), defer_kind="indefinite", note=match.group(2).strip())
    if match := _AUTO.fullmatch(line):
        return Command("auto", match.group(1).upper(), note=(match.group(2) or "").strip())
    if match := _MANUAL.fullmatch(line):
        return Command("manual", match.group(1).upper())
    return None


def parse_message(text: str) -> list[Command] | None:
    """The commands in *text*, or None when any non-empty line is not a review command.

    Strict on purpose: the review topic also carries ordinary chat for the agent, so a message is taken only when
    every line is a command. A note continues on the following lines until the next command line.
    """
    commands: list[Command] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        command = parse_line(raw)
        if command is not None:
            commands.append(command)
        elif commands and commands[-1].kind in ("answer", "auto") and commands[-1].note:
            last = commands[-1]
            commands[-1] = Command(last.kind, last.short_id, option=last.option, note=f"{last.note}\n{raw.rstrip()}")
        else:
            return None
    return commands or None


# --- helpers -----------------------------------------------------------------------------------------------


def hermes_bin() -> str:
    """The ``hermes`` executable: ``HERMES_BIN`` when set (tests, non-PATH installs), else the one on PATH."""
    pinned = os.environ.get("HERMES_BIN", "").strip()
    if pinned:
        return pinned
    found = shutil.which("hermes")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "hermes"
    return str(fallback)


def default_runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN")}
    return subprocess.run(  # noqa: S603 - fixed binary, argument list
        [hermes_bin(), *args], input=stdin, capture_output=True, text=True, timeout=120, env=env, check=False
    )


class ActionFailed(RuntimeError):
    """The action did not take effect; the message says why."""


def _today() -> str:
    return dt.date.today().isoformat()


def _project(item: dict[str, Any]) -> ProjectSettings:
    config, error = load_settings()
    if error:
        raise ActionFailed(f"settings file is invalid: {error}")
    project = config.project(item.get("project") or "") if item.get("project") else None
    if project is None and len(config.projects) == 1:
        project = config.projects[0]
    if project is None:
        raise ActionFailed(f"{item['short_id']}: no project '{item.get('project')}' in settings")
    return project


def _decision_text(item: dict[str, Any], option: dict[str, Any]) -> str:
    return str((option.get("args") or {}).get("decision") or option["label"]).strip()


def _learned(note: str) -> re.Match | None:
    """An agent answer under a learned rule starts with ``rule learned_x (from Dnn):`` (review_auto.settle_learned)."""
    return re.match(r"rule (learned_[a-z0-9_]+) \(from (D\d+)\)", note or "")


def _swept(note: str) -> bool:
    """An agent answer from the stale sweep starts with ``rule stale_sweep (`` (review_sweep.handle)."""
    return (note or "").startswith("rule stale_sweep (")


def _by_agent(item: dict[str, Any]) -> bool:
    """The answer being applied was given by an agent under the user's auto mark (surface ``agent``)."""
    return item.get("answered_by") == "agent"


def _review_header(item: dict[str, Any], option: dict[str, Any], note: str) -> str:
    who = "the user chose"
    if _by_agent(item):
        learned = _learned(note)
        if learned:
            who = f"an agent chose under rule {learned.group(1)}"
        elif _swept(note):
            who = "an agent chose the recommended option under rule stale_sweep (idle for 3 days)"
        else:
            who = "an agent chose (the user marked this item auto)"
    lines = [f"Daily review {item['short_id']}: {who} {option['id']}: {option['label']}"]
    question = (item.get("version") or {}).get("question")
    if question:
        lines.append(f"Question: {question}")
    if note:
        lines += ["", "Agent's reason and the user's auto note:" if _by_agent(item) else "Note from the user (unchanged):", note]
    return "\n".join(lines)


def _attend_comment(item: dict[str, Any], option: dict[str, Any], note: str, you: str, after: str) -> str:
    """The user's answer plus a handoff block, so the card shows on his Waiting-on-you list until it moves."""
    lines = [
        "**ATTEND — NEEDS YOU** — " + f"{item['short_id']}: you chose {option['id']} and do the next step yourself.",
        "",
        f"**You:** {you}",
        f"**Next:** {after or 'agents resume this card after you record the step as done.'}",
        "",
        "---",
        _review_header(item, option, note),
    ]
    return "\n".join(lines)


# --- Kanban actions ----------------------------------------------------------------------------------------


class Kanban:
    def __init__(self, run: Runner) -> None:
        self.run = run

    def _call(self, board: str, *args: str, stdin: str | None = None) -> str:
        proc = self.run(["kanban", "--board", board, *args], stdin)
        if proc.returncode != 0:
            raise ActionFailed((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:500])
        return proc.stdout

    def show(self, board: str, card: str) -> dict[str, Any]:
        return json.loads(self._call(board, "show", "--json", card))

    def comment(self, board: str, card: str, text: str) -> None:
        self._call(board, "comment", "--author", "user", card, text)
        shown = self.show(board, card)
        if not any(c.get("body") == text for c in shown.get("comments") or []):
            raise ActionFailed(f"comment on {card} not found on read-back")

    def unblock(self, board: str, card: str) -> str:
        self._call(board, "unblock", card)
        status = (self.show(board, card).get("task") or {}).get("status")
        if status in ("blocked", "scheduled", "triage"):
            raise ActionFailed(f"{card} is still {status} after unblock")
        return str(status)

    def create(
        self, board: str, *, title: str, assignee: str, body: str, key: str, skills: tuple[str, ...] | list[str] = ()
    ) -> str:
        extra = [arg for skill in skills for arg in ("--skill", skill)]
        out = self._call(
            board,
            "create",
            title,
            "--assignee",
            assignee,
            *extra,
            "--body-file",
            "-",
            "--idempotency-key",
            key,
            "--json",
            stdin=body,
        )
        start = out.find("{")
        card = (json.loads(out[start:]) if start >= 0 else {}).get("id")
        if not card or not CARD_ID.fullmatch(str(card)):
            raise ActionFailed(f"create printed no card id: {out[:200]}")
        self.show(board, card)
        return str(card)

    def archive(self, board: str, card: str) -> None:
        self._call(board, "archive", card)
        status = (self.show(board, card).get("task") or {}).get("status")
        if status != "archived":
            raise ActionFailed(f"{card} is {status} after archive")


# --- file actions ------------------------------------------------------------------------------------------


def _decision_rows(item: dict[str, Any], option: dict[str, Any], note: str, decision: str) -> list[dict[str, Any]]:
    """The rows an applied answer appends. An agent answer under an auto mark writes two: the user's delegation
    (his decision, with his note) and the agent's choice (``by: agent``, rule ``auto_delegated``)."""
    if not _by_agent(item):
        row: dict[str, Any] = {"date": _today(), "by": "user", "decision": decision, "review": item["short_id"]}
        if note:
            row["note"] = note
        return [row]
    mark = item.get("auto") or {}
    if _swept(note):
        # The user's standing delegation (2026-11-01) covers it; no auto mark exists. Only the agent row.
        row = {"date": _today(), "by": "agent", "rule": "stale_sweep", "review": item["short_id"], "decision": decision}
        return [{**row, "reason": note}]
    learned = _learned(note)
    if learned:
        # A learned rule covered this item; the user did not mark it. Only the agent row, citing the rule.
        return [
            {
                "date": _today(),
                "by": "agent",
                "rule": learned.group(1),
                "review": item["short_id"],
                "learned_from": learned.group(2),
                "decision": decision,
                "reason": note,
            }
        ]
    delegation: dict[str, Any] = {
        "date": str(mark.get("at") or "")[:10] or _today(),
        "by": "user",
        "decision": f"Delegated review {item['short_id']} to the agents (marked auto)",
        "review": item["short_id"],
    }
    if mark.get("note"):
        delegation["note"] = mark["note"]
    agent: dict[str, Any] = {
        "date": _today(),
        "by": "agent",
        "rule": "auto_delegated",
        "review": item["short_id"],
        "decision": decision,
        "reason": note,
    }
    return [delegation, agent]


def _cycle_path(project: ProjectSettings, cycle_id: str) -> Path:
    doc = load(project.root).cycles.get(cycle_id)
    if doc is None:
        raise ActionFailed(f"unknown cycle {cycle_id}")
    return doc.path


def _decision_target(project: ProjectSettings, item: dict[str, Any], args: dict[str, Any]) -> Path:
    snapshot = load(project.root)
    cycle = args.get("cycle") or item.get("cycle")
    if cycle:
        if cycle not in snapshot.cycles:
            raise ActionFailed(f"unknown cycle {cycle}")
        return snapshot.cycles[cycle].path
    initiative = args.get("initiative") or item.get("initiative")
    if initiative and initiative in snapshot.initiatives:
        return snapshot.initiatives[initiative].path
    raise ActionFailed("record_decision needs a cycle or an initiative")


def record_decision(project: ProjectSettings, item: dict[str, Any], option: dict[str, Any], note: str) -> str:
    args = option.get("args") or {}
    path = _decision_target(project, item, args)
    rows = _decision_rows(item, option, note, _decision_text(item, option))

    def change(fm: dict[str, Any]) -> None:
        fm["decisions"] = [*(fm.get("decisions") or []), *rows]

    after = edit_frontmatter(path, project.root, change)
    if (after.get("decisions") or [])[-len(rows) :] != rows:
        raise ActionFailed(f"decision not found in {path.name} on read-back")
    return f"decision recorded in {path.name}"


def _validate_cycle_fields(fields: Any) -> None:
    if not isinstance(fields, dict) or not fields:
        raise ActionFailed("set_cycle_fields needs a cycle and fields")
    for name, value in fields.items():
        if name not in USER_OWNED_CYCLE_FIELDS and name not in CYCLE_FIELD_ALIASES:
            raise ActionFailed(f"'{name}' is not a user-owned cycle field")
        if name == "stage" and value not in STAGES:
            raise ActionFailed(f"unknown stage '{value}'")
        if name == "verdict" and value not in VERDICTS:
            raise ActionFailed(f"unknown verdict '{value}'")


def _apply_cycle_fields(fm: dict[str, Any], fields: dict[str, Any]) -> None:
    for name, value in fields.items():
        if name == "disposition" and isinstance(value, dict):
            fm[name] = {**(fm.get(name) or {}), **value}
        elif name in CYCLE_FIELD_ALIASES:
            group, key = CYCLE_FIELD_ALIASES[name]
            fm[group] = {**(fm.get(group) or {}), key: value}
        else:
            fm[name] = value


def cycle_fields_problems(project: ProjectSettings, cycle: str, fields: Any) -> list[str]:
    """Why a set_cycle_fields option would fail when answered (bad fields, or a cycle left invalid); [] if none.

    The option writer calls this, so the user is never offered a choice that cannot be applied."""
    try:
        _validate_cycle_fields(fields)
        path = _cycle_path(project, cycle)
    except ActionFailed as exc:
        return [str(exc)]

    def proposed(fm: dict[str, Any]) -> None:
        _apply_cycle_fields(fm, fields)
        # Applying the option records a decision in the same write.
        fm["decisions"] = [*(fm.get("decisions") or []), {"date": _today(), "by": "user", "decision": "Preview"}]

    return preview_cycle_errors(path, proposed)


def set_cycle_fields(project: ProjectSettings, item: dict[str, Any], option: dict[str, Any], note: str) -> str:
    args = option.get("args") or {}
    cycle = args.get("cycle") or item.get("cycle")
    fields = args.get("fields") or {}
    if not cycle:
        raise ActionFailed("set_cycle_fields needs a cycle and fields")
    _validate_cycle_fields(fields)
    path = _cycle_path(project, cycle)
    rows = _decision_rows(item, option, note, _decision_text(item, option))

    def change(fm: dict[str, Any]) -> None:
        _apply_cycle_fields(fm, fields)
        fm["decisions"] = [*(fm.get("decisions") or []), *rows]

    try:
        after = edit_frontmatter(path, project.root, change)
    except WriteRefused as exc:
        raise ActionFailed(str(exc)) from exc
    for name, value in fields.items():
        got = (
            {key: (after.get(name) or {}).get(key) for key in value}
            if name == "disposition" and isinstance(value, dict)
            else (after.get(CYCLE_FIELD_ALIASES[name][0]) or {}).get(CYCLE_FIELD_ALIASES[name][1])
            if name in CYCLE_FIELD_ALIASES
            else after.get(name)
        )
        if got != value:
            raise ActionFailed(f"{cycle}.{name} is {got!r} on read-back, expected {value!r}")
    return f"{cycle}: " + ", ".join(f"{k}={v}" for k, v in fields.items())


PARK_DAYS = 30  # a parked idea with no review date comes back for review after this many days


def _idea_id(item: dict[str, Any], option: dict[str, Any]) -> str:
    return str((option.get("args") or {}).get("idea") or (item.get("basis") or {}).get("idea") or "")


def set_idea(
    project: ProjectSettings,
    item: dict[str, Any],
    option: dict[str, Any],
    note: str,
    status: str,
    *,
    promoted_to: str | None = None,
) -> str:
    """Set an idea's status together with the field that status needs, so the file always validates.

    promoted -> promoted_to (the caller resolves it: an existing cycle or initiative, or a card it created),
    merged -> merged_into (an existing initiative, cycle, or idea), parked -> review_on (args or today + PARK_DAYS)."""
    args = option.get("args") or {}
    idea = _idea_id(item, option)
    snapshot = load(project.root)
    doc = snapshot.ideas.get(idea)
    if doc is None:
        raise ActionFailed(f"unknown idea {idea}")
    fields: dict[str, Any] = {"status": status, "reason": note or option["label"]}
    if status == "promoted":
        if not promoted_to:
            raise ActionFailed(f"promoting {idea} needs a destination: an existing cycle or initiative, or a card")
        fields["promoted_to"] = promoted_to
    elif status == "merged":
        into = str(args.get("into") or "")
        if not into:
            raise ActionFailed("merging an idea needs args.into")
        known = into in snapshot.cycles or into in snapshot.initiatives or into in snapshot.ideas
        if not known or into == idea:
            raise ActionFailed(f"cannot merge {idea} into '{into}': not a known initiative, cycle, or idea")
        fields["merged_into"] = into
    elif status == "parked":
        fields["review_on"] = str(args.get("review_on") or (dt.date.today() + dt.timedelta(days=PARK_DAYS)).isoformat())

    def change(fm: dict[str, Any]) -> None:
        fm.update(fields)

    after = edit_frontmatter(doc.path, project.root, change)
    wrong = {k: after.get(k) for k, v in fields.items() if after.get(k) != v}
    if wrong:
        raise ActionFailed(f"{idea} read back with {wrong}")
    detail = {"promoted": "promoted_to", "merged": "merged_into", "parked": "review_on"}.get(status)
    return f"{idea} -> {status}" + (f" ({detail}: {fields[detail]})" if detail else "")


def _delivery_card_body(doc, item: dict[str, Any], option: dict[str, Any], note: str) -> str:
    fm = doc.frontmatter
    evidence = "\n".join(f"- {e}" for e in (fm.get("evidence") or [])) or "-"
    parts = [
        f"Promoted idea `{fm.get('id')}` ({fm.get('size') or 'cycle'}): {doc.path}",
        f"Problem: {fm.get('problem') or '-'}\nExpected outcome: {fm.get('expected_outcome') or '-'}",
        f"Evidence:\n{evidence}",
    ]
    if fm.get("related_initiative"):
        parts.append(f"Initiative: {fm['related_initiative']}")
    parts += [
        _review_header(item, option, note),
        "Route this through the normal lifecycle: plan it (or reuse an approved plan named above), then deliver.",
    ]
    return "\n\n".join(parts)


# --- executor ----------------------------------------------------------------------------------------------


class Executor:
    def __init__(self, store: ReviewStore, run: Runner | None = None) -> None:
        self.store = store
        self.kanban = Kanban(run or default_runner)

    # The public entry points return one result dict each: {short_id, state, message}.

    def submit(self, short_id: str, option: str, *, note: str = "", surface: str, telegram_msg_id: str | None = None) -> dict:
        item = self.store.get(short_id)
        if item is None:
            return _result(short_id, "refused", f"{short_id.upper()} is not a review item")
        chosen = next((o for o in (item.get("version") or {}).get("options", []) if o["id"] == str(option).upper()), None)
        if chosen and chosen["action"] in UNSUPPORTED:
            return _result(item["short_id"], "refused", UNSUPPORTED[chosen["action"]])
        try:
            recorded = self.store.answer(short_id, option, note=note, surface=surface, telegram_msg_id=telegram_msg_id)
        except ReviewError as exc:
            return _result(item["short_id"], "refused", str(exc))
        if not recorded["accepted"]:
            return _result(item["short_id"], "refused", f"not recorded: {recorded['reason']}")
        if recorded["option"]["confirm"]:
            self.store.set_status(item["short_id"], "awaiting_confirm", expect=("answered",))
            return _result(
                item["short_id"],
                "awaiting_confirm",
                f"{recorded['option']['label']} cannot be undone. Reply `confirm {item['short_id']}` to apply it.",
            )
        return self.execute(item["short_id"])

    def confirm(self, short_id: str) -> dict:
        item = self.store.get(short_id)
        if item is None:
            return _result(short_id, "refused", f"{short_id.upper()} is not a review item")
        if item["status"] != "awaiting_confirm":
            return _result(item["short_id"], "refused", f"{item['short_id']} is {item['status']}; nothing to confirm")
        return self.execute(item["short_id"], expect=("awaiting_confirm",))

    def defer(self, command: Command) -> dict:
        try:
            item = self.store.defer(command.short_id, kind=command.defer_kind, until=command.until, reason=command.note)
        except ReviewError as exc:
            return _result(command.short_id, "refused", str(exc))
        return _result(item["short_id"], "deferred", _defer_text(command.defer_kind, command.until, command.note))

    def run_commands(self, commands: list[Command], *, surface: str, telegram_msg_id: str | None = None) -> list[dict]:
        results = []
        for command in commands:
            if command.kind == "answer":
                results.append(
                    self.submit(
                        command.short_id, command.option, note=command.note, surface=surface, telegram_msg_id=telegram_msg_id
                    )
                )
            elif command.kind == "confirm":
                results.append(self.confirm(command.short_id))
            elif command.kind == "auto":
                results.append(self.mark_auto(command.short_id, command.note, surface=surface))
            elif command.kind == "manual":
                results.append(self.unmark_auto(command.short_id, surface=surface))
            else:
                results.append(self.defer(command))
        return results

    def mark_auto(self, short_id: str, note: str = "", *, surface: str) -> dict:
        """The user hands this item to the agents; the next recovery tick handles it (review_tick.md "Auto items")."""
        suspended = self.store.rule_states().get("auto_delegated")
        if suspended and suspended["state"] == "suspended":
            return _result(short_id, "refused", f"auto handling is suspended since {suspended['at'][:10]}: {suspended['reason']}")
        try:
            item = self.store.mark_auto(short_id, note, surface=surface)
        except ReviewError as exc:
            return _result(short_id, "refused", str(exc))
        return _result(
            item["short_id"],
            "auto",
            "marked auto: the next recovery tick handles it; `manual " + item["short_id"] + "` takes it back",
        )

    def unmark_auto(self, short_id: str, *, surface: str) -> dict:
        try:
            item = self.store.unmark_auto(short_id, surface=surface)
        except ReviewError as exc:
            return _result(short_id, "refused", str(exc))
        return _result(item["short_id"], "open", "back with you; the agents leave it alone")

    def execute(self, short_id: str, *, expect: tuple[str, ...] = ("answered",)) -> dict:
        if not self.store.set_status(short_id, "applying", expect=expect):
            current = self.store.get(short_id) or {}
            return _result(short_id, "refused", f"{short_id} is {current.get('status')}; not applying")
        item = self.store.get(short_id) or {}
        answer, option = self.store.latest_answer(short_id)
        note = answer["note"]
        if answer["surface"] == "agent":
            item["answered_by"] = "agent"
            item["auto"] = self.store.last_auto_mark(short_id)
        stale = self._recheck(item, answer)
        if stale:
            return stale
        self.store.log_action(answer["id"], short_id, option["action"], option.get("args") or {}, "applying")
        try:
            outcome, receipt = self._apply(item, option, note, answer)
        except (ActionFailed, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.store.log_action(answer["id"], short_id, option["action"], option.get("args") or {}, "failed", error=str(exc))
            self.store.set_status(short_id, "failed", expect=("applying",), note=str(exc)[:500])
            return _result(short_id, "failed", str(exc))
        self.store.log_action(answer["id"], short_id, option["action"], option.get("args") or {}, outcome, receipt=receipt)
        if outcome == "applied" and item["source"] in COLLECTED_SOURCES:
            # The same gate with the same basis must not come back as a new item at the next collect.
            self.store.dismiss(item["key"], evidence_hash=item["basis_hash"])
        return _result(short_id, outcome, receipt)

    def _promote(
        self,
        project: ProjectSettings,
        item: dict[str, Any],
        option: dict[str, Any],
        note: str,
        answer: dict[str, Any],
        assignee: str | None = None,
    ) -> str:
        """Promote an idea to a destination: ``args.into`` (an existing cycle or initiative) or a new delivery card.

        The card is created first (idempotent per answer), then the idea gets ``status: promoted`` and
        ``promoted_to`` in one write. A failure before that write leaves the idea file as it was."""
        args = option.get("args") or {}
        idea = _idea_id(item, option)
        snapshot = load(project.root)
        doc = snapshot.ideas.get(idea)
        if doc is None:
            raise ActionFailed(f"unknown idea {idea}")
        into = str(args.get("into") or "")
        if into:
            if into not in snapshot.cycles and into not in snapshot.initiatives:
                raise ActionFailed(f"cannot promote {idea} into '{into}': not a known cycle or initiative")
            stage = str(snapshot.cycles[into].frontmatter.get("stage")) if into in snapshot.cycles else ""
            if stage in CLOSED_STAGES:
                raise ActionFailed(f"cannot promote {idea} into {into}: that cycle is {stage}")
            return set_idea(project, item, option, note, "promoted", promoted_to=into)
        board = str(args.get("board") or project.kanban_board or "")
        owner = str(args.get("assignee") or assignee or project.intake_assignee or "")
        if not board or not owner:
            raise ActionFailed(
                f"promoting {idea} needs args.into, or a board and an assignee for its delivery card "
                "(set intake_assignee in the project settings)"
            )
        card = self.kanban.create(
            board,
            title=str(doc.frontmatter.get("title") or idea)[:200],
            assignee=owner,
            body=_delivery_card_body(doc, item, option, note),
            key=f"review:{item['short_id']}:{answer['id']}",
        )
        receipt = set_idea(project, item, option, note, "promoted", promoted_to=card)
        return f"{receipt}; created {card} for {owner}"

    def complete_promotions(
        self, project: ProjectSettings, *, assignee: str | None = None, dry_run: bool = False
    ) -> list[dict[str, Any]]:
        """Finish promotions an earlier executor applied without a destination (status promoted, no promoted_to).

        Uses the user's original answer and note; asks nothing again. Idempotent."""
        snapshot = load(project.root)
        done = []
        for short_id in self.store.applied_idea_promotions():
            item = self.store.get(short_id) or {}
            if item.get("project") not in (None, project.id):
                continue
            answer, option = self.store.latest_answer(short_id)
            args = option.get("args") or {}
            if option["action"] != "idea_status" or args.get("status") != "promoted":
                continue
            idea = _idea_id(item, option)
            doc = snapshot.ideas.get(idea)
            if doc is None or doc.frontmatter.get("status") != "promoted" or doc.frontmatter.get("promoted_to"):
                continue
            entry = {"short_id": short_id, "idea": idea, "answered_at": answer["answered_at"], "option": option["label"]}
            if not dry_run:
                self.store.log_action(answer["id"], short_id, "idea_status", args, "applying", receipt="complete promotion")
                try:
                    receipt = self._promote(project, item, option, answer["note"], answer, assignee=assignee)
                except (ActionFailed, OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.store.log_action(answer["id"], short_id, "idea_status", args, "failed", error=str(exc))
                    raise
                self.store.log_action(answer["id"], short_id, "idea_status", args, "applied", receipt=receipt)
                entry["promoted_to"] = load(project.root).ideas[idea].frontmatter.get("promoted_to")
                entry["receipt"] = receipt
            done.append(entry)
        return done

    def _recheck(self, item: dict[str, Any], answer: dict[str, Any]) -> dict | None:
        """Re-read the source of a collected item. Returns a result when the answer must not be applied."""
        if item["source"] not in COLLECTED_SOURCES:
            return None
        try:
            project = _project(item)
            # A Kanban item re-reads only its own card: did it finish, or did its state change since the options?
            # Whether it still needs a human is the recovery tick's call; it closes the item on its next pass.
            basis = item.get("basis") or {}
            gates = (
                {str(basis.get("board")): {str(basis.get("card")): {"id": basis.get("card")}}}
                if item["source"] == "kanban" and basis.get("board") and basis.get("card")
                else None
            )
            collected = collect_project(project, gates=gates)
        except (ActionFailed, OSError) as exc:
            self.store.set_status(item["short_id"], "failed", expect=("applying",), note=f"recheck failed: {exc}")
            return _result(item["short_id"], "failed", f"could not re-read the source: {exc}")
        if item["source"] not in collected["ok"]:
            reason = "; ".join(collected["errors"]) or "source unreadable"
            self.store.set_status(item["short_id"], "failed", expect=("applying",), note=f"recheck failed: {reason}")
            return _result(item["short_id"], "failed", f"could not re-read the source: {reason}")
        current = next((c for c in collected["candidates"] if c["key"] == item["key"]), None)
        shown = self.store.versions(item["short_id"])[answer["version"] - 1]["basis_hash"]
        if current is None:
            self.store.log_action(
                answer["id"], item["short_id"], "recheck", {}, "resolved_elsewhere", receipt="the gate closed in its source"
            )
            self.store.close_item(
                item["short_id"], "resolved_elsewhere", note="Closed in its source before the answer was applied."
            )
            return _result(item["short_id"], "resolved_elsewhere", "already resolved in its source; nothing applied")
        if basis_hash(current["basis"]) != shown:
            self.store.log_action(
                answer["id"],
                item["short_id"],
                "recheck",
                {},
                "stale",
                receipt="the source changed after the options were written",
            )
            self.store.refresh_basis(item["short_id"], current["basis"])
            self.store.set_status(
                item["short_id"], "open", expect=("applying",), note="Source changed; new options at the next review."
            )
            return _result(
                item["short_id"],
                "stale",
                "the source changed since these options were written; nothing applied, it returns with new options",
            )
        return None

    def _apply(self, item: dict[str, Any], option: dict[str, Any], note: str, answer: dict[str, Any]) -> tuple[str, str]:
        action, args = option["action"], option.get("args") or {}
        sid = item["short_id"]
        if action == "acknowledge":
            self.store.set_status(sid, "applied", expect=("applying",))
            return "applied", "acknowledged"
        if action == "defer":
            kind = args.get("kind") or "state_change"
            reason = note or option["label"]
            self.store.defer(sid, kind=kind, until=args.get("until"), reason=reason, expect=("applying",))
            return "deferred", _defer_text(kind, args.get("until"), reason)
        if action == "attend":
            board, card = _card(item, args)
            you = str(args.get("you") or "").strip()
            if not you:
                raise ActionFailed("attend needs args.you: the step the user does himself")
            self.kanban.comment(board, card, _attend_comment(item, option, note, you, str(args.get("next") or "").strip()))
            reason = f"attend: {you}"
            self.store.defer(sid, kind="state_change", until=None, reason=reason, expect=("applying",))
            return "deferred", f"posted the answer to {card}; it waits on you: {you}"
        if action == "handoff":
            self.store.set_status(sid, "ready_for_owner", expect=("applying",))
            owner = (
                item.get("owner_job") or item.get("owner_profile") or args.get("job") or args.get("profile") or "the asking job"
            )
            return "ready_for_owner", f"handed to {owner}; it applies the answer and acknowledges"
        if action in ("kanban_comment", "kanban_unblock", "kanban_archive"):
            board, card = _card(item, args)
            header = _review_header(item, option, note)
            if action == "kanban_archive":
                self.kanban.comment(board, card, header)
                self.kanban.archive(board, card)
                receipt = f"{card} archived"
            else:
                self.kanban.comment(board, card, header)
                receipt = f"commented on {card}"
                if action == "kanban_unblock":
                    receipt = f"{card} unblocked, now {self.kanban.unblock(board, card)}"
        elif action == "kanban_create":
            board = str(args.get("board") or _project(item).kanban_board or "")
            title, assignee = str(args.get("title") or "").strip(), str(args.get("assignee") or "").strip()
            if not board or not title or not assignee:
                raise ActionFailed("kanban_create needs board, title, and assignee")
            body = "\n\n".join(
                part
                for part in (
                    str(args.get("body") or "").strip(),
                    _review_header(item, option, note),
                    f"Context from the review:\n{(item.get('version') or {}).get('context') or item.get('context') or '-'}",
                )
                if part
            )
            card = self.kanban.create(board, title=title, assignee=assignee, body=body, key=f"review:{sid}:{answer['id']}")
            receipt = f"created {card} for {assignee}"
        elif action == "record_decision":
            receipt = record_decision(_project(item), item, option, note)
        elif action == "set_cycle_fields":
            receipt = set_cycle_fields(_project(item), item, option, note)
        elif action == "idea_status":
            status = str(args.get("status") or "")
            if status not in IDEA_TARGETS:
                raise ActionFailed(f"idea_status cannot set '{status}'")
            if status == "promoted":
                receipt = self._promote(_project(item), item, option, note, answer)
            else:
                receipt = set_idea(_project(item), item, option, note, status)
        elif action == "idea_reject":
            receipt = set_idea(_project(item), item, option, note, "rejected")
        elif action == "rule_state":
            rule, state = str(args.get("rule") or ""), str(args.get("state") or "")
            if item["source"] != "learn":
                raise ActionFailed("rule_state applies only to rule-learning items (source learn)")
            if not is_agent_rule(rule):
                raise ActionFailed(f"unknown delegated rule '{rule}'")
            why = note or f"{item['short_id']}: {option['label']}"
            changed = self.store.set_rule_state(rule, state, why, short_id=sid)
            receipt = f"rule {rule} is {state}" + ("" if changed else " (unchanged)")
        else:
            raise ActionFailed(UNSUPPORTED.get(action, f"no executor for '{action}'"))
        self.store.set_status(sid, "applied", expect=("applying",))
        return "applied", receipt


def _card(item: dict[str, Any], args: dict[str, Any]) -> tuple[str, str]:
    basis = item.get("basis") or {}
    board = str(args.get("board") or basis.get("board") or "")
    card = str(args.get("card") or basis.get("card") or "")
    if not board or not CARD_ID.fullmatch(card):
        raise ActionFailed("a Kanban action needs a board and a card id")
    return board, card


def _defer_text(kind: str, until: str | None, reason: str) -> str:
    if kind == "date":
        return f"deferred until {until}"
    if kind == "state_change":
        return "deferred until its source changes"
    return f"deferred: {reason}" if reason else "deferred"


def _result(short_id: str, state: str, message: str) -> dict[str, str]:
    return {"short_id": str(short_id).upper(), "state": state, "message": message}


def format_results(results: list[dict[str, str]]) -> str:
    marks = {"applied": "✅", "deferred": "⏸", "ready_for_owner": "↪️", "awaiting_confirm": "⚠️", "auto": "🤖", "open": "↩️"}
    return "\n".join(f"{marks.get(r['state'], '❌')} {r['short_id']}: {r['message']}" for r in results)
