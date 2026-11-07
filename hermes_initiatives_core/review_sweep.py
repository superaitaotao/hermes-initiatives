"""Stale sweep: the review job re-checks review items nobody has looked at for 3 days (user decision 2026-11-01).

An item is stale when it is still with the user (open, watching, deferred, failed, or waiting for a confirm) and
nothing happened to it for ``STALE_DAYS``: no new options, answer, action, or deferral. The tick lists the oldest
few (``tick-in.json`` → ``stale``); for each, the agent re-reads its source and does one of four things, each
checked here before anything is written:

- ``keep``: the item still needs the user as it stands. Logged, so the item is checked again 3 days later.
- ``refresh``: the facts moved on (the plan is outdated, the card changed, the ask no longer fits). The item returns
  to ``open`` and the same tick writes new options. They reach the user in the daily digest marked "refreshed",
  without a Telegram push of their own (``ReviewStore.to_push``).
- ``resolve``: nothing is left to decide (the card finished, the decision is already recorded). Settled under
  ``stale_sweep``.
- ``apply``: answer an open item with its recommended option, only when the agent classes the step low or medium
  risk. Settled under ``stale_sweep``; the decision row says so. Irreversible options, steps only the user does
  (``attend``), parking options, merges, rule changes, rule-learning and urgent items, and the deep-tier merge gate
  stay with the user.

The user overrules a settlement like any other (``daily_review.py overrule``), which suspends ``resolve`` and
``apply`` until he restores the rule. ``keep`` and ``refresh`` decide nothing and keep working.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .review_auto import NEVER, _apply_option
from .review_exec import UNSUPPORTED, Runner
from .reviews import ReviewStore, current

RULE = "stale_sweep"
DELEGATION = "user delegation 2026-11-01"
STALE_DAYS = 3
MAX_LISTED = 8  # per tick, oldest first; the rest wait for the next tick
MAX_APPLIES_PER_DAY = 5
VERBS = ("keep", "refresh", "resolve", "apply")
STALE_STATUSES = ("open", "watching", "deferred", "failed", "awaiting_confirm")
RISKS = ("low", "medium")
PARKING = ("defer", "acknowledge")
DEEP_MERGE = re.compile(r"NEEDS YOUR MERGE APPROVAL|REVIEW PASSED\s*[—-]+\s*DEEP", re.IGNORECASE)


class SweepRefused(ValueError):
    """The sweep does not cover this step; nothing was written."""


def listing(store: ReviewStore, now: dt.datetime | None = None, limit: int = MAX_LISTED) -> list[dict[str, Any]]:
    """The stale items the tick prints, oldest first, with what the agent needs to decide."""
    out = []
    triaged = {i["short_id"] for i in store.needs_options()}  # the tick prints these in ``items`` already
    for item in [i for i in store.stale_items(now=now) if i["short_id"] not in triaged][:limit]:
        version = item.get("version") or {}
        out.append(
            {
                "short_id": item["short_id"],
                "status": item["status"],
                "idle_since": item["last_touched"],
                "source": item["source"],
                "title": item["title"],
                "initiative": item["initiative"],
                "cycle": item["cycle"],
                "links": item["links"],
                "context": (item["context"] or "")[:1500],
                "watch_reason": item.get("watch_reason"),
                "deferral": item.get("deferral"),
                "options_current": current(item),
                "question": version.get("question"),
                "recommend": version.get("recommend"),
                "options": [
                    {k: o[k] for k in ("id", "label", "action", "args", "confirm")} for o in version.get("options") or []
                ],
            }
        )
    return out


def _why(why: Any) -> str:
    why = str(why or "").strip()
    if len(why) < 20:
        raise SweepRefused("why must say in a sentence what was re-read and what it showed")
    return why


def _suspended(store: ReviewStore) -> None:
    state = store.rule_states().get(RULE)
    if state and state["state"] == "suspended":
        raise SweepRefused(f"rule {RULE} is suspended since {state['at'][:10]}: {state['reason']}; keep or refresh it")


def _users_own(item: dict[str, Any]) -> None:
    sid = item["short_id"]
    if item["source"] == "learn" or item.get("urgent_rule"):
        raise SweepRefused(f"{sid} stays with the user (rule-learning or urgent item); keep or refresh it")
    version = item.get("version") or {}
    text = " ".join(str(t or "") for t in (item.get("context"), version.get("question"), version.get("context")))
    if DEEP_MERGE.search(text):
        raise SweepRefused(f"{sid} is the deep-tier merge gate; only the user approves a merge")


def _option(item: dict[str, Any], option_id: str) -> dict[str, Any]:
    sid = item["short_id"]
    if not current(item):
        raise SweepRefused(f"{sid} has no options for its current source; refresh it instead")
    version = item["version"]
    if option_id != version["recommend"]:
        raise SweepRefused(f"{sid}: only the recommended option ({version['recommend']}) may be applied; refresh it otherwise")
    option = next(o for o in version["options"] if o["id"] == option_id)
    if option.get("confirm"):
        raise SweepRefused(f"{sid} option {option_id} cannot be undone; it needs the user's confirm")
    if option["action"] in UNSUPPORTED or option["action"] in NEVER:
        raise SweepRefused(f"{sid} option {option_id} ({option['action']}) stays with the user")
    if option["action"] in PARKING:
        raise SweepRefused(f"{sid} option {option_id} only parks the item; keep it instead")
    return option


def handle(
    store: ReviewStore,
    entry: dict[str, Any],
    *,
    project: Any = None,
    run: Runner | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Apply one ``{"short_id", "do", "why", "option"?, "risk"?}`` entry. Raises SweepRefused when nothing was written."""
    verb = str(entry.get("do") or "").strip()
    if verb not in VERBS:
        raise SweepRefused(f"do must be one of {', '.join(VERBS)}")
    why = _why(entry.get("why"))
    sid = str(entry.get("short_id") or "").upper()
    item = store.get(sid)
    if item is None:
        raise SweepRefused(f"unknown item {sid or '(empty)'}")
    if project is not None and item.get("project") not in (None, project.id):
        raise SweepRefused(f"{sid} belongs to project {item.get('project')}")
    if sid not in {i["short_id"] for i in store.stale_items(now=now)}:
        raise SweepRefused(f"{sid} is {item['status']} and not idle for {STALE_DAYS} days; the sweep leaves it alone")
    shown = (item.get("version") or {}).get("n", 0)
    if verb == "keep":
        store.log_action(None, sid, "sweep_keep", {"version": shown}, item["status"], receipt=why[:2000])
        return {"short_id": sid, "state": item["status"], "message": f"kept: {why}"}
    if verb == "refresh":
        if item["status"] != "open" and not store.set_status(sid, "open", expect=STALE_STATUSES):
            raise SweepRefused(f"{sid} changed; not refreshed")
        store.log_action(None, sid, "sweep_refresh", {"version": shown, "from": item["status"]}, "open", receipt=why[:2000])
        return {"short_id": sid, "state": "open", "message": f"refreshed: new options at this tick; {why}"}
    _suspended(store)
    _users_own(item)
    if verb == "resolve":
        if not store.settle(sid, RULE, f"resolved: {why}", expect=STALE_STATUSES):
            raise SweepRefused(f"{sid} changed; not resolved")
        return {"short_id": sid, "state": "settled", "message": f"resolved: {why}"}
    if item["status"] != "open":
        raise SweepRefused(f"{sid} is {item['status']}; only open items can be applied (refresh it first)")
    risk = str(entry.get("risk") or "").strip().lower()
    if risk not in RISKS:
        raise SweepRefused(f"risk must be low or medium (the project policy risk rubric), got {risk or '(none)'}")
    option = _option(item, str(entry.get("option") or "").strip().upper())
    if _applies_today(store) >= MAX_APPLIES_PER_DAY:
        raise SweepRefused(f"the sweep applied {MAX_APPLIES_PER_DAY} options today; the rest wait")
    note = f"rule {RULE} ({DELEGATION}, risk {risk}): {why}"
    result = _apply_option(store, item, option, note, RULE, run)
    if result["state"] != "settled":
        raise SweepRefused(f"{sid}: {result['state']}: {result.get('message', '')}")
    return result


def _applies_today(store: ReviewStore) -> int:
    start = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return sum(
        1
        for a in store.actions_since(start)
        if a["action"] == f"settle:{RULE}" and not str(a.get("receipt") or "").startswith("resolved:")
    )
