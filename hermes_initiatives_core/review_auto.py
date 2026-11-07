"""Auto items: the user hands one review item to the agents, and the recovery tick handles it.

The user marks an item ``auto`` (Review tab button, or ``auto D12: note`` in the review topic). The mark is his
decision for that one item; the note is his instruction. On its next run the recovery tick does one of three
things, each checked here before anything is written:

- ``apply``: answer the item with one of its current options. The answer is recorded with surface ``agent`` and runs
  through the normal executor (source recheck, read-back writes, logged actions). Decision rows record the user's
  delegation (``by: user``) and the agent's choice (``by: agent``, rule ``auto_delegated``). When the option is
  applied, the item moves to ``settled`` so it shows under "Settled by agents", where the user can overrule it.
- ``resolve``: close the item without an option because nothing is left to do (the agent names the evidence).
- ``return``: give the item back to the user with the reason; the next tick writes fresh options for him.

``apply`` and ``resolve`` also append a delegated rule (``review_learned``): why the triage did not handle the item
(``why_not_auto``), and which later items the same step covers (``when`` and ``match``), or a grounded ``skip``. The
rule is checked before anything is written. Later ticks settle items it covers with ``daily_review.py settle``
under the rule's name (``settle_learned``), without a new mark.

Refused: irreversible options (they need the user's confirm), merges, rule changes, options written for an older
source state, and anything while the user has suspended ``auto_delegated`` by overruling one of its settlements.
"""

from __future__ import annotations

from typing import Any

from . import review_learned
from .review_exec import UNSUPPORTED, Executor, Runner
from .reviews import ReviewError, ReviewStore, current

RULE = "auto_delegated"
VERBS = ("apply", "resolve", "return")
NEVER = ("rule_state", "attend")  # rule_state changes how agents decide; attend is a step only the user does


class AutoRefused(ValueError):
    """The auto step does not cover this; nothing was written."""


def _suspended(store: ReviewStore) -> None:
    state = store.rule_states().get(RULE)
    if state and state["state"] == "suspended":
        raise AutoRefused(f"rule {RULE} is suspended since {state['at'][:10]}: {state['reason']}")


def _auto_item(store: ReviewStore, short_id: str) -> dict[str, Any]:
    item = store.get(short_id)
    if item is None:
        raise AutoRefused(f"unknown item {short_id}")
    if item["status"] != "auto":
        raise AutoRefused(f"{item['short_id']} is {item['status']}; only items the user marked auto are handled")
    return item


def _why(why: str) -> str:
    why = (why or "").strip()
    if len(why) < 20:
        raise AutoRefused("why must say in a sentence what the agent checked and why this step is right")
    return why


def pending(store: ReviewStore) -> list[dict[str, Any]]:
    """Items waiting for the agents, with what the tick needs to decide (the tick prints these as ``auto``)."""
    out = []
    for item in store.items(statuses=("auto",)):
        version = item.get("version") or {}
        out.append(
            {
                "short_id": item["short_id"],
                "key": item["key"],
                "source": item["source"],
                "title": item["title"],
                "project": item["project"],
                "initiative": item["initiative"],
                "cycle": item["cycle"],
                "links": item["links"],
                "context": item["context"],
                "user_note": (item.get("auto") or {}).get("note") or "",
                "marked_at": (item.get("auto") or {}).get("at"),
                "options_current": current(item),
                "question": version.get("question"),
                "recommend": version.get("recommend"),
                "options": [
                    {k: o[k] for k in ("id", "label", "action", "args", "confirm")} for o in version.get("options") or []
                ],
            }
        )
    return out


def _option(item: dict[str, Any], option_id: str) -> dict[str, Any]:
    sid = item["short_id"]
    if not current(item):
        raise AutoRefused(f"{sid} has no options for its current source; write new options first, or return it")
    option = next((o for o in item["version"]["options"] if o["id"] == option_id), None)
    if option is None:
        raise AutoRefused(f"{sid} has no option {option_id or '(empty)'}")
    if option.get("confirm"):
        raise AutoRefused(f"{sid} option {option_id} cannot be undone; it needs the user's confirm, so return it")
    if option["action"] in UNSUPPORTED or option["action"] in NEVER:
        raise AutoRefused(f"{sid} option {option_id} ({option['action']}) stays with the user")
    return option


def _rule(item: dict[str, Any], option: dict[str, Any] | None, verb: str, entry: dict[str, Any]) -> dict | None:
    try:
        return review_learned.prepare(item, option, verb, entry.get("rule"))
    except review_learned.RuleRefused as exc:
        raise AutoRefused(str(exc)) from exc


def _apply_option(
    store: ReviewStore, item: dict[str, Any], option: dict[str, Any], why: str, rule: str, run: Runner | None
) -> dict[str, Any]:
    sid = item["short_id"]
    try:
        recorded = store.answer(sid, option["id"], note=why, surface="agent")
    except ReviewError as exc:
        raise AutoRefused(str(exc)) from exc
    if not recorded["accepted"]:
        raise AutoRefused(f"{sid}: not recorded: {recorded['reason']}")
    result = Executor(store, run).execute(sid)
    if result["state"] == "applied":
        store.settle(sid, rule, f"{option['id']}: {option['label']} -> {result['message']}", expect=("applied",))
        result = {**result, "state": "settled"}
    return result


def handle(store: ReviewStore, entry: dict[str, Any], run: Runner | None = None) -> dict[str, Any]:
    """Apply one ``{"short_id", "do", "option"?, "why", "rule"?}`` entry. Raises AutoRefused when nothing was
    written. A settled apply or resolve appends its rule and returns its name as ``rule_learned``."""
    verb = str(entry.get("do") or "").strip()
    if verb not in VERBS:
        raise AutoRefused(f"do must be one of {', '.join(VERBS)}")
    _suspended(store)
    item = _auto_item(store, str(entry.get("short_id") or "").upper())
    why = _why(str(entry.get("why") or ""))
    sid = item["short_id"]
    if verb == "return":
        shown = (item.get("version") or {}).get("n", 0)
        if not store.set_status(sid, "open", expect=("auto",)):
            raise AutoRefused(f"{sid} changed; not returned")
        store.log_action(None, sid, "auto_return", {"version": shown}, "open", receipt=why[:2000])
        return {"short_id": sid, "state": "open", "message": f"returned to the user: {why}"}
    option = None if verb == "resolve" else _option(item, str(entry.get("option") or "").strip().upper())
    spec = _rule(item, option, verb, entry)
    if verb == "resolve":
        if not store.settle(sid, RULE, f"resolved without an option: {why}", expect=("auto",)):
            raise AutoRefused(f"{sid} changed; not resolved")
        result = {"short_id": sid, "state": "settled", "message": f"resolved: {why}"}
    else:
        result = _apply_option(store, item, option, why, RULE, run)
        if result["state"] == "stale":
            # The source changed under the agent: nothing was applied. The item stays with the agents, not the user.
            store.set_status(sid, "auto", expect=("open",))
    if result["state"] == "settled":
        result["rule_learned"] = review_learned.append(store, spec, sid) if spec else None
        if not spec:
            store.log_action(None, sid, "rule_skipped", {}, "recorded", receipt=str(entry["rule"]["skip"])[:2000])
    return result


def settle_learned(
    store: ReviewStore,
    short_id: str,
    rule: str,
    args: dict[str, Any],
    why: str,
    *,
    run: Runner | None = None,
    project: Any = None,
) -> dict[str, Any]:
    """Settle an open item under a learned rule. ``args`` is ``{"option": "B"}`` or ``{"resolve": true}``.

    The code checks that the rule exists, is active, is under its daily cap, and covers this exact step (signature
    and match); the model has checked the rule's ``when`` against the live source and says so in ``why``."""
    learned = review_learned.get(store, rule)
    if learned is None:
        raise AutoRefused(f"unknown rule '{rule}'")
    if learned["state"] != "active":
        raise AutoRefused(f"rule {rule} is suspended: {learned.get('state_reason') or ''}")
    if learned["uses_today"] >= review_learned.MAX_USES_PER_DAY:
        raise AutoRefused(f"rule {rule} settled {review_learned.MAX_USES_PER_DAY} items today; the rest wait")
    unknown = set(args) - {"option", "resolve"}
    if unknown or bool(args.get("resolve")) == bool(args.get("option")):
        raise AutoRefused('a learned rule takes args {"option": "<id>"} or {"resolve": true}')
    verb = "resolve" if args.get("resolve") else "apply"
    if verb != learned["do"]:
        raise AutoRefused(f"rule {rule} covers {learned['do']}, not {verb}")
    why = _why(why)
    item = store.get(short_id)
    if item is None:
        raise AutoRefused(f"unknown item {short_id}")
    sid = item["short_id"]
    if item["status"] != "open" or item["section"] != "gated":
        raise AutoRefused(f"{sid} is {item['status']}; only open items can be settled")
    if project is not None and item.get("project") not in (None, project.id):
        raise AutoRefused(f"{sid} belongs to project {item.get('project')}")
    if item["source"] == "learn" or item.get("urgent_rule"):
        raise AutoRefused(f"{sid} stays with the user")
    option = None if verb == "resolve" else _option(item, str(args.get("option") or "").strip().upper())
    if not review_learned.covers(learned, item, option, verb):
        got = review_learned.signature(item, option, verb)
        raise AutoRefused(f"rule {rule} does not cover {sid} ({got}; rule: {learned['signature']}, match {learned['match']})")
    reason = f"rule {rule} (from {learned['from_short_id']}): {why}"
    if verb == "resolve":
        receipt = f"resolved: {reason}"
        if not store.settle(sid, rule, receipt, expect=("open",)):
            raise AutoRefused(f"{sid} changed while settling; it is no longer open")
        return {"short_id": sid, "rule": rule, "receipt": receipt}
    result = _apply_option(store, item, option, reason, rule, run)
    if result["state"] != "settled":
        raise AutoRefused(f"{sid}: {result['state']}: {result.get('message', '')}")
    return {"short_id": sid, "rule": rule, "receipt": result.get("message", "")}
