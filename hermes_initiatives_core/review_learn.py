"""Rule learning: the user's own answers propose new delegated rules; his overrules suspend them.

Two loops keep ``AGENT_RULES`` close to what the user actually decides (user decision 2026-10-14). A third,
``review_learned``, appends a rule each time he marks an item auto; overrule suspends those like any other rule:

- **Propose.** ``mine`` groups answered items by a signature (source kind, action, argument shape). When the latest
  ``MIN_STREAK`` answers with one signature all took the recommended option with no note, the user is answering
  that kind of item on autopilot. ``propose`` files one review item for it. Approving that item only creates a card
  for the project's intake lane to plan and code the rule with tests; the rule exists only after that code merges.
  A signature on the bar (success criteria, stage changes, merges, unblocks of human gates, new scope, consent) is
  never proposed, however often he agrees.
- **Suspend.** ``overrule`` records that a settlement was wrong. The rule that made it refuses everything at once,
  and a review item asks whether to restore it, keep it suspended, or plan an undo of what it wrote.

Nothing here writes a rule or an initiative file. Proposals are filed once per signature (any status, ever).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .reviews import ReviewStore, basis_hash, now_iso
from .settings import ProjectSettings

MIN_STREAK = 3
LOOKBACK_DAYS = 60

# Actions that are on the bar whatever their source: the user keeps these (review_tick.md "The bar").
PROTECTED_ACTIONS = (
    "idea_reject",
    "kanban_archive",
    "kanban_unblock",
    "pr_merge",
    "pr_merge_waive_review",
    "record_decision",
    "handoff",
    "attend",
    "rule_state",
)
# Cycle fields whose change is a stage, criteria, verdict, or disposition decision.
PROTECTED_FIELDS = ("stage", "measurement", "result", "verdict", "disposition")
# Source kinds that are always his: success criteria, agent requests (withdraw_answered covers those), and rules.
PROTECTED_KINDS = ("criteria", "request", "learn")


def _kind(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "")
    return "request" if source.startswith("request:") else source


def signature(item: dict[str, Any], option: dict[str, Any]) -> str | None:
    """A stable label for "this kind of item, answered this way", or None when the pair is on the bar."""
    kind, action, args = _kind(item), str(option.get("action") or ""), option.get("args") or {}
    if item.get("urgent_rule") or kind in PROTECTED_KINDS or action in PROTECTED_ACTIONS:
        return None
    if kind == "kanban" and action != "defer":
        return None  # recovery already judged these gates human; only parking them is routine
    if action == "set_cycle_fields":
        fields = sorted((args.get("fields") or {}).keys())
        if not fields or set(fields) & set(PROTECTED_FIELDS):
            return None
        shape = "+".join(fields)
    elif action == "idea_status":
        status = str(args.get("status") or "")
        if status == "promoted":
            return None  # new scope; repair_promotion covers the one delegated case
        shape = status
    elif action == "defer":
        shape = str(args.get("kind") or "state_change")
    elif action == "kanban_create":
        shape = str(args.get("assignee") or "")
    else:
        shape = ""
    reasons = (item.get("basis") or {}).get("reasons") if kind == "cycle" else None
    context = "+".join(sorted(str(r) for r in reasons)) if reasons else ""
    return ":".join(part for part in (kind, context, action, shape) if part)


def mine(store: ReviewStore, *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Signatures whose latest MIN_STREAK answers were all the recommended option with no note."""
    now = now or dt.datetime.now(dt.UTC)
    since = (now - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    history: dict[str, list[dict[str, Any]]] = {}
    for item in store.answered_items(since=since):
        for answer in store.answers(item["short_id"]):
            if answer["surface"] == "agent":
                continue  # applied by an agent under an auto mark: not the user's own answer
            version = store.versions(item["short_id"])[answer["version"] - 1]
            option = next((o for o in version["options"] if o["id"] == answer["option"]), None)
            if option is None:
                continue
            label = signature(item, option)
            if label is None:
                continue
            history.setdefault(label, []).append(
                {
                    "short_id": item["short_id"],
                    "title": item["title"],
                    "project": item.get("project"),
                    "answered_at": answer["answered_at"],
                    "label": option["label"],
                    "agreed": answer["option"] == version["recommend"] and not (answer["note"] or "").strip(),
                }
            )
    found = []
    for label, rows in sorted(history.items()):
        rows.sort(key=lambda r: r["answered_at"])
        streak: list[dict[str, Any]] = []
        for row in reversed(rows):
            if not row["agreed"]:
                break
            streak.append(row)
        if len(streak) >= MIN_STREAK:
            found.append({"signature": label, "evidence": list(reversed(streak)), "answers": len(rows)})
    return found


def _project(store_items: list[dict[str, Any]], projects: list[ProjectSettings]) -> ProjectSettings | None:
    ids = {r.get("project") for r in store_items if r.get("project")}
    if len(ids) == 1:
        return next((p for p in projects if p.id in ids), None)
    return projects[0] if len(projects) == 1 else None


def propose(store: ReviewStore, projects: list[ProjectSettings], *, now: dt.datetime | None = None) -> list[str]:
    """File one review item per new candidate signature. Returns the short ids filed."""
    filed = []
    for candidate in mine(store, now=now):
        key = f"learn:rule:{candidate['signature']}"
        if store.has_key(key):
            continue
        project = _project(candidate["evidence"], projects)
        lane = project and (project.intake_assignee or project.plan_assignee)
        if project is None or not project.kanban_board or not lane:
            continue
        ids = ", ".join(r["short_id"] for r in candidate["evidence"])
        examples = "\n".join(
            f"- {r['short_id']} ({r['answered_at'][:10]}): {r['title'][:90]} -> {r['label'][:90]}" for r in candidate["evidence"]
        )
        basis = {"signature": candidate["signature"], "evidence": [r["short_id"] for r in candidate["evidence"]]}
        item, _ = store.upsert_item(
            key,
            section="gated",
            source="learn",
            title=f"Delegate a rule for {candidate['signature']}?",
            basis=basis,
            project=project.id,
            evidence_hash=basis_hash(basis),
        )
        body = "\n".join(
            [
                "Add a delegated rule to hermes-initiatives (`hermes_initiatives_core/review_settle.py`) for items of",
                f"signature `{candidate['signature']}`. The user took the recommended option with no note on each:",
                examples,
                "",
                "Plan first; the plan names the exact predicate the rule checks in code, what it writes, and what it",
                "refuses. Follow the existing rules: a refusal writes nothing, every settlement writes a by: agent audit",
                "row, and the rule must refuse anything on the review bar. Add tests in tests/test_review_settle.py,",
                "list the rule in AGENT_RULES and review_tick.md. The user merges the PR.",
            ]
        )
        options = [
            {
                "id": "A",
                "label": f"Plan this rule: create a card for {lane} (the rule exists only after its PR merges)",
                "action": "kanban_create",
                "args": {
                    "board": project.kanban_board,
                    "assignee": lane,
                    "title": f"Plan delegated rule: {candidate['signature']}"[:200],
                    "body": body,
                },
            },
            {
                "id": "B",
                "label": "No: I keep deciding these myself; do not propose it again",
                "action": "acknowledge",
                "args": {},
            },
            {
                "id": "C",
                "label": "Ask me again in 30 days",
                "action": "defer",
                "args": {"kind": "date", "until": ((now or dt.datetime.now(dt.UTC)) + dt.timedelta(days=30)).date().isoformat()},
            },
        ]
        context = (
            f"You answered {len(candidate['evidence'])} items of this kind in a row with the recommended option and no "
            f"note ({ids}). An agent could settle them under a coded rule. You still approve the plan and merge it."
        )
        store.add_version(
            item["short_id"],
            question=f"Should agents settle items like {ids} under a coded rule?",
            options=options,
            recommend="A",
            context=context,
        )
        filed.append(item["short_id"])
    return filed


def overrule(store: ReviewStore, projects: list[ProjectSettings], short_id: str, reason: str) -> dict[str, Any]:
    """Suspend the rule that settled *short_id* and file the follow-up decision for the user."""
    done = store.overrule(short_id, reason)
    settled = store.get(done["short_id"]) or {}
    project = next((p for p in projects if p.id == settled.get("project")), None) or (projects[0] if len(projects) == 1 else None)
    rule = done["rule"]
    key = f"learn:overrule:{done['short_id']}"
    basis = {"overruled": done["short_id"], "rule": rule, "at": now_iso()}
    item, _ = store.upsert_item(
        key,
        section="gated",
        source="learn",
        title=f"Rule {rule} suspended after you overruled {done['short_id']}",
        basis=basis,
        project=project.id if project else None,
        initiative=settled.get("initiative"),
        cycle=settled.get("cycle"),
    )
    options = [
        {
            "id": "A",
            "label": f"Keep {rule} suspended; leave what it wrote for {done['short_id']} as it is",
            "action": "acknowledge",
            "args": {},
        },
        {
            "id": "B",
            "label": f"Restore {rule}: the overrule was a one-off",
            "action": "rule_state",
            "args": {"rule": rule, "state": "active"},
        },
    ]
    lane = project and (project.intake_assignee or project.plan_assignee)
    if project and project.kanban_board and lane:
        options.append(
            {
                "id": "C",
                "label": f"Keep {rule} suspended and create a card for {lane} to undo what it wrote for {done['short_id']}",
                "action": "kanban_create",
                "args": {
                    "board": project.kanban_board,
                    "assignee": lane,
                    "title": f"Undo agent settlement {done['short_id']} ({rule})",
                    "body": f"Rule {rule} settled {done['short_id']}: {done['receipt']}\nThe user overruled it: {reason}\n"
                    "Plan the reversal of exactly that write (files, idea status, or card), then apply it.",
                },
            }
        )
    store.add_version(
        item["short_id"],
        question=f"{rule} is suspended. What should happen next?",
        options=options,
        recommend="A",
        context=f"{done['short_id']} was settled by {rule}: {done['receipt'][:300]}\nYour reason: {reason}",
    )
    return {**done, "follow_up": item["short_id"]}
