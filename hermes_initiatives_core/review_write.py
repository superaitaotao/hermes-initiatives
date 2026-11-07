"""Validate the generator's output and store it. Nothing is written when any check fails.

Input (JSON)::

    {
      "review_id": "R20261012-003000-ab12",
      "items": {
        "D3": {"question": "...", "options": [{"id": "A", "label": "...", "action": "kanban_unblock", "args": {}}, ...],
               "recommend": "A", "why": "2-4 lines of context",
               "summary": {"what": "what is blocked and what waits on it", "why": "the actual cause, in plain words"}},
        "D1": {"watch": "Machine block; the card says nothing is needed from you."}
      },
      "summaries": {"in-lead-conversion": "2-3 lines"},
      "updates": {"in-lead-conversion": "one paragraph"},
      "suggestions": [{"key": "suggest:...", "title": "...", "question": "...", "options": [...], "recommend": "A",
                       "evidence": ["path or url"], "project": "p", "initiative": "in-x"}]
    }

Every open item that needs options (see ``ReviewStore.needs_options``) must have an entry: options, or a watch with a
reason. Unknown ids, unknown actions, more than 3 suggestions, and suggestions without evidence are rejected.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .reviews import ReviewError, ReviewStore, basis_hash, validate_options, validate_summary

MAX_SUGGESTIONS = 3
# Why an item needs no human now. A watch reason starts with one of these, then names its evidence (review_tick.md).
WATCH_GROUNDS = ("machine:", "agent_owned:", "already_decided:", "duplicate_of ", "waiting_on:")

# What each action does when the executor (review_exec) applies it, and the args it reads. Shown to the generator.
ACTIONS_HELP: dict[str, str] = {
    "record_decision": "Append a user decision to a cycle or initiative file. args: {path?, cycle?, decision}",
    "set_cycle_fields": "Set user-owned cycle fields (stage, verdict, disposition, success_criteria, next_action). "
    "args: {cycle, fields}. A criteria item is answered with fields.success_criteria, one cycle per option. "
    "Moving to measuring needs a recorded verified release plus metric/source/baseline/window in the same option if "
    "missing (aliases: primary_metric, source, baseline, observation_window {starts, length_days}). A criteria-based "
    "spot-check can leave the window null. Invalid options and writes are refused.",
    "idea_status": "Promote, park, or merge an idea. args: {idea, status, into?, assignee?, review_on?}. "
    "promoted: into = an existing cycle or initiative; without into, a delivery card is created for assignee "
    "(default: the project's intake lane) and recorded as promoted_to. merged: into is required and must exist. "
    "parked: review_on defaults to 30 days from now.",
    "idea_reject": "Reject an idea (needs confirm). args: {idea}",
    "kanban_comment": "Comment on a card with the user's note unchanged. args: {board, card}",
    "kanban_unblock": "Comment with the user's answer, then unblock the card. args: {board, card}",
    "kanban_create": "Create a card for the owning profile; the note goes in the spec. args: {board, assignee, title}",
    "kanban_archive": "Archive a card (needs confirm). args: {board, card}",
    "pr_merge": "NOT AVAILABLE YET: answers that pick it are refused. For a merge ask, offer kanban_comment "
    "('I will merge PR #N at <sha> in GitHub') or defer instead.",
    "pr_merge_waive_review": "NOT AVAILABLE YET: answers that pick it are refused. Do not offer it.",
    "handoff": "Give the answer back to the asking job, which applies it. args: {profile, job}",
    "attend": "The user approves and does a step himself (attended session, login, physical action). Posts his answer "
    "and a `**You:**` handoff to the card, then parks the item until the card changes; the card stays on his "
    "Waiting-on-you list until then. args: {board, card, you: the step he does, next?: what agents do after}",
    "defer": "Keep the item parked; nothing reaches the card. Only for 'not now' options, never for an approval. "
    "args: {kind: date|state_change|indefinite, until?}",
    "acknowledge": "Record that the user saw it. No write. args: {}",
    "rule_state": "Restore or suspend a delegated settle rule. Only for learn: items. args: {rule, state: active|suspended}",
}
# An item the user marked auto keeps its status when it gets new options; the auto step then applies one.


def _check_item(short_id: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{short_id}: entry must be an object"]
    if "watch" in entry:
        reason = str(entry.get("watch") or "").strip()
        if not reason:
            return [f"{short_id}: a watch needs a reason"]
        if not reason.startswith(WATCH_GROUNDS) or len(reason) < 20:
            return [f"{short_id}: a watch reason starts with one of {', '.join(WATCH_GROUNDS)} and names its evidence"]
        return []
    errors = []
    if not str(entry.get("question") or "").strip():
        errors.append(f"{short_id}: question is required")
    try:
        validate_options(entry.get("options") or [], str(entry.get("recommend") or ""))
    except ReviewError as exc:
        errors.append(f"{short_id}: {exc}")
    # Every blocker opens with what is blocked and why, before the question and options (review_tick.md).
    try:
        validate_summary(entry.get("summary"))
    except ReviewError as exc:
        errors.append(f"{short_id}: {exc}")
    return errors


# An option label that approves something. On ``defer`` it parks the item and nothing reaches the card (D75).
APPROVING = re.compile(r"^\s*(approve|accept|confirm|yes\b|go ahead|proceed|agree|ok\b|okay\b)", re.IGNORECASE)
PARKING_ACTIONS = ("defer", "acknowledge")


def _check_acts(item: dict[str, Any], entry: Any) -> list[str]:
    """A Kanban gate needs one option that acts, and an approval must carry the action that delivers it.

    Only Kanban gates: there a parked answer posts nothing to the card, and the card leaves the Review tab while it
    still waits on the user (D75). Cycle reviews may be all-park (``defer_exploring_cycle``)."""
    if not isinstance(entry, dict) or "watch" in entry or item.get("source") != "kanban":
        return []
    options = [o for o in entry.get("options") or [] if isinstance(o, dict)]
    errors = []
    if options and all(str(o.get("action") or "") in PARKING_ACTIONS for o in options):
        errors.append(
            f"{item['short_id']}: every option only parks the item, so no answer reaches the card; give the approving "
            "option the action that does it (kanban_unblock, kanban_comment, handoff, or attend for a step the user does "
            "himself), or watch it"
        )
    for option in options:
        if str(option.get("action") or "") == "defer" and APPROVING.match(str(option.get("label") or "")):
            errors.append(
                f"{item['short_id']}: option {option.get('id')} approves but its action is defer, which posts nothing; "
                "use kanban_unblock, kanban_comment, handoff, or attend"
            )
        if str(option.get("action") or "") == "attend" and not str((option.get("args") or {}).get("you") or "").strip():
            errors.append(f"{item['short_id']}: option {option.get('id')}: attend needs args.you (the step the user does)")
    return errors


def _check_targets(item: dict[str, Any], entry: Any) -> list[str]:
    """Options must not point at a closed cycle, and only rule-learning items may change a rule's state."""
    if not isinstance(entry, dict) or "watch" in entry:
        return []
    errors = []
    closed: dict[str, str] | None = None
    for option in entry.get("options") or []:
        if not isinstance(option, dict):
            continue
        action, args = option.get("action"), option.get("args") or {}
        if action == "rule_state" and item["source"] != "learn":
            errors.append(f"{item['short_id']}: option {option.get('id')}: rule_state is only for rule-learning items")
        if action == "record_decision" and item["source"] == "criteria":
            errors.append(
                f"{item['short_id']}: option {option.get('id')}: record_decision does not set the criteria, so the "
                "gate stays open; use set_cycle_fields with {cycle, fields: {success_criteria: ...}}"
            )
        if action == "set_cycle_fields":
            for problem in _cycle_field_problems(item, args):
                errors.append(f"{item['short_id']}: option {option.get('id')}: {problem}")
        into = str(args.get("into") or "") if action == "idea_status" else ""
        if into:
            closed = _closed_cycles(item) if closed is None else closed
            if into in closed:
                errors.append(f"{item['short_id']}: option {option.get('id')}: {into} is {closed[into]}; pick an open cycle")
    return errors


def _check_watch_owner(item: dict[str, Any], entry: Any) -> list[str]:
    """An agent_owned/waiting_on watch must not name only cards that already finished: the sync would reopen it at
    once, and the item would flap between watching and open every tick."""
    if not isinstance(entry, dict) or "watch" not in entry:
        return []
    from .review_collect import finished_owner_cards

    reason = str(entry.get("watch") or "").strip()
    board = (item.get("basis") or {}).get("board")
    if not board and reason.startswith(("agent_owned:", "waiting_on:")):
        from .settings import load_settings

        config, _ = load_settings()
        board = next((p.kanban_board for p in config.projects if p.id == item.get("project") and p.kanban_board), None)
    finished = finished_owner_cards(item, reason, str(board) if board else None)
    if finished:
        return [
            f"{item['short_id']}: the watch names only finished cards ({', '.join(finished)}); "
            "name the card or condition that still has to change, or give options"
        ]
    return []


def _cycle_field_problems(item: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """A set_cycle_fields option must apply cleanly: moving a cycle to measuring without its measurement fields
    (or any other change the validator rejects) is refused here, so the user is asked for them in the same option."""
    from .review_exec import cycle_fields_problems
    from .settings import load_settings

    cycle = str(args.get("cycle") or item.get("cycle") or "")
    if not cycle:
        return ["set_cycle_fields needs args.cycle"]
    config, _ = load_settings()
    project = next((p for p in config.projects if p.id == item.get("project")), None)
    if project is None:
        return []  # the executor reports the unknown project when the answer is applied
    problems = cycle_fields_problems(project, cycle, args.get("fields"))
    if any("measurement fields" in p or "observation_window" in p for p in problems):
        problems.append(
            "put the missing measurement fields in the same option (fields.primary_metric, source, baseline, "
            "observation_window {starts, length_days}; a spot-check with success_criteria needs no window)"
        )
    return problems


def _closed_cycles(item: dict[str, Any]) -> dict[str, str]:
    from .model import CLOSED_STAGES
    from .settings import load_settings
    from .store import load

    config, _ = load_settings()
    projects = [p for p in config.projects if p.id == item.get("project")] or config.projects
    out: dict[str, str] = {}
    for project in projects:
        for cycle_id, doc in load(project.root).cycles.items():
            stage = str(doc.frontmatter.get("stage"))
            if stage in CLOSED_STAGES:
                out[cycle_id] = stage
    return out


def _check_suggestion(index: int, entry: Any) -> list[str]:
    label = f"suggestion {index + 1}"
    if not isinstance(entry, dict):
        return [f"{label}: must be an object"]
    errors = []
    key = str(entry.get("key") or "")
    if not key.startswith("suggest:"):
        errors.append(f"{label}: key must start with 'suggest:'")
    if not str(entry.get("title") or "").strip() or not str(entry.get("question") or "").strip():
        errors.append(f"{label}: title and question are required")
    evidence = entry.get("evidence")
    if not isinstance(evidence, list) or not [e for e in evidence if str(e).strip()]:
        errors.append(f"{label}: at least one evidence path or URL is required")
    try:
        validate_options(entry.get("options") or [], str(entry.get("recommend") or ""))
    except ReviewError as exc:
        errors.append(f"{label}: {exc}")
    return errors


def apply(store: ReviewStore, payload: dict[str, Any]) -> dict[str, Any]:
    review_id = str(payload.get("review_id") or "")
    # A 30-minute tick writes options without a review; summaries, updates, and suggestions belong to the daily one.
    if review_id and store.review(review_id) is None:
        return {"errors": [f"unknown review '{review_id}'"], "skipped_suggestions": []}
    if not review_id and any(payload.get(k) for k in ("summaries", "updates", "suggestions")):
        return {"errors": ["summaries, updates, and suggestions need the review_id of today's review"], "skipped_suggestions": []}
    expected = {i["short_id"] for i in store.needs_options()}
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        return {"errors": ["items must be an object keyed by short id"], "skipped_suggestions": []}
    errors: list[str] = []
    for short_id, entry in items.items():
        item = store.get(short_id)
        if item is None or item["section"] != "gated":
            errors.append(f"{short_id}: not a gated item in this review")
            continue
        if item["status"] not in ("open", "watching", "auto"):
            errors.append(f"{short_id}: item is {item['status']}; only open items take new options")
            continue
        if item["status"] == "auto" and isinstance(entry, dict) and "watch" in entry:
            errors.append(f"{short_id}: the user marked it auto; apply, resolve, or return it (auto step), do not watch it")
            continue
        errors.extend(_check_item(short_id, entry))
        errors.extend(_check_targets(item, entry))
        errors.extend(_check_acts(item, entry))
        errors.extend(_check_watch_owner(item, entry))
    for short_id in sorted(expected - {str(k).upper() for k in items}):
        errors.append(f"{short_id}: missing; give options or a watch reason")
    suggestions = payload.get("suggestions") or []
    if not isinstance(suggestions, list):
        errors.append("suggestions must be a list")
        suggestions = []
    if len(suggestions) > MAX_SUGGESTIONS:
        errors.append(f"at most {MAX_SUGGESTIONS} suggestions, got {len(suggestions)}")
    for index, entry in enumerate(suggestions):
        errors.extend(_check_suggestion(index, entry))
    if errors:
        return {"errors": errors, "skipped_suggestions": []}

    for short_id, entry in items.items():
        if "watch" in entry:
            store.watch(short_id, str(entry["watch"]))
            continue
        store.reopen(short_id)
        store.add_version(
            short_id,
            question=entry["question"],
            options=entry["options"],
            recommend=entry["recommend"],
            context=str(entry.get("why") or ""),
            review_id=review_id or None,
            summary=entry["summary"],
        )
    if not review_id:
        return {"errors": [], "skipped_suggestions": [], "suggestions": []}
    skipped: list[str] = []
    linked: list[str] = []
    for entry in suggestions:
        evidence_hash = basis_hash(sorted(str(e) for e in entry["evidence"]))
        if store.is_dismissed(entry["key"], evidence_hash):
            skipped.append(entry["key"])
            continue
        item, _ = store.upsert_item(
            entry["key"],
            section="suggested",
            source="suggestion",
            title=entry["title"],
            basis={"evidence": sorted(str(e) for e in entry["evidence"])},
            links=[{"kind": "evidence", "ref": str(e)} for e in entry["evidence"]],
            project=entry.get("project"),
            initiative=entry.get("initiative"),
            evidence_hash=evidence_hash,
        )
        store.add_version(
            item["short_id"],
            question=entry["question"],
            options=entry["options"],
            recommend=entry["recommend"],
            context=str(entry.get("why") or ""),
            review_id=review_id,
        )
        linked.append(item["short_id"])
    store.link_items(review_id, linked)
    store.update_review(
        review_id,
        summaries_json=json.dumps({"summaries": payload.get("summaries") or {}, "updates": payload.get("updates") or {}}),
    )
    return {"errors": [], "skipped_suggestions": skipped, "suggestions": linked}
