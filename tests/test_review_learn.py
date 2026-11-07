"""Rule learning: repeated agreement proposes a rule; an overrule suspends one. Nothing here writes a rule."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest
from conftest import DECIDED, SUMMARY, cycle_md, write

from hermes_initiatives_core import review_exec, review_learn, review_settle, review_write, reviews
from hermes_initiatives_core.settings import load_settings

NOW = dt.datetime(2026, 10, 20, tzinfo=dt.UTC)


@pytest.fixture
def env(isolated_hermes, root):
    write(root / "in-demo" / "cycles" / "in-demo-c02.md", cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    write(root / "in-demo" / "cycles" / "in-demo-c03.md", cycle_md("in-demo-c03", stage="abandoned", decisions=DECIDED))
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "demo",
                        "label": "Demo",
                        "root": str(root),
                        "kanban_board": "default",
                        "intake_assignee": "orchestrator",
                    }
                ]
            }
        )
    )
    return {"store": reviews.ReviewStore(), "projects": load_settings()[0].projects, "root": root}


def _answered(store, n, *, source="idea", action="idea_status", args=None, pick="A", note="", key=None):
    args = {"status": "parked"} if args is None else args
    item, _ = store.upsert_item(
        key or f"{source}:demo:x{n}:triage",
        section="gated",
        source=source,
        title=f"Item {n}",
        basis={"n": n},
        project="demo",
    )
    store.add_version(
        item["short_id"],
        question="What now?",
        options=[
            {"id": "A", "label": "Do the usual", "action": action, "args": args},
            {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="ctx",
    )
    store.answer(item["short_id"], pick, note=note, surface="tab")
    return item["short_id"]


def test_three_silent_agreements_propose_one_rule_item_once(env):
    store = env["store"]
    ids = [_answered(store, n) for n in range(3)]
    assert [c["signature"] for c in review_learn.mine(store, now=NOW)] == ["idea:idea_status:parked"]
    filed = review_learn.propose(store, env["projects"], now=NOW)
    assert len(filed) == 1
    item = store.get(filed[0])
    assert item["source"] == "learn" and item["status"] == "open" and reviews.current(item)
    for sid in ids:
        assert sid in item["version"]["context"]
    plan = item["version"]["options"][0]
    assert plan["action"] == "kanban_create" and plan["args"]["assignee"] == "orchestrator"
    assert "The user merges the PR" in plan["args"]["body"]
    assert review_learn.propose(store, env["projects"], now=NOW) == [], "a signature is proposed once"
    store.close_item(filed[0], "applied")
    assert review_learn.propose(store, env["projects"], now=NOW) == [], "not even after the item closes"


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"note": "only this once"}, "a note means he thought about it"),
        ({"pick": "B"}, "he did not take the recommendation"),
    ],
)
def test_a_break_in_the_streak_proposes_nothing(env, overrides, why):
    store = env["store"]
    _answered(store, 0)
    _answered(store, 1, **overrides)
    _answered(store, 2)
    assert review_learn.mine(store, now=NOW) == [], why


@pytest.mark.parametrize(
    ("source", "action", "args"),
    [
        ("criteria", "record_decision", {"decision": "x"}),
        ("cycle", "set_cycle_fields", {"fields": {"stage": "decided"}}),
        ("kanban", "kanban_unblock", {"board": "default", "card": "t_00000001"}),
        ("idea", "idea_status", {"status": "promoted", "into": "in-demo-c02"}),
        ("idea", "idea_reject", {}),
        ("request:release", "record_decision", {"decision": "x"}),
    ],
)
def test_decisions_on_the_bar_are_never_proposed(env, source, action, args):
    store = env["store"]
    for n in range(4):
        _answered(store, n, source=source, action=action, args=args, key=f"{source}:{n}")
    assert review_learn.mine(store, now=NOW) == []


def test_overrule_suspends_the_rule_and_files_a_follow_up(env):
    store = env["store"]
    item, _ = store.upsert_item(
        "suggest:old", section="suggested", source="suggestion", title="Old", basis={"e": 1}, project="demo"
    )
    other, _ = store.upsert_item("suggest:new", section="suggested", source="suggestion", title="New", basis={"e": 2})
    review_settle.Settler(store, env["projects"][0]).settle(
        item["short_id"], "retire_suggestion", {"replaced_by": other["short_id"]}, "The newer suggestion carries the same ask."
    )
    with pytest.raises(reviews.ReviewError, match="few words"):
        review_learn.overrule(store, env["projects"], item["short_id"], "no")
    done = review_learn.overrule(store, env["projects"], item["short_id"], "The new one asks something else.")
    assert done["rule"] == "retire_suggestion"
    assert store.rule_states()["retire_suggestion"]["state"] == "suspended"
    assert store.get(item["short_id"])["status"] == "settled", "the record of what happened stays"
    follow = store.get(done["follow_up"])
    assert follow["source"] == "learn" and reviews.current(follow)
    assert [o["action"] for o in follow["version"]["options"]] == ["acknowledge", "rule_state", "kanban_create"]
    with pytest.raises(reviews.ReviewError, match="already overruled"):
        review_learn.overrule(store, env["projects"], item["short_id"], "The new one asks something else.")
    # Restoring is the user's answer on the follow-up item.
    result = review_exec.Executor(store).submit(done["follow_up"], "B", surface="tab")
    assert result["state"] == "applied", result
    assert store.rule_states()["retire_suggestion"]["state"] == "active"


def test_only_learn_items_may_change_a_rule_and_options_never_target_closed_cycles(env):
    store = env["store"]
    item, _ = store.upsert_item(
        "idea:demo:idea-x:triage", section="gated", source="idea", title="Idea", basis={"idea": "idea-x"}, project="demo"
    )
    promote = {"idea": "idea-x", "status": "promoted", "into": "in-demo-c03"}
    restore = {"rule": "repair_promotion", "state": "active"}
    bad = {
        "question": "Promote?",
        "summary": SUMMARY,
        "recommend": "A",
        "options": [
            {"id": "A", "label": "Into c03", "action": "idea_status", "args": promote},
            {"id": "B", "label": "Restore", "action": "rule_state", "args": restore},
        ],
    }
    result = review_write.apply(store, {"items": {item["short_id"]: bad}})
    text = " ".join(result["errors"])
    assert "in-demo-c03 is abandoned" in text and "rule_state is only for rule-learning items" in text
    bad["options"][0]["args"]["into"] = "in-demo-c02"
    bad["options"][1] = {"id": "B", "label": "Park", "action": "defer", "args": {}}
    assert review_write.apply(store, {"items": {item["short_id"]: bad}})["errors"] == []
