"""Read-only hints for bounded routine triage; settlement checks remain authoritative."""

from __future__ import annotations

from typing import Any

from .reviews import ReviewStore, current


def candidates(store: ReviewStore) -> list[dict[str, Any]]:
    """List current outcome options worth checking against delegated rules."""
    found = []
    states = store.rule_states()
    for item in store.items(statuses=("open",), limit=5000):
        basis = item.get("basis")
        if item["source"] != "cycle" or not isinstance(basis, dict) or basis.get("reasons") != ["outcome_review_ready"]:
            # A basis that is not a mapping cannot prove the ready review; skip it instead of raising AttributeError.
            continue
        if (
            not current(item)
            or item.get("urgent_rule")
            or item.get("owner_profile")
            or item.get("owner_job")
            or item.get("blocks")
            or store.answers(item["short_id"])
        ):
            continue
        version = item.get("version") or {}
        option = next((o for o in version.get("options", []) if o.get("id") == version.get("recommend")), {})
        if option.get("confirm"):
            continue
        action = option.get("action")
        fields = (option.get("args") or {}).get("fields") or {}
        if action == "kanban_create":
            rule = "plan_outcome_followup"
        elif action == "set_cycle_fields" and "measurement" in fields:
            rule = "normalize_outcome_contract"
        else:
            continue
        state = states.get(rule) or {}
        if state.get("state") == "suspended":
            continue
        found.append(
            {
                "short_id": item["short_id"],
                "rule": rule,
                "project": item.get("project"),
                "initiative": item.get("initiative"),
                "cycle": item.get("cycle"),
                "basis": item["basis"],
                "context": item.get("context"),
                "previous": version,
                "instruction": "Read live sources and prove the delegated rule; this hint is not permission to apply the option.",
            }
        )
    return found
