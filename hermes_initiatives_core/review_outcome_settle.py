"""``plan_outcome_followup``: one generated, read-only plan card for a ready outcome review (2026-10-24).

The rule never executes the option's proposed implementation and never selects a product alternative. It reads the
current assessment receipt, proves the evidence and proposal by SHA-256, and starts one card that may only
investigate and plan against a decision the user already approved. The source hashes make the card's authority
auditable: the cited user row and the cycle file as they were when the agent decided.

The rule is bound to the collector: a changed basis, a suspended rule, a user answer, or a prior plan without a
later user decision refuses before any write. One card per item, at most ``FOLLOWUPS_PER_DAY`` a day.

The recommendation must carry the exact signature ``cycle:outcome_review_ready:kanban_create:<plan lane>``, and
every option in the item must stay off the review bar. One verdict, disposition, stage, measurement or decision
option anywhere in the item refuses the whole item: a routine option beside a protected one is not separable from
the user's own decision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from . import outcome_loop, review_collect, review_learn, reviews, writers
from .docs import parse_markdown
from .kanban import CYCLE_MARKER
from .lifecycle import as_list
from .model import ASSESSMENT_STATUSES, CYCLE_ID
from .review_deferral import fresh_doc
from .store import active_observations, load

RULE = "plan_outcome_followup"
FOLLOWUPS_PER_DAY = 3
DECIDED_STAGES = ("decided", "shipping", "measuring")
# The option actions this rule tolerates: everything else carries a human gate and refuses.
OPTION_ACTIONS = ("defer", "acknowledge", "record_decision", "set_cycle_fields", "kanban_create")
CARD_ARGS = {"board", "assignee", "title", "body"}


def _canonical(value: Any) -> str:
    # ``default=str``: an unquoted YAML date parses as ``datetime.date``; its ISO text is what the hashing
    # caller and the audit row record, so the digest must not depend on how the loader represented it.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _cited_user(settle, doc: Any, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    users = settle._user_decisions(doc.frontmatter)
    index = args.get("repairs_decision")
    if type(index) is not int or not 0 <= index < len(users):
        raise settle.SettleRefused(f"repairs_decision must index one of the cycle's {len(users)} user decisions (0-based)")
    if index != len(users) - 1:
        raise settle.SettleRefused("repairs_decision must cite the latest user decision; older intent is ambiguous")
    return index, users[index]


def _artifact(settle, path_value: Any, expected: Any) -> str:
    path = Path(str(path_value or "")).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise settle.SettleRefused(f"assessment artifact {path or '?'} is missing or a symlink; re-collect the evidence")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise settle.SettleRefused(f"assessment artifact {path} is unreadable: {exc}") from exc
    if not content.strip() or not isinstance(expected, str) or _digest(content) != expected:
        raise settle.SettleRefused(f"assessment artifact {path} does not match its recorded SHA-256")
    return str(path)


def _option_signature(settle, item: dict[str, Any], candidate: Any) -> str:
    """The option's review-bar signature; refuses a malformed, gated, confirmed or protected option.

    The rule may only settle the whole item while every option stays routine (Appendix E.8). One protected
    alternative anywhere in the item, recommended or not, keeps the item with the user: a verdict, disposition,
    stage, measurement or decision is not separable from the routine option beside it. Types are checked before the
    shared helper, so a malformed option, its arguments or its fields refuses instead of raising inside it.
    """
    if not isinstance(candidate, dict):
        raise settle.SettleRefused("an option is not a mapping; the item stays with the user")
    name = candidate.get("id")
    action = candidate.get("action")
    if action not in OPTION_ACTIONS:
        raise settle.SettleRefused(f"option {name} carries a human gate")
    if candidate.get("confirm"):
        raise settle.SettleRefused("an option needs confirm; the user answers it")
    args = candidate.get("args")
    if args is not None and not isinstance(args, dict):
        raise settle.SettleRefused(f"option {name} has malformed arguments; the item stays with the user")
    fields = (args or {}).get("fields")
    if fields is not None and not isinstance(fields, dict):
        raise settle.SettleRefused(f"option {name} has malformed fields; the item stays with the user")
    signature = review_learn.signature(item, candidate)
    if signature is None:
        raise settle.SettleRefused(f"option {name} is on the review bar; the outcome stays with the user")
    return signature


def followup(settler, item: dict[str, Any], args: dict[str, Any], why: str) -> dict[str, Any]:
    """Start one bounded plan card for a fresh outcome-review recommendation, then settle the item."""
    from . import review_settle as settle

    store, project = settler.store, settler.project
    if not isinstance(args, dict) or set(args) != {"repairs_decision", "authority_sha256", "source_sha256"}:
        raise settle.SettleRefused(
            f"{RULE} takes exactly repairs_decision, authority_sha256, source_sha256 as arguments; authority_sha256 "
            f"and source_sha256 are the SHA-256 of the cited user row and the cycle file; got "
            f"{', '.join(sorted(map(str, args)))}"
        )
    if type(args.get("repairs_decision")) is not int:
        raise settle.SettleRefused("repairs_decision must be a 0-based index among the cycle's by: user decisions")
    board, lane = project.kanban_board, project.plan_assignee
    if not board:
        raise settle.SettleRefused("project settings need kanban_board before a follow-up plan can be created")
    if not lane:
        raise settle.SettleRefused("project settings need plan_assignee before a follow-up plan can be created")
    if item.get("project") != project.id:
        raise settle.SettleRefused(f"the item must carry this project ({project.id}); got {item.get('project')}")
    if item.get("section") != "gated" or item.get("source") != "cycle":
        raise settle.SettleRefused("only a gated cycle item can start the follow-up plan")
    cycle_id = str(item.get("cycle") or "")
    if not CYCLE_ID.fullmatch(cycle_id) or item.get("key") != f"cycle:{project.id}:{cycle_id}:gate":
        raise settle.SettleRefused("the item key and cycle must name this project's cycle")
    if any(item.get(k) is not None for k in ("urgent_rule", "owner_profile", "owner_job")) or item.get("blocks"):
        raise settle.SettleRefused("the item carries urgency, an owner, or blocked work; it stays with the user")
    basis = item.get("basis") or {}
    if (
        basis.get("reasons") != ["outcome_review_ready"]
        or basis.get("stage") not in DECIDED_STAGES
        or basis.get("verdict") not in ("unset", None)
        or basis.get("disposition") not in ("unset", None)
    ):
        raise settle.SettleRefused(
            "the basis must be a fresh outcome review of a decided cycle with an unset verdict and disposition"
        )
    sid = item["short_id"]
    if not reviews.current(item) or sid in {i["short_id"] for i in store.needs_options()}:
        raise settle.SettleRefused("the basis or options are not current; the next tick rewrites them")
    if store.answers(sid):
        raise settle.SettleRefused("the user already answered this item; the answer wins")
    version = item["version"]
    options = version["options"]
    if not isinstance(options, list):
        raise settle.SettleRefused("the item's options are malformed; the item stays with the user")
    chosen = [o for o in options if isinstance(o, dict) and o.get("id") == version.get("recommend")]
    if len(chosen) != 1 or chosen[0].get("action") != "kanban_create":
        raise settle.SettleRefused("the recommended option must be one kanban_create")
    option = chosen[0]
    expected = f"cycle:outcome_review_ready:kanban_create:{lane}"
    for candidate in options:
        signature = _option_signature(settle, item, candidate)
        if candidate is option and signature != expected:
            raise settle.SettleRefused(f"the recommended signature must be {expected}")
    card_args = option.get("args") or {}
    if set(card_args) - CARD_ARGS:
        raise settle.SettleRefused("the recommended option has unsupported arguments")
    if card_args.get("assignee") != lane:
        raise settle.SettleRefused("the recommended assignee must be the plan lane")
    if "board" in card_args and card_args["board"] != board:
        raise settle.SettleRefused("the recommended board must be the project board")
    title = card_args.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise settle.SettleRefused("the recommended title must be non-empty and at most 200 characters")
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise settle.SettleRefused("the sources are malformed or ambiguous")
    if cycle_id not in snapshot.cycles:
        raise settle.SettleRefused(f"{cycle_id} is not a recorded cycle")
    try:
        # ``fresh_doc`` re-reads the cycle and refuses a symlinked or relocated cycle or initiative file, before
        # this rule ever claims the item or creates a card.
        doc = fresh_doc(project, snapshot, cycle_id)
    except (ValueError, OSError) as exc:
        raise settle.SettleRefused(str(exc)) from exc
    if doc.frontmatter.get("stage") not in DECIDED_STAGES:
        raise settle.SettleRefused("the recorded cycle must be decided, shipping, or measuring")
    if (basis.get("verdict") is None or basis.get("disposition") is None) and (
        (doc.frontmatter.get("result") or {}).get("verdict", "unset") not in ("unset", None)
        or (doc.frontmatter.get("disposition") or {}).get("action", "unset") not in ("unset", None)
    ):
        # The collector spells an absent verdict as null; that is ``unset`` only while the live source is unset.
        raise settle.SettleRefused("the basis omits a verdict or disposition that the cycle now records")
    owner = snapshot.cycle_initiative.get(cycle_id)
    initiative = snapshot.initiatives.get(owner)
    if owner != item.get("initiative") or initiative is None:
        raise settle.SettleRefused("the cycle must belong to the item's initiative")
    if initiative.frontmatter.get("status") != "active":
        raise settle.SettleRefused("the initiative must be active")
    if not (initiative.frontmatter.get("outcome_policy") or {}).get("enabled"):
        raise settle.SettleRefused("the initiative's outcome policy is not enabled")
    index, authority = _cited_user(settle, doc, args)
    try:
        authority_date = dt.date.fromisoformat(str(authority.get("date") or ""))
    except ValueError as exc:
        raise settle.SettleRefused("the cited user decision needs an ISO date") from exc
    opened = str(item.get("opened_at") or "")[:10]
    if opened and authority_date.isoformat() > opened:
        raise settle.SettleRefused(
            f"the cited decision ({authority_date}) is newer than this item opened ({opened}); the user saw both"
        )
    if any(str(row.get("date") or "") > authority_date.isoformat() for row in settle._user_decisions(initiative.frontmatter)):
        # A later initiative decision may already replace the cycle decision this plan would rest on. Same-day
        # rows are not later: the fixture's origin decision and its cycle decision share a date.
        raise settle.SettleRefused("a later initiative user decision needs reconciliation before a follow-up plan")
    try:
        initiative_bytes = initiative.path.read_bytes()
        initiative_digest = _digest(initiative_bytes)
        if parse_markdown(initiative_bytes.decode("utf-8"), initiative.path).frontmatter != initiative.frontmatter:
            raise settle.SettleRefused("initiative authority changed during collection")
    except OSError as exc:
        raise settle.SettleRefused(f"the initiative file is unreadable: {exc}") from exc
    epoch = str(basis.get("epoch") or "")
    rows = active_observations(snapshot.observations.get(cycle_id))
    receipt = outcome_loop.latest_receipt(rows, epoch, None)
    if (
        receipt is None
        or receipt.get("at") != basis.get("receipt")
        or receipt.get("status") != basis.get("finding")
        or receipt.get("artifact_sha256") != basis.get("artifacts")
        or basis.get("finding") not in ASSESSMENT_STATUSES
        or not isinstance(basis.get("artifacts"), dict)
    ):
        raise settle.SettleRefused("the assessment basis does not match a recorded receipt for this epoch")
    evidence = _artifact(settle, receipt.get("evidence"), basis["artifacts"].get("evidence"))
    proposal = _artifact(settle, receipt.get("proposal"), basis["artifacts"].get("proposal"))
    decisions = doc.frontmatter.get("decisions") or []
    previous = [i for i, row in enumerate(decisions) if isinstance(row, dict) and row.get("rule") == RULE]
    if previous and not settle._user_decisions({"decisions": decisions[previous[-1] + 1 :]}):
        raise settle.SettleRefused("chain guard: the previous follow-up plan needs a later user decision")
    failed = []
    for action in store.actions_since(None):
        if action["action"] != f"settle:{RULE}" or action["state"] != "failed":
            continue
        prior = store.get(action["short_id"])
        if prior and prior["key"] == item["key"]:
            failed.append(action["at"][:10])
    if failed and not any(str(row.get("date") or "") > max(failed) for row in settle._user_decisions(doc.frontmatter)):
        raise settle.SettleRefused("chain guard: the failed follow-up needs a strictly later user decision")
    if _digest(_canonical(authority).encode("utf-8")) != args.get("authority_sha256"):
        raise settle.SettleRefused("the cited decision does not match the authority SHA-256 in the arguments")
    if _digest(doc.path.read_bytes()) != args.get("source_sha256"):
        raise settle.SettleRefused("the cycle file changed; the source SHA-256 in the arguments is stale")
    fresh = next((c for c in review_collect._cycle_gates(project, snapshot, dt.date.today()) if c["key"] == item["key"]), None)
    if fresh is None or reviews.basis_hash(fresh["basis"]) != item["basis_hash"]:
        raise settle.SettleRefused("stale collector candidate or basis; the next tick rewrites this item")
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).replace(microsecond=0).isoformat()
    if len([a for a in store.actions_since(since) if a["action"] == f"settle:{RULE}"]) >= FOLLOWUPS_PER_DAY:
        raise settle.SettleRefused(f"{FOLLOWUPS_PER_DAY} {RULE} actions exist in the last 24 hours")
    body = "\n".join(
        [
            f"- Initiative cycle: {cycle_id}",
            "",
            "Write a read-only investigation and repair plan for the ready outcome assessment of this cycle.",
            f"Cycle file: {doc.path}",
            f"Assessment epoch: {epoch}; finding (not a user verdict): {basis.get('finding')}.",
            f"Evidence: {evidence} (sha256 {basis['artifacts'].get('evidence')})",
            f"Proposal: {proposal} (sha256 {basis['artifacts'].get('proposal')})",
            f"The cited user decision (decisions[by: user][{index}], {authority.get('date')}): {authority.get('decision')}",
            "The evidence and proposal are alternatives, not a chosen policy.",
            "",
            "Planning context only, not authority to execute:",
            "- Write the plan the user can approve or reject. Read-only analysis that the plan needs is allowed.",
            "- Do not implement, change config, data, or credentials, or open a deployment.",
            "- Do not adopt a product policy, create a new cycle, or widen the approved scope.",
            "- Verdict: undecided. Disposition: undecided; the outcome review, verdict, "
            f"and disposition stay with {settler.owner_name}.",
            "- Complete this card with the plan's path and SHA-256; the daily review then asks the user to decide.",
            "",
            f"Started by the daily review under {settler.owner_name}'s rule {RULE} "
            f"(review {sid}, recommended option {option['id']}).",
            f"Why now: {why}",
        ]
    )
    if any(marker.lower() != cycle_id for marker in CYCLE_MARKER.findall(f"{title}\n{body}")):
        raise settle.SettleRefused("every Initiative cycle marker must name this cycle")
    if not CYCLE_MARKER.search(body):
        raise settle.SettleRefused("the card body must carry the Initiative cycle marker")
    if not store.claim_under_rule(sid, basis_hash=item["basis_hash"], version=version["n"]):
        raise settle.SettleRefused("the item changed while settling; the claim was refused")
    card = None
    receipt_text = ""
    try:
        if _digest(doc.path.read_bytes()) != args["source_sha256"]:
            raise settle.ActionFailed("the cycle file changed under the claim")
        card = settler.kanban.create(
            board,
            title=f"Plan outcome follow-up {cycle_id}",
            assignee=lane,
            body=body,
            key=f"review-settle:{sid}:{item['basis_hash']}",
            skills=project.plan_skills,
        )
        audit = settle._audit_row(
            RULE,
            sid,
            f"Started read-only follow-up plan {card} for {lane}; the outcome review, verdict, disposition, "
            f"and scope stay with {settler.owner_name}",
            why,
            card=card,
            epoch=epoch,
            authority_sha256=args["authority_sha256"],
            source_sha256=args["source_sha256"],
        )

        def change(current: dict[str, Any]) -> None:
            # Source CAS inside the writer's own lock: a cycle or initiative file edited after the checks can no
            # longer receive this agent's link and audit row, and both land in one write instead of two nested locks.
            if _digest(doc.path.read_bytes()) != args["source_sha256"] or current != doc.frontmatter:
                raise settle.ActionFailed("the cycle file changed before the link and audit could be written")
            if _digest(initiative.path.read_bytes()) != initiative_digest:
                raise settle.ActionFailed("the initiative changed before the link and audit could be written")
            cards = [str(c) for c in as_list(current.get("kanban_cards"))]
            if card in {str(c) for c in as_list(current.get("excluded_cards"))}:
                raise settle.ActionFailed(f"the user excluded {card} from {cycle_id}")
            if card not in cards:
                cards.append(card)
            current["kanban_cards"] = cards
            current["decisions"] = [*(current.get("decisions") or []), audit]

        after = writers.edit_frontmatter(doc.path, project.root, change)
        if card not in [str(c) for c in as_list(after.get("kanban_cards"))] or (after.get("decisions") or [])[-1:] != [audit]:
            raise settle.ActionFailed("the card link and audit row failed read-back")
        receipt_text = f"{card} for {lane}, linked to {cycle_id}"
        if not store.settle(sid, RULE, receipt_text, expect=("applying",), args=args):
            raise settle.ActionFailed("the claimed item could not be settled")
    except Exception as exc:  # noqa: BLE001 - release the claim, keeping any created card in the error
        error = f"{card}: {exc}" if card else str(exc)
        store.release_rule_claim(sid, rule=RULE, error=error)
        raise settle.ActionFailed(f"{sid}: {RULE} failed: {error}") from exc
    return {"short_id": sid, "rule": RULE, "receipt": receipt_text}
