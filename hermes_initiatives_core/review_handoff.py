"""Handed-off answers for profiles with no job of their own: give each one a Kanban card so it gets applied.

A request filed with ``request_decision.py --owner-job <cron id>`` is picked up by that job (``--answers``). A
request filed from an interactive session names only ``--owner-profile``; nothing polls for it, so a ``handoff``
answer would sit in ``ready_for_owner`` until someone remembers. The 30-minute tick calls :func:`dispatch`, which
creates one card per answer on the project's board, assigned to the owner profile. The card carries the question,
the chosen option, and the user's note unchanged, and tells the worker to acknowledge the item when it has applied
the answer. The card key is ``review-handoff:<item>:<answer id>``, so a later tick never makes a second card, and a
new answer (after a reopen) gets its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .review_exec import ActionFailed, Kanban, Runner, default_runner
from .reviews import ReviewStore
from .settings import Settings

ACTION = "handoff_card"
SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"


def _project(config: Settings, item: dict[str, Any]):
    """The item's project by id; a request filed with the board name instead is matched on the board."""
    ref = item.get("project") or ""
    project = config.project(ref) if ref else None
    if project is None and ref:
        project = next((p for p in config.projects if p.kanban_board == ref), None)
    if project is None and not ref and len(config.projects) == 1:
        project = config.projects[0]
    return project


def card_body(item: dict[str, Any], option: dict[str, Any], note: str) -> str:
    version = item.get("version") or {}
    sid = item["short_id"]
    lines = [
        f"The user answered review item {sid}. Apply the answer, then acknowledge it.",
        "",
        f"Question: {version.get('question') or item['title']}",
        f"Answer: {option['id']}: {option['label']}",
    ]
    if note:
        lines += ["", "Note from the user (unchanged):", note]
    summary = version.get("summary") or {}
    if summary.get("what") or summary.get("why"):
        lines += ["", f"What is held: {summary.get('what') or '-'}", f"Why: {summary.get('why') or '-'}"]
    context = version.get("context") or item.get("context")
    if context:
        lines += ["", "Context from the request:", context]
    if item.get("blocks"):
        lines += ["", f"Cards this answer unblocks: {', '.join(item['blocks'])}"]
    lines += [
        "",
        "Do exactly what the chosen option says; it is the user's decision, so do not re-ask it. If you cannot apply "
        "it (the situation changed, or it needs something only the user can do), block this card and say why "
        "instead of guessing.",
        "",
        "When it is applied, record what you did on the review item, in one or two sentences:",
        "",
        f'    python3 {SCRIPTS / "request_decision.py"} --ack {sid} --receipt "<what was applied>"',
        "",
        'The item stays under "In progress" in the Review tab until it is acknowledged.',
    ]
    return "\n".join(lines)


def dispatch(store: ReviewStore, config: Settings, run: Runner | None = None) -> dict[str, list]:
    """Create the missing card for each handed-off answer whose owner is a profile without a job."""
    kanban = Kanban(run or default_runner)
    created: list[dict[str, str]] = []
    errors: list[str] = []
    for item in store.items(statuses=("ready_for_owner",)):
        if item.get("owner_job") or not item.get("owner_profile") or not str(item["source"]).startswith("request:"):
            continue  # a job reads its own answers with --answers
        sid = item["short_id"]
        try:
            answer, option = store.latest_answer(sid)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the rest
            errors.append(f"{sid}: {exc}")
            continue
        done = [
            a for a in store.actions(sid) if a["action"] == ACTION and a["answer_id"] == answer["id"] and a["state"] == "created"
        ]
        if done:
            continue
        project = _project(config, item)
        if project is None or not project.kanban_board:
            errors.append(f"{sid}: no project with a Kanban board for '{item.get('project') or '-'}'")
            continue
        try:
            card = kanban.create(
                project.kanban_board,
                title=f"Apply review answer {sid}: {item['title']}"[:200],
                assignee=str(item["owner_profile"]),
                body=card_body(item, option, answer.get("note") or ""),
                key=f"review-handoff:{sid}:{answer['id']}",
                skills=tuple(project.plan_skills),
            )
        except ActionFailed as exc:
            store.log_action(answer["id"], sid, ACTION, {"board": project.kanban_board}, "failed", error=str(exc)[:500])
            errors.append(f"{sid}: {exc}")
            continue
        store.log_action(
            answer["id"], sid, ACTION, {"board": project.kanban_board, "card": card}, "created", receipt=f"card {card}"
        )
        created.append({"short_id": sid, "card": card, "assignee": str(item["owner_profile"])})
    return {"created": created, "errors": errors}
