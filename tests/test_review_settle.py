"""Agent settlement: delegated rules close review items, with an audit row; anything outside a rule is refused.

Kanban writes run the real ``hermes kanban`` CLI against the isolated test board (conftest pins HERMES_HOME and
HERMES_KANBAN_HOME under tmp_path).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import DECIDED, cycle_md, write

from hermes_initiatives_core import review_collect, review_settle, reviews
from hermes_initiatives_core.docs import parse_markdown
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

WHY = "It repairs the approved no-logged-inputs rule recorded on 2026-10-12."

IDEA = """
---
id: {id}
title: Fix a defect in approved work
status: proposed
size: {size}
related_initiative: in-demo
problem: The shipped code breaks an approved rule.
expected_outcome: The rule holds.
evidence:
- notes/audit.md
source: default/manual
similar_to: []
created: '2026-10-13'
promoted_to: null
merged_into: null
review_on: null
reason: null
---
Body kept.
"""


@pytest.fixture
def env(isolated_hermes, root):
    kbc.init_db(board="default")
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="exploring", hypothesis="H"))
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
                        "plan_assignee": "planner",
                        "plan_skills": ["plan"],
                    }
                ]
            }
        )
    )
    store = reviews.ReviewStore()
    project = load_settings()[0].projects[0]
    return {"store": store, "root": root, "project": project}


def _sync(env):
    review_collect.sync(env["store"], [env["project"]], gates={"default": {}})


def _idea(env, idea_id, size="task"):
    write(env["root"] / "ideas" / f"{idea_id}.md", IDEA.format(id=idea_id, size=size))
    _sync(env)
    return env["store"].get_by_key(f"idea:demo:{idea_id}:triage")["short_id"]


def _fm(path):
    return parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter


def _tasks():
    conn = kbc.connect(board="default")
    try:
        return {t.id: t for t in kb.list_tasks(conn)}
    finally:
        conn.close()


def test_repair_promotion_promotes_into_the_decided_cycle_and_audits(env):
    idea = "idea-20261013-fix-the-logging"
    sid = _idea(env, idea)
    settler = review_settle.Settler(env["store"], env["project"])
    done = settler.settle(sid, "repair_promotion", {"into": "in-demo-c02", "repairs_decision": 0}, WHY)
    assert "in-demo-c02" in done["receipt"]
    idea_fm = _fm(env["root"] / "ideas" / f"{idea}.md")
    assert idea_fm["status"] == "promoted" and idea_fm["promoted_to"] == "in-demo-c02"
    row = _fm(env["root"] / "in-demo" / "cycles" / "in-demo-c02.md")["decisions"][-1]
    assert row["by"] == "agent" and row["rule"] == "repair_promotion" and row["review"] == sid and row["idea"] == idea
    item = env["store"].get(sid)
    assert item["status"] == "settled" and env["store"].answers(sid) == [], "no answer is recorded in the user's name"
    assert [a["action"] for a in env["store"].actions(sid)] == ["settle:repair_promotion"]
    report = validate(load(env["root"]), check_sources=False)
    assert not report.errors.get("in-demo-c02") and not report.errors.get(idea)
    _sync(env)
    assert env["store"].get_by_key(f"idea:demo:{idea}:triage") is None, "a settled idea does not return"


@pytest.mark.parametrize(
    ("size", "args", "message"),
    [
        ("task", {"into": "in-demo-c01", "repairs_decision": 0}, "exploring"),  # not yet approved: new scope
        ("task", {"into": "in-demo-c03", "repairs_decision": 0}, "abandoned"),  # stopped cycle is not revived
        ("task", {"into": "in-demo-c02", "repairs_decision": 5}, "repairs_decision"),  # must cite a real decision
        ("task", {"into": "in-demo-c02"}, "repairs_decision"),
        ("initiative", {"into": "in-demo-c02", "repairs_decision": 0}, "new scope"),
    ],
)
def test_repair_promotion_refuses_anything_outside_the_rule(env, size, args, message):
    idea = f"idea-20261013-out-of-rule-{size}"
    sid = _idea(env, idea, size=size)
    with pytest.raises(review_settle.SettleRefused, match=message):
        review_settle.Settler(env["store"], env["project"]).settle(sid, "repair_promotion", args, WHY)
    assert _fm(env["root"] / "ideas" / f"{idea}.md")["status"] == "proposed", "a refusal writes nothing"
    assert env["store"].get(sid)["status"] == "open"
    assert len(_fm(env["root"] / "in-demo" / "cycles" / "in-demo-c02.md")["decisions"]) == 1


def test_settle_refuses_unknown_rules_short_reasons_and_items_the_user_parked(env):
    sid = _idea(env, "idea-20261013-parked-by-user")
    settler = review_settle.Settler(env["store"], env["project"])
    args = {"into": "in-demo-c02", "repairs_decision": 0}
    with pytest.raises(review_settle.SettleRefused, match="unknown rule"):
        settler.settle(sid, "idea_reject", args, WHY)
    with pytest.raises(review_settle.SettleRefused, match="why"):
        settler.settle(sid, "repair_promotion", args, "fine")
    env["store"].defer(sid, kind="state_change", until=None, reason="user parked it")
    with pytest.raises(review_settle.SettleRefused, match="deferred"):
        settler.settle(sid, "repair_promotion", args, WHY)


def test_plan_exploring_cycle_creates_one_linked_plan_card_and_returns_when_done(env):
    _sync(env)
    key = "cycle:demo:in-demo-c01:gate"
    sid = env["store"].get_by_key(key)["short_id"]
    before = set(_tasks())
    done = review_settle.Settler(env["store"], env["project"]).settle(
        sid, "plan_exploring_cycle", {}, "The cycle's next step is a plan; no credential or production change is named."
    )
    created = set(_tasks()) - before
    assert len(created) == 1
    card = created.pop()
    task = _tasks()[card]
    assert task.assignee == "planner" and "- Initiative cycle: in-demo-c01" in task.body
    assert "Do not implement" in task.body and card in done["receipt"]
    cycle = _fm(env["root"] / "in-demo" / "cycles" / "in-demo-c01.md")
    assert cycle["stage"] == "exploring", "starting a plan does not start the cycle"
    assert cycle["kanban_cards"] == [card]
    assert cycle["decisions"][-1]["by"] == "agent" and cycle["decisions"][-1]["card"] == card
    assert not validate(load(env["root"]), check_sources=False).errors.get("in-demo-c01")
    # While the plan card is open, the cycle is not a decision for the user.
    _sync(env)
    assert env["store"].get_by_key(key) is None
    # When the plan card is done, the cycle comes back to the user as plan_ready.
    conn = kbc.connect(board="default")
    kb.complete_task(conn, card, result="plan at tasks/plan.md")
    conn.close()
    _sync(env)
    back = env["store"].get_by_key(key)
    assert back["status"] == "open" and "plan_ready" in back["basis"]["reasons"]
    with pytest.raises(review_settle.SettleRefused, match="ready plan"):
        review_settle.Settler(env["store"], env["project"]).settle(back["short_id"], "plan_exploring_cycle", {}, WHY)


def test_plan_starts_are_capped_per_day(env, monkeypatch):
    monkeypatch.setattr(review_settle, "PLAN_STARTS_PER_DAY", 0)
    _sync(env)
    sid = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    before = set(_tasks())
    with pytest.raises(review_settle.SettleRefused, match="last 24 hours"):
        review_settle.Settler(env["store"], env["project"]).settle(sid, "plan_exploring_cycle", {}, WHY)
    assert set(_tasks()) == before


def test_retire_suggestion_needs_a_replacing_item(env):
    _sync(env)
    gate = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")
    item, _ = env["store"].upsert_item(
        "suggest:old-ask", section="suggested", source="suggestion", title="Old ask", basis={"e": 1}, evidence_hash="h1"
    )
    settler = review_settle.Settler(env["store"], env["project"])
    with pytest.raises(review_settle.SettleRefused, match="replaced_by"):
        settler.settle(item["short_id"], "retire_suggestion", {"replaced_by": "D999"}, WHY)
    with pytest.raises(review_settle.SettleRefused, match="own suggestions"):
        settler.settle(gate["short_id"], "retire_suggestion", {"replaced_by": item["short_id"]}, WHY)
    settler.settle(item["short_id"], "retire_suggestion", {"replaced_by": gate["short_id"]}, WHY)
    assert env["store"].get(item["short_id"])["status"] == "settled"
    assert env["store"].is_dismissed("suggest:old-ask", "h1"), "the same suggestion is not filed again"


def test_reopen_returns_a_met_deferral_and_asks_for_fresh_options(env):
    _sync(env)
    sid = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    env["store"].add_version(
        sid,
        question="Start c01?",
        options=[
            {"id": "A", "label": "Start", "action": "record_decision", "args": {"decision": "Start"}},
            {"id": "B", "label": "Park", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="ctx",
    )
    env["store"].defer(sid, kind="state_change", until=None, reason="Park c01 until PR #341 merges")
    _sync(env)
    assert env["store"].get(sid)["status"] == "deferred", "an unchanged source does not reopen it"
    with pytest.raises(reviews.ReviewError, match="reason"):
        review_settle.reopen_deferral(env["store"], sid, "merged")
    review_settle.reopen_deferral(env["store"], sid, "Condition 'until PR #341 merges' met: #341 merged 2026-10-12")
    assert env["store"].get(sid)["status"] == "open"
    assert sid in {i["short_id"] for i in env["store"].needs_options()}, "the next tick writes options that see it"
    env["store"].add_version(
        sid,
        question="Start c01 now that #341 merged?",
        options=[
            {"id": "A", "label": "Start", "action": "record_decision", "args": {"decision": "Start"}},
            {"id": "B", "label": "Park", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="ctx",
    )
    assert sid not in {i["short_id"] for i in env["store"].needs_options()}, "fresh options end the reopen"


def _request(env, key="req:release:path", opened="2026-10-13T10:00:00+00:00", **extra):
    item, _ = env["store"].upsert_item(
        key,
        section="gated",
        source="request:release",
        title="Release path for the backlog",
        basis={"ask": key},
        opened_at=opened,
        project="demo",
        evidence_hash="ev1",
        **extra,
    )
    return item["short_id"]


def test_withdraw_answered_closes_a_request_an_earlier_user_decision_answers(env):
    sid = _request(env)
    done = review_settle.Settler(env["store"], env["project"]).settle(
        sid,
        "withdraw_answered",
        {"in": "in-demo-c02", "decision": 0},
        "The 2026-09-01 decision in c02 already says run it.",
    )
    assert "in-demo-c02" in done["receipt"] and "Run it" in done["receipt"]
    assert env["store"].get(sid)["status"] == "settled" and env["store"].answers(sid) == []
    row = _fm(env["root"] / "in-demo" / "cycles" / "in-demo-c02.md")["decisions"][-1]
    assert row["by"] == "agent" and row["rule"] == "withdraw_answered" and row["review"] == sid
    assert not validate(load(env["root"]), check_sources=False).errors.get("in-demo-c02")
    assert env["store"].is_dismissed("req:release:path", "ev1"), "the same request is not filed again"


@pytest.mark.parametrize(
    ("setup", "args", "message"),
    [
        ({}, {"in": "in-demo-c02", "decision": 3}, "decision must index"),
        ({}, {"in": "in-demo-c99", "decision": 0}, "existing cycle or initiative"),
        ({"opened": "2026-08-01T00:00:00+00:00"}, {"in": "in-demo-c02", "decision": 0}, "newer than the request"),
        ({"urgent_rule": "security_consent"}, {"in": "in-demo-c02", "decision": 0}, "urgent"),
    ],
)
def test_withdraw_answered_refuses_anything_outside_the_rule(env, setup, args, message):
    sid = _request(env, **setup)
    with pytest.raises(review_settle.SettleRefused, match=message):
        review_settle.Settler(env["store"], env["project"]).settle(sid, "withdraw_answered", args, WHY)
    assert env["store"].get(sid)["status"] == "open"
    assert len(_fm(env["root"] / "in-demo" / "cycles" / "in-demo-c02.md")["decisions"]) == 1, "a refusal writes nothing"


def test_withdraw_answered_never_closes_collected_gates(env):
    sid = _idea(env, "idea-20261013-a-real-gate")
    with pytest.raises(review_settle.SettleRefused, match="agent-filed requests"):
        review_settle.Settler(env["store"], env["project"]).settle(
            sid, "withdraw_answered", {"in": "in-demo-c02", "decision": 0}, WHY
        )


def test_a_suspended_rule_refuses_until_restored(env):
    sid = _request(env)
    env["store"].set_rule_state("withdraw_answered", "suspended", "D9 overruled: it was not answered")
    settler = review_settle.Settler(env["store"], env["project"])
    args = {"in": "in-demo-c02", "decision": 0}
    with pytest.raises(review_settle.SettleRefused, match="suspended"):
        settler.settle(sid, "withdraw_answered", args, WHY)
    assert env["store"].get(sid)["status"] == "open"
    env["store"].set_rule_state("withdraw_answered", "active", "restored by the user")
    settler.settle(sid, "withdraw_answered", args, WHY)
    assert env["store"].get(sid)["status"] == "settled"


@pytest.fixture
def wait(env):
    import hashlib

    target = env["root"] / "in-demo" / "cycles" / "in-demo-c01.md"
    prerequisite = {"kind": "cycle_stage", "cycle": "in-demo-c02", "equals": "measuring"}
    row = {
        "date": "2026-10-14", "by": "user",
        "decision": "Park the review of in-demo-c01 until in-demo-c02 reaches measuring.",
        "review_deferral": {
            "schema_version": 1, "rule": "defer_exploring_cycle", "purpose": "routine_prerequisite_wait",
            "cycle": "in-demo-c01", "prerequisite": prerequisite,
        },
    }
    write(target, cycle_md("in-demo-c01", decisions=[row]))
    (env["root"] / ".review-write.lock").touch()
    _sync(env)
    sid = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    options = [
        {"id": "A", "label": "Park the review", "action": "defer",
         "args": {"kind": "state_change", "prerequisite": prerequisite}},
        {"id": "B", "label": "Keep the review parked", "action": "defer",
         "args": {"kind": "state_change", "prerequisite": prerequisite}},
    ]
    env["store"].add_version(sid, question="Park the review?", options=options, recommend="A", context="Routine wait.")
    args = {
        "authority_decision": 0,
        "authority_sha256": hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest(),
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    return {**env, "sid": sid, "args": args, "target": target, "row": row, "prerequisite": prerequisite}


def _park(wait):
    return review_settle.Settler(wait["store"], wait["project"]).settle(
        wait["sid"], "defer_exploring_cycle", wait["args"], "The latest user row authorizes this routine prerequisite wait."
    )


def test_defer_exploring_cycle_parks_with_agent_audit_without_answer(wait):
    before = parse_markdown(wait["target"].read_text(), wait["target"])
    done = _park(wait)
    after = parse_markdown(wait["target"].read_text(), wait["target"])
    assert done["status"] == "deferred" and done["receipt"]["verified"] is True
    audit = after.frontmatter["decisions"].pop()
    assert after.frontmatter == before.frontmatter and after.body == before.body
    assert audit["by"] == "agent" and audit["rule"] == "defer_exploring_cycle"
    assert audit["operation"] == done["operation"] and audit["review"] == wait["sid"]
    item = wait["store"].get(wait["sid"])
    assert item["status"] == "deferred" and item["deferral"]["kind"] == "state_change"
    assert item["deferral"]["until"] is None and wait["store"].answers(wait["sid"]) == []
    actions = wait["store"].actions(wait["sid"])
    assert [(a["action"], a["answer_id"], a["state"]) for a in actions] == [
        ("settle:defer_exploring_cycle", None, "deferred")
    ]
    assert not wait["store"].is_dismissed(item["key"], item["basis_hash"])


def _snapshot(wait):
    import sqlite3

    with sqlite3.connect(wait["store"].path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        rows = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
    files = {str(p.relative_to(wait["root"])): p.read_bytes() for p in wait["root"].rglob("*") if p.is_file()}
    return rows, files


@pytest.mark.parametrize("fault", ["audit_write", "after_audit", "final_commit", "after_commit"])
def test_defer_exploring_cycle_recovers_partial_write_once(wait, monkeypatch, fault):
    from hermes_initiatives_core import review_deferral
    from hermes_initiatives_core.review_exec import ActionFailed

    original = review_deferral.atomic_write
    def broken(path, text):
        if fault == "audit_write":
            raise OSError("injected atomic write failure")
        original(path, text)
        raise OSError("injected after audit failure")
    if fault in ("final_commit", "after_commit"):
        original_verify = review_deferral._verify
        calls = []
        def broken_verify(*args):
            original_verify(*args)
            calls.append(True)
            if fault == "final_commit" or len(calls) == 2:
                raise OSError("injected final readback failure")
        monkeypatch.setattr(review_deferral, "_verify", broken_verify)
    else:
        monkeypatch.setattr(review_deferral, "atomic_write", broken)
    with pytest.raises(ActionFailed, match="operation .* failed"):
        _park(wait)
    expected = "deferred" if fault == "after_commit" else "applying"
    assert wait["store"].get(wait["sid"])["status"] == expected
    if fault != "after_commit":
        assert wait["store"].answer(wait["sid"], "A", surface="tab")["accepted"] is False
    _sync(wait)
    assert wait["store"].get(wait["sid"])["status"] == expected
    monkeypatch.setattr(review_deferral, "atomic_write", original)
    if fault in ("final_commit", "after_commit"):
        monkeypatch.setattr(review_deferral, "_verify", original_verify)
    done = _park(wait)
    assert done["status"] == ("already_deferred" if fault == "after_commit" else "deferred")
    snapshot = _snapshot(wait)
    assert _park(wait)["status"] == "already_deferred"
    assert _snapshot(wait) == snapshot
    assert len([r for r in _fm(wait["target"])["decisions"] if r.get("rule") == "defer_exploring_cycle"]) == 1
    assert len(wait["store"].actions(wait["sid"])) == 1


def test_defer_exploring_cycle_same_basis_and_changed_basis_collection(wait):
    _park(wait)
    _sync(wait)
    assert wait["store"].get(wait["sid"])["status"] == "deferred"
    fm = _fm(wait["target"])
    fm["stage"] = "decided"
    write(wait["target"], cycle_md("in-demo-c01", **{k: v for k, v in fm.items() if k != "id"}))
    _sync(wait)
    item = wait["store"].get_by_key("cycle:demo:in-demo-c01:gate")
    assert (item["short_id"], item["status"]) == (wait["sid"], "open")


def test_defer_exploring_cycle_verified_completion_and_explicit_reopen(wait):
    _park(wait)
    with pytest.raises(reviews.ReviewError, match="prerequisite"):
        review_settle.reopen_deferral(wait["store"], wait["sid"], "The prerequisite supposedly completed.", wait["project"])
    write(wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md",
          cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    done = review_settle.reopen_deferral(wait["store"], wait["sid"], "The prerequisite reached measuring.", wait["project"])
    assert done["status"] == "open"
    assert wait["sid"] in {i["short_id"] for i in wait["store"].needs_options()}
    with pytest.raises(reviews.ReviewError, match="not deferred"):
        review_settle.reopen_deferral(wait["store"], wait["sid"], "The prerequisite reached measuring.", wait["project"])


def test_defer_exploring_cycle_suspension_and_overrule(wait):
    from hermes_initiatives_core import review_learn

    wait["store"].set_rule_state("defer_exploring_cycle", "suspended", "User suspended this rule")
    snapshot = _snapshot(wait)
    with pytest.raises(review_settle.SettleRefused, match="suspended"):
        _park(wait)
    assert _snapshot(wait) == snapshot
    wait["store"].set_rule_state("defer_exploring_cycle", "active", "User restored this rule")
    done = _park(wait)
    result = review_learn.overrule(wait["store"], [wait["project"]], wait["sid"], "This wait was not appropriate.")
    assert result["rule"] == "defer_exploring_cycle"
    follow_up = wait["store"].get(result["follow_up"])
    assert follow_up["source"] == "learn" and follow_up["version"]["options"][1]["args"] == {
        "rule": "defer_exploring_cycle", "state": "active"
    }
    assert wait["store"].rule_states()["defer_exploring_cycle"]["state"] == "suspended"
    assert wait["store"].get(wait["sid"])["status"] == "deferred"
    assert wait["store"].actions(wait["sid"])[-1]["args_json"] == json.dumps(
        {"rule": "defer_exploring_cycle", "operation": done["operation"]}
    )
    with pytest.raises(reviews.ReviewError, match="already overruled"):
        review_learn.overrule(wait["store"], [wait["project"]], wait["sid"], "This wait was not appropriate.")


@pytest.mark.parametrize("case", [
    "args_extra", "bool_index", "missing_authority", "source_hash", "authority_hash", "project", "key", "ownership",
    "section", "source", "basis", "mixed_reasons", "urgent", "answers", "stale_options", "missing_options",
    "missing_lock", "paused", "linked_cards", "derived_cards", "result", "disposition", "later_target", "later_initiative",
    "missing_instruction", "unknown_instruction", "boolean_version", "wrong_purpose", "self", "missing_cycle",
    "cross_project", "cyclic", "abandoned", "already_met", "no_prerequisite_decision", "date", "indefinite",
    "plan approval", "new scope", "start", "abandon", "success criteria", "verdict", "rank", "rejection", "release",
    "UI gate", "public cutover", "merge", "review verdict", "policy", "waiver", "permission", "security",
    "credentials", "secrets", "privacy", "data consent", "product outcome", "priority", "conflicting decisions",
])
def test_defer_exploring_cycle_refuses_without_writes(wait, case):
    import hashlib

    store, sid = wait["store"], wait["sid"]
    fm = _fm(wait["target"])
    row = fm["decisions"][0]
    pre = row["review_deferral"]["prerequisite"]
    prerequisite_path = wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md"
    if case == "args_extra":
        wait["args"]["until"] = "tomorrow"
    elif case == "bool_index":
        wait["args"]["authority_decision"] = False
    elif case in ("source_hash", "authority_hash"):
        wait["args"]["source_sha256" if case == "source_hash" else "authority_sha256"] = "0" * 64
    elif case == "missing_authority":
        wait["args"]["authority_decision"] = 99
    elif case in ("project", "key", "ownership", "section", "source", "urgent"):
        field = {"ownership": "initiative", "urgent": "urgent_rule"}.get(case, case)
        with store._connect() as conn:
            conn.execute(f"UPDATE items SET {field} = ? WHERE short_id = ?", ("wrong", sid))
    elif case in ("basis", "mixed_reasons", "stale_options"):
        basis = store.get(sid)["basis"]
        if case == "basis":
            basis["stage"] = "decided"
        else:
            basis["reasons"].append("plan_ready")
        store.refresh_basis(sid, basis)
    elif case == "answers":
        store.answer(sid, "A", surface="tab")
        store.set_status(sid, "open")
    elif case == "missing_options":
        with store._connect() as conn:
            conn.execute("DELETE FROM item_versions WHERE short_id = ?", (sid,))
    elif case == "missing_lock":
        (wait["root"] / ".review-write.lock").unlink()
    elif case in ("paused", "later_initiative"):
        path = wait["root"] / "in-demo" / "initiative.md"
        from hermes_initiatives_core.docs import dump_markdown
        doc = parse_markdown(path.read_text(), path)
        data = doc.frontmatter
        if case == "paused":
            data["status"] = "paused"
        else:
            data["decisions"].append({"date": "2026-10-15", "by": "user", "decision": "Changed instruction"})
        write(path, dump_markdown(data, doc.body))
    elif case == "linked_cards":
        fm["kanban_cards"] = ["t_abcdef12"]
    elif case == "derived_cards":
        conn = kbc.connect(board="default")
        kb.create_task(conn, title="Derived plan", assignee="planner", body="- Initiative cycle: in-demo-c01")
        conn.close()
    elif case == "result":
        fm["result"]["verdict"] = "win"
    elif case == "disposition":
        fm["disposition"]["action"] = "continue"
    elif case == "later_target":
        fm["decisions"].append({"date": "2026-10-15", "by": "user", "decision": "New instruction"})
    elif case == "missing_instruction":
        del row["review_deferral"]
    elif case == "unknown_instruction":
        row["review_deferral"]["extra"] = True
    elif case == "boolean_version":
        row["review_deferral"]["schema_version"] = True
    elif case == "wrong_purpose":
        row["review_deferral"]["purpose"] = "release_approval"
    elif case in ("self", "missing_cycle", "cross_project"):
        pre["cycle"] = {"self": "in-demo-c01", "missing_cycle": "in-demo-c99", "cross_project": "in-other-c01"}[case]
    elif case == "cyclic":
        dependency = dict(row)
        dependency["review_deferral"] = {**row["review_deferral"], "cycle": "in-demo-c02",
                                          "prerequisite": {**pre, "cycle": "in-demo-c01"}}
        write(prerequisite_path, cycle_md("in-demo-c02", stage="shipping", decisions=[dependency]))
    elif case in ("abandoned", "already_met", "no_prerequisite_decision"):
        write(prerequisite_path, cycle_md("in-demo-c02", stage={"abandoned": "abandoned", "already_met": "measuring"}.get(
            case, "shipping"), decisions=[] if case == "no_prerequisite_decision" else DECIDED))
    elif case in ("date", "indefinite"):
        pre["kind"] = case
    else:
        options = store.get(sid)["version"]["options"]
        options[1]["label"] = f"Approve {case}"
        store.add_version(sid, question="Park the review?", options=options, recommend="A", context="Routine wait.")
    if json.dumps(fm, sort_keys=True) != json.dumps(_fm(wait["target"]), sort_keys=True):
        write(wait["target"], cycle_md("in-demo-c01", **{k: v for k, v in fm.items() if k != "id"}))
        wait["args"]["source_sha256"] = hashlib.sha256(wait["target"].read_bytes()).hexdigest()
        wait["args"]["authority_sha256"] = hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    before = _snapshot(wait)
    with pytest.raises(review_settle.SettleRefused) as refused:
        _park(wait)
    assert str(refused.value)
    assert _snapshot(wait) == before


def test_defer_exploring_cycle_replay_and_concurrency(wait):
    import concurrent.futures
    import threading

    barrier = threading.Barrier(2)
    def worker():
        store = reviews.ReviewStore(wait["store"].path)
        barrier.wait(timeout=10)
        return _park({**wait, "store": store})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=20) for f in [pool.submit(worker), pool.submit(worker)]]
    assert sorted(r["status"] for r in results) == ["already_deferred", "deferred"]
    assert results[0]["operation"] == results[1]["operation"]
    assert len(wait["store"].actions(wait["sid"])) == 1


@pytest.mark.parametrize("race", ["answer", "options", "collect", "suspension"])
def test_defer_exploring_cycle_applying_blocks_competing_writes(wait, monkeypatch, race):
    import concurrent.futures
    import threading

    from hermes_initiatives_core import review_deferral

    ready, resume = threading.Event(), threading.Event()
    original = review_deferral.atomic_write
    def paused_write(path, text):
        ready.set()
        assert resume.wait(10), "race did not resume projection"
        original(path, text)
    monkeypatch.setattr(review_deferral, "atomic_write", paused_write)
    other = reviews.ReviewStore(wait["store"].path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(_park, wait)
        assert ready.wait(10), "operation never reached applying"
        if race == "answer":
            assert other.answer(wait["sid"], "A", surface="tab")["accepted"] is False
        elif race == "options":
            with pytest.raises(reviews.ReviewError, match="applying"):
                version = other.get(wait["sid"])["version"]
                other.add_version(wait["sid"], question="Changed options?", options=version["options"],
                                  recommend="B", context="Changed")
        elif race == "collect":
            before = other.get(wait["sid"])
            review_collect.sync(other, [wait["project"]], gates={"default": {}})
            assert other.get(wait["sid"]) == before
        else:
            other.set_rule_state("defer_exploring_cycle", "suspended", "User suspended during preparation")
        resume.set()
        done = pending.result(timeout=20)
    assert done["receipt"]["verified"] is True
    assert other.get(wait["sid"])["status"] == ("open" if race == "suspension" else "deferred")


@pytest.mark.parametrize("race", ["answer", "options", "collect", "suspension"])
def test_defer_exploring_cycle_competing_change_wins_before_preparation(wait, monkeypatch, race):
    from contextlib import contextmanager

    from hermes_initiatives_core import review_deferral

    original = review_deferral.root_lock
    other = reviews.ReviewStore(wait["store"].path)
    @contextmanager
    def competing_change(project):
        if race == "answer":
            other.answer(wait["sid"], "A", surface="tab")
        elif race == "options":
            v = other.get(wait["sid"])["version"]
            other.add_version(wait["sid"], question="Approve release?", options=v["options"], recommend="B", context="Release")
        elif race == "collect":
            fm = _fm(wait["target"])
            fm["stage"] = "decided"
            write(wait["target"], cycle_md("in-demo-c01", **{k: v for k, v in fm.items() if k != "id"}))
            review_collect.sync(other, [project], gates={"default": {}})
        else:
            other.set_rule_state("defer_exploring_cycle", "suspended", "Suspended before preparation")
        snapshot.append(_snapshot(wait))
        with original(project):
            yield
    snapshot = []
    monkeypatch.setattr(review_deferral, "root_lock", competing_change)
    with pytest.raises(review_settle.SettleRefused):
        _park(wait)
    assert _snapshot(wait) == snapshot[0]


@pytest.mark.parametrize("change", ["prerequisite_completed", "suspended", "external_edit"])
def test_defer_exploring_cycle_recovery_observes_newer_state(wait, monkeypatch, change):
    from hermes_initiatives_core import review_deferral
    from hermes_initiatives_core.review_exec import ActionFailed

    original = review_deferral.atomic_write
    def fail_after_write(path, text):
        original(path, text)
        raise OSError("interrupted after projection")
    monkeypatch.setattr(review_deferral, "atomic_write", fail_after_write)
    with pytest.raises(ActionFailed):
        _park(wait)
    monkeypatch.setattr(review_deferral, "atomic_write", original)
    if change == "prerequisite_completed":
        write(wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md",
              cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    elif change == "suspended":
        wait["store"].set_rule_state("defer_exploring_cycle", "suspended", "Suspended while interrupted")
    else:
        wait["target"].write_text(wait["target"].read_text() + "New user-owned body.\n")
        before = wait["target"].read_bytes()
        with pytest.raises(ActionFailed, match="newer source edits"):
            _park(wait)
        assert wait["target"].read_bytes() == before
        assert wait["store"].get(wait["sid"])["status"] == "applying"
        return
    done = _park(wait)
    assert done["status"] == "open" and done["receipt"]["verified"] is True
    assert [a["action"] for a in wait["store"].actions(wait["sid"])] == [
        "settle:defer_exploring_cycle", "reopen_deferral"
    ]


@pytest.mark.parametrize("location", ["question", "body", "next_action", "title", "earlier_decision", "unknown_field"])
def test_defer_exploring_cycle_requires_pending_structured_prerequisite(wait, location):
    import hashlib

    from hermes_initiatives_core.docs import dump_markdown

    doc = parse_markdown(wait["target"].read_text(), wait["target"])
    fm, body = doc.frontmatter, doc.body
    if location == "body":
        body = "Approve production cutover.\n"
    elif location == "earlier_decision":
        fm["decisions"].insert(0, {"date": "2026-10-13", "by": "user", "decision": "Approve the policy first."})
        wait["args"]["authority_decision"] = 1
    else:
        fm[location] = "Approve production cutover."
    write(wait["target"], dump_markdown(fm, body))
    wait["args"]["source_sha256"] = hashlib.sha256(wait["target"].read_bytes()).hexdigest()
    before = _snapshot(wait)
    with pytest.raises(review_settle.SettleRefused):
        _park(wait)
    assert _snapshot(wait) == before


@pytest.mark.parametrize("field", ["measurement", "result", "disposition"])
def test_defer_exploring_cycle_nested_bar_refuses_and_routine_control_parks(wait, field):
    import hashlib

    from hermes_initiatives_core.docs import dump_markdown

    doc = parse_markdown(wait["target"].read_text(), wait["target"])
    original = dict(doc.frontmatter[field])
    doc.frontmatter[field]["success_criteria"] = "Approve new success criteria before this cycle starts."
    write(wait["target"], dump_markdown(doc.frontmatter, doc.body))
    wait["args"]["source_sha256"] = hashlib.sha256(wait["target"].read_bytes()).hexdigest()
    before = _snapshot(wait)
    with pytest.raises(review_settle.SettleRefused, match="intent|review-bar"):
        _park(wait)
    assert _snapshot(wait) == before
    doc.frontmatter[field] = original
    write(wait["target"], dump_markdown(doc.frontmatter, doc.body))
    wait["args"]["source_sha256"] = hashlib.sha256(wait["target"].read_bytes()).hexdigest()
    assert _park(wait)["status"] == "deferred"


@pytest.mark.parametrize("phase", ["retry_before_audit", "retry_after_audit", "during_audit"])
def test_defer_exploring_cycle_recovery_rechecks_initiative_authority(wait, monkeypatch, phase):
    from hermes_initiatives_core import review_deferral
    from hermes_initiatives_core.docs import dump_markdown
    from hermes_initiatives_core.review_exec import ActionFailed

    path = wait["root"] / "in-demo" / "initiative.md"
    original_source = path.read_bytes()
    original_write = review_deferral.atomic_write
    def change_authority():
        doc = parse_markdown(path.read_text(), path)
        doc.frontmatter["decisions"].append({
            "date": "2026-10-15", "by": "user", "decision": "Do not park this cycle; review the new scope first."
        })
        write(path, dump_markdown(doc.frontmatter, doc.body))
    def interrupted(target, text):
        if phase != "retry_before_audit":
            original_write(target, text)
        if phase == "during_audit":
            change_authority()
        else:
            raise OSError("interrupted projection")
    monkeypatch.setattr(review_deferral, "atomic_write", interrupted)
    with pytest.raises(ActionFailed, match="operation .* failed"):
        _park(wait)
    monkeypatch.setattr(review_deferral, "atomic_write", original_write)
    if phase != "during_audit":
        change_authority()
    before = _snapshot(wait)
    with pytest.raises(ActionFailed, match="initiative.*authority"):
        _park(wait)
    assert _snapshot(wait)[1] == before[1]
    assert wait["store"].get(wait["sid"])["status"] == "applying"
    assert wait["store"].actions(wait["sid"]) == []
    with wait["store"]._connect() as conn:
        op = dict(conn.execute("SELECT * FROM agent_deferral_ops WHERE short_id = ?", (wait["sid"],)).fetchone())
    assert op["state"] == "failed" and "initiative" in op["error"]
    path.write_bytes(original_source)
    assert _park(wait)["status"] == "deferred"
    assert [a["action"] for a in wait["store"].actions(wait["sid"])] == ["settle:defer_exploring_cycle"]


@pytest.mark.parametrize("failure", ["missing_lock", "lock_io", "connection"])
def test_defer_exploring_cycle_prepared_retry_reports_operational_failure(wait, monkeypatch, failure):
    from contextlib import contextmanager

    from hermes_initiatives_core import review_deferral
    from hermes_initiatives_core.review_exec import ActionFailed

    original = review_deferral.atomic_write
    def interrupted(path, text):
        raise OSError("interrupted before audit")
    monkeypatch.setattr(review_deferral, "atomic_write", interrupted)
    with pytest.raises(ActionFailed):
        _park(wait)
    monkeypatch.setattr(review_deferral, "atomic_write", original)
    with wait["store"]._connect() as conn:
        operation = conn.execute("SELECT operation FROM agent_deferral_ops WHERE short_id = ?", (wait["sid"],)).fetchone()[0]
    with monkeypatch.context() as patcher:
        if failure == "missing_lock":
            (wait["root"] / ".review-write.lock").unlink()
        elif failure == "lock_io":
            @contextmanager
            def broken_lock(project):
                raise OSError("lock I/O failed")
                yield
            patcher.setattr(review_deferral, "root_lock", broken_lock)
        else:
            @contextmanager
            def broken_connection():
                raise OSError("connection failed")
                yield
            patcher.setattr(wait["store"], "_connect", broken_connection)
        with pytest.raises(ActionFailed, match=f"operation {operation} failed"):
            _park(wait)
    assert wait["store"].get(wait["sid"])["status"] == "applying"
    if failure == "missing_lock":
        (wait["root"] / ".review-write.lock").touch()
    assert _park(wait)["status"] == "deferred"
    assert len(wait["store"].actions(wait["sid"])) == 1


@pytest.mark.parametrize("change", ["completed", "suspended"])
def test_defer_exploring_cycle_completed_replay_requires_explicit_reopen(wait, change):
    _park(wait)
    if change == "completed":
        write(wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md",
              cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    else:
        wait["store"].set_rule_state("defer_exploring_cycle", "suspended", "User suspended the completed wait")
    before = _snapshot(wait)
    assert _park(wait)["status"] == "already_deferred"
    assert _snapshot(wait) == before
    assert wait["store"].get(wait["sid"])["status"] == "deferred"
    if change == "completed":
        assert review_settle.reopen_deferral(wait["store"], wait["sid"],
                                            "The prerequisite reached measuring.", wait["project"])["status"] == "open"


@pytest.mark.parametrize("call", ["reopen", "replay"])
def test_defer_exploring_cycle_completed_operation_ignores_later_initiative_decisions(wait, call):
    from hermes_initiatives_core.docs import dump_markdown

    _park(wait)
    path = wait["root"] / "in-demo" / "initiative.md"
    doc = parse_markdown(path.read_text(), path)
    doc.frontmatter["decisions"].append(
        {"date": "2026-10-15", "by": "user", "decision": "Confirmed: keep waiting for in-demo-c02."}
    )
    write(path, dump_markdown(doc.frontmatter, doc.body))
    if call == "replay":
        assert _park(wait)["status"] == "already_deferred"
        assert wait["store"].get(wait["sid"])["status"] == "deferred"
        return
    write(wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md",
          cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    done = review_settle.reopen_deferral(wait["store"], wait["sid"], "The prerequisite reached measuring.", wait["project"])
    assert done["status"] == "open"
    assert wait["sid"] in {i["short_id"] for i in wait["store"].needs_options()}
    assert [a["action"] for a in wait["store"].actions(wait["sid"])] == [
        "settle:defer_exploring_cycle", "reopen_deferral"
    ]


def test_defer_exploring_cycle_history_does_not_own_later_user_deferral(wait):
    _park(wait)
    write(wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md",
          cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    review_settle.reopen_deferral(wait["store"], wait["sid"], "The prerequisite reached measuring.", wait["project"])
    version = wait["store"].get(wait["sid"])["version"]
    wait["store"].add_version(wait["sid"], question="Wait longer?", options=version["options"],
                              recommend="A", context="An ordinary user wait.")
    wait["store"].defer(wait["sid"], kind="state_change", until=None, reason="User's separate ordinary wait")
    write(wait["root"] / "in-demo" / "cycles" / "in-demo-c02.md",
          cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    done = review_settle.reopen_deferral(wait["store"], wait["sid"], "The ordinary user wait condition is met.")
    assert done["status"] == "open"
    assert wait["sid"] in {i["short_id"] for i in wait["store"].needs_options()}
    assert [a["action"] for a in wait["store"].actions(wait["sid"])] == [
        "settle:defer_exploring_cycle", "reopen_deferral", "reopen_deferral"
    ]
    assert len([r for r in _fm(wait["target"])["decisions"] if r.get("by") == "agent"]) == 1


# --- defer_behind_focus_cycle: park a decided no-cards cycle behind the initiative's focus cycle ---------------

FOCUS_RULE = "defer_behind_focus_cycle"
FOCUS_WHY = "The initiative's focus cycle in-demo-c02 is still shipping, so this decided cycle waits its turn."


def _focus_set(env, cycle_id):
    """Set the initiative's focus_cycle: the one user-owned field this rule's wait is anchored on."""
    from hermes_initiatives_core.docs import dump_markdown

    path = env["root"] / "in-demo" / "initiative.md"
    doc = parse_markdown(path.read_text(encoding="utf-8"), path)
    doc.frontmatter["focus_cycle"] = cycle_id
    write(path, dump_markdown(doc.frontmatter, doc.body))


def _focus_rewrite(focus, cycle_id, **overrides):
    path = focus["root"] / "in-demo" / "cycles" / f"{cycle_id}.md"
    data = {k: v for k, v in _fm(path).items() if k != "id"}
    data.update(overrides)
    write(path, cycle_md(cycle_id, **data))


def _focus_options(focus, change=None, recommend=None):
    """Write a new version of the item's options with one field changed."""
    version = focus["store"].get(focus["sid"])["version"]
    options = json.loads(json.dumps(version["options"]))
    if change is not None:
        change(options)
    focus["store"].add_version(
        focus["sid"],
        question=version["question"],
        options=options,
        recommend=recommend or version["recommend"],
        context=version["context"],
    )


@pytest.fixture
def focus(env):
    """A decided cycle with no cards while another cycle of the same initiative is the focus cycle."""
    write(env["root"] / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="decided", decisions=DECIDED))
    write(env["root"] / "in-demo" / "cycles" / "in-demo-c02.md", cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    _focus_set(env, "in-demo-c02")
    _sync(env)
    sid = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    env["store"].add_version(
        sid,
        question="Start in-demo-c01 while in-demo-c02 is the focus cycle?",
        options=[
            {
                "id": "A",
                "label": "Start it now",
                "action": "set_cycle_fields",
                "args": {"cycle": "in-demo-c01", "fields": {"stage": "shipping"}},
            },
            {"id": "B", "label": "Keep it behind the focus cycle", "action": "defer", "args": {"kind": "state_change"}},
        ],
        recommend="B",
        context="The cycle is decided with no cards and its initiative already runs a focus cycle.",
    )
    return {**env, "sid": sid, "target": env["root"] / "in-demo" / "cycles" / "in-demo-c01.md"}


def _park_focus(focus, args=None, why=FOCUS_WHY):
    return review_settle.Settler(focus["store"], focus["project"]).settle(
        focus["sid"], FOCUS_RULE, args if args is not None else {}, why
    )


def test_defer_behind_focus_cycle_parks_with_agent_audit(focus):
    before = parse_markdown(focus["target"].read_text(), focus["target"])
    done = _park_focus(focus)
    after = parse_markdown(focus["target"].read_text(), focus["target"])
    assert done["status"] == "deferred" and done["rule"] == FOCUS_RULE, "the rule parks; it does not settle"
    assert done["receipt"]["focus_cycle"] == "in-demo-c02" and done["receipt"]["focus_stage"] == "shipping"
    audit = after.frontmatter["decisions"].pop()
    assert after.frontmatter == before.frontmatter and after.body == before.body
    assert audit["by"] == "agent" and audit["rule"] == FOCUS_RULE and audit["review"] == focus["sid"]
    assert audit["focus_cycle"] == "in-demo-c02" and audit["focus_stage"] == "shipping"
    assert audit["reason"] == FOCUS_WHY
    item = focus["store"].get(focus["sid"])
    assert item["status"] == "deferred" and item["deferral"]["kind"] == "state_change"
    assert item["deferral"]["until"] is None and focus["store"].answers(focus["sid"]) == []
    actions = focus["store"].actions(focus["sid"])
    assert [(a["action"], a["answer_id"], a["state"]) for a in actions] == [(f"settle:{FOCUS_RULE}", None, "deferred")]
    assert json.loads(actions[0]["receipt"])["deferral_id"] == done["receipt"]["deferral_id"]
    assert not focus["store"].is_dismissed(item["key"], item["basis_hash"]), "a park is not a settlement"
    errors = validate(load(focus["root"]), check_sources=False).errors
    assert not errors.get("in-demo-c01") and not errors.get("in-demo-c02"), errors


@pytest.mark.parametrize(
    "case",
    [
        "args_extra",
        "short_why",
        "suspended",
        "not_open",
        "other_project",
        "no_project",
        "wrong_section",
        "wrong_source",
        "wrong_key",
        "wrong_initiative",
        "unknown_cycle",
        "urgent",
        "owner_profile",
        "blocks",
        "mixed_reasons",
        "shipping_stage",
        "verdict_set",
        "disposition_set",
        "no_reason",
        "stale_options",
        "no_options",
        "recommend_start",
        "kind_date",
        "extra_until",
        "kanban_unblock",
        "attend",
        "prior_answer",
        "prior_answer_note",
        "paused_initiative",
        "cycle_next_action",
        "later_cycle_decision",
        "later_initiative_decision",
        "own_focus",
        "no_focus",
        "focus_measuring",
        "focus_release_verified",
        "focus_other_initiative",
    ],
)
def test_defer_behind_focus_cycle_refuses_without_writes(focus, case):
    import datetime as dt

    from hermes_initiatives_core.docs import dump_markdown

    store, sid, root = focus["store"], focus["sid"], focus["root"]
    cycle_path = root / "in-demo" / "cycles" / "in-demo-c01.md"
    initiative_path = root / "in-demo" / "initiative.md"
    args, why = {}, FOCUS_WHY

    def set_column(column, value):
        with store._connect() as conn:
            conn.execute(f"UPDATE items SET {column} = ? WHERE short_id = ?", (value, sid))

    def set_basis(**overrides):
        basis = store.get(sid)["basis"]
        basis.update(overrides)
        store.refresh_basis(sid, basis)

    def add_decision(path, day):
        doc = parse_markdown(path.read_text(encoding="utf-8"), path)
        doc.frontmatter["decisions"].append({"date": day, "by": "user", "decision": "Start it now, before the focus"})
        write(path, dump_markdown(doc.frontmatter, doc.body))

    if case == "args_extra":
        args, message = {"until": "2026-11-01"}, "takes no arguments"
    elif case == "short_why":
        why, message = "Too short.", "why must say"
    elif case == "suspended":
        store.set_rule_state(FOCUS_RULE, "suspended", "The user suspended this rule")
        message = "is suspended since"
    elif case == "not_open":
        store.set_status(sid, "watching")
        message = "only open items can be settled"
    elif case == "other_project":
        set_column("project", "other")
        message = "belongs to project other"
    elif case == "no_project":
        set_column("project", None)
        message = "has no project attribution"
    elif case == "wrong_section":
        set_column("section", "suggested")
        message = "only a gated cycle item"
    elif case == "wrong_source":
        set_column("source", "idea")
        message = "only a gated cycle item"
    elif case == "wrong_key":
        set_column("key", "cycle:demo:in-demo-c01:other")
        message = "the item key must be"
    elif case == "wrong_initiative":
        set_column("initiative", "in-other")
        message = "is not a cycle of initiative"
    elif case == "unknown_cycle":
        with store._connect() as conn:
            conn.execute("UPDATE items SET cycle = 'in-demo-c99', key = 'cycle:demo:in-demo-c99:gate' WHERE short_id = ?", (sid,))
        message = "is not a cycle of initiative"
    elif case == "urgent":
        set_column("urgent_rule", "security_consent")
        message = "carries urgency"
    elif case == "owner_profile":
        set_column("owner_profile", "coder")
        message = "carries urgency"
    elif case == "blocks":
        set_column("blocks_json", json.dumps(["t_abcdef12"]))
        message = "carries urgency"
    elif case == "mixed_reasons":
        set_basis(reasons=["no_cards", "evaluation_due"])
        message = "the basis must be a decided cycle"
    elif case == "shipping_stage":
        set_basis(stage="shipping")
        message = "the basis must be a decided cycle"
    elif case == "verdict_set":
        set_basis(verdict="win")
        message = "the basis must be a decided cycle"
    elif case == "disposition_set":
        set_basis(disposition="continue")
        message = "the basis must be a decided cycle"
    elif case == "no_reason":
        set_basis(reasons=[])
        message = "the basis must be a decided cycle"
    elif case == "stale_options":
        set_basis(cards_done=["t_abcdef12"])
        message = "options are not current"
    elif case == "no_options":
        with store._connect() as conn:
            conn.execute("DELETE FROM item_versions WHERE short_id = ?", (sid,))
        message = "options are not current"
    elif case == "recommend_start":
        _focus_options(focus, recommend="A")
        message = "recommended option must be the one defer"
    elif case == "kind_date":
        _focus_options(focus, change=lambda options: options[1]["args"].update(kind="date"))
        message = "recommended option must be the one defer"
    elif case == "extra_until":
        _focus_options(focus, change=lambda options: options[1]["args"].update(until="2026-11-01"))
        message = "recommended option must be the one defer"
    elif case == "kanban_unblock":
        _focus_options(focus, change=lambda options: options[0].update(action="kanban_unblock"))
        message = "carries a human gate"
    elif case == "attend":
        _focus_options(focus, change=lambda options: options[0].update(action="attend", args={"board": "default"}))
        message = "carries a human gate"
    elif case == "prior_answer":
        store.answer(sid, "A", surface="tab")
        store.set_status(sid, "open")
        message = "was not an empty defer"
    elif case == "prior_answer_note":
        store.answer(sid, "B", surface="tab", note="I thought about it for a while")
        store.set_status(sid, "open")
        message = "was not an empty defer"
    elif case == "paused_initiative":
        doc = parse_markdown(initiative_path.read_text(encoding="utf-8"), initiative_path)
        doc.frontmatter["status"] = "paused"
        write(initiative_path, dump_markdown(doc.frontmatter, doc.body))
        message = "only an active initiative"
    elif case == "cycle_next_action":
        _focus_rewrite(focus, "in-demo-c01", next_action="Link the Kanban cards that deliver this cycle")
        message = "already names a next action"
    elif case == "later_cycle_decision":
        add_decision(cycle_path, dt.date.today().isoformat())
        message = "on or after this item opened"
    elif case == "later_initiative_decision":
        add_decision(initiative_path, dt.date.today().isoformat())
        message = "on or after this item opened"
    elif case == "own_focus":
        _focus_set(focus, "in-demo-c01")
        message = "is this item's own cycle"
    elif case == "no_focus":
        _focus_set(focus, None)
        message = "no focus cycle set"
    elif case == "focus_measuring":
        _focus_rewrite(focus, "in-demo-c02", stage="measuring")
        message = "is measuring"
    elif case == "focus_release_verified":
        _focus_rewrite(focus, "in-demo-c02", stage="decided")
        write(
            root / "in-demo" / "observations" / "in-demo-c02.jsonl",
            json.dumps({"at": "2026-10-01T00:00:00Z", "by": "prod-operator", "kind": "release_verified", "evidence": "r.md"})
            + "\n",
        )
        message = "is measuring"
    else:
        write(
            root / "in-other" / "initiative.md",
            """
            ---
            id: in-other
            title: Other initiative
            status: active
            decisions:
              - {date: 2026-09-01, by: user, decision: Start other}
            ---
            Other brief.
        """,
        )
        write(root / "in-other" / "cycles" / "in-other-c01.md", cycle_md("in-other-c01", stage="shipping", decisions=DECIDED))
        _focus_set(focus, "in-other-c01")
        message = "is not a cycle of"

    before = _snapshot(focus)
    with pytest.raises(review_settle.SettleRefused, match=message) as refused:
        review_settle.Settler(store, focus["project"]).settle(sid, FOCUS_RULE, args, why)
    assert str(refused.value), "a refusal names the check that failed"
    assert _snapshot(focus) == before
    assert store.answers(sid) == [] or case in ("prior_answer", "prior_answer_note")


@pytest.mark.parametrize("move", ["stage_measuring", "verified_release", "focus_changed"])
def test_defer_behind_focus_cycle_returns_when_the_focus_moves(focus, move):
    _park_focus(focus)
    if move == "stage_measuring":
        _focus_rewrite(focus, "in-demo-c02", stage="measuring")
    elif move == "verified_release":
        _focus_rewrite(focus, "in-demo-c02", stage="decided")
        write(
            focus["root"] / "in-demo" / "observations" / "in-demo-c02.jsonl",
            json.dumps({"at": "2026-10-01T00:00:00Z", "by": "prod-operator", "kind": "release_verified", "evidence": "r.md"})
            + "\n",
        )
    else:
        write(
            focus["root"] / "in-demo" / "cycles" / "in-demo-c03.md", cycle_md("in-demo-c03", stage="decided", decisions=DECIDED)
        )
        _focus_set(focus, "in-demo-c03")
    _sync(focus)
    item = focus["store"].get(focus["sid"])
    assert item["status"] == "open", "the item comes back by itself when the focus cycle moves on"
    assert focus["sid"] in {i["short_id"] for i in focus["store"].needs_options()}
    actions = focus["store"].actions(focus["sid"])
    assert [a["action"] for a in actions] == [f"settle:{FOCUS_RULE}", "reopen_deferral"]
    assert json.loads(actions[0]["receipt"])["focus_cycle"] == "in-demo-c02"
    assert json.loads(actions[1]["args_json"])["version"] == 1
    assert len([r for r in _fm(focus["target"])["decisions"] if r.get("by") == "agent"]) == 1


def test_defer_behind_focus_cycle_return_ignores_other_deferrals(focus):
    _park_focus(focus)
    focus["store"].defer(focus["sid"], kind="state_change", until=None, reason="The user's own separate wait")
    _focus_rewrite(focus, "in-demo-c02", stage="measuring")
    _sync(focus)
    item = focus["store"].get(focus["sid"])
    assert item["status"] == "deferred", "a park the user made himself is not this rule's to return"
    assert item["deferral"]["reason"] == "The user's own separate wait"
    assert [a["action"] for a in focus["store"].actions(focus["sid"])] == [f"settle:{FOCUS_RULE}"]


def test_defer_behind_focus_cycle_overrule_suspends_and_reopens(focus):
    from hermes_initiatives_core import review_learn

    done = _park_focus(focus)
    result = review_learn.overrule(
        focus["store"], [focus["project"]], focus["sid"], "This cycle should not wait behind that focus cycle."
    )
    assert result["rule"] == FOCUS_RULE and "in-demo-c02" in result["receipt"]
    assert focus["store"].rule_states()[FOCUS_RULE]["state"] == "suspended"
    assert focus["store"].get(focus["sid"])["status"] == "open"
    assert focus["sid"] in {i["short_id"] for i in focus["store"].needs_options()}
    assert json.loads(focus["store"].actions(focus["sid"])[1]["args_json"]) == {
        "rule": FOCUS_RULE,
        "deferral_id": done["receipt"]["deferral_id"],
    }
    follow_up = focus["store"].get(result["follow_up"])
    assert follow_up["source"] == "learn"
    assert follow_up["version"]["options"][1]["args"] == {"rule": FOCUS_RULE, "state": "active"}
    with pytest.raises(reviews.ReviewError, match="already overruled"):
        review_learn.overrule(focus["store"], [focus["project"]], focus["sid"], "The same park overruled twice.")
    with pytest.raises(review_settle.SettleRefused, match="suspended"):
        _park_focus(focus)


@pytest.mark.parametrize("case", ["answer_during_claim", "audit_failure"])
def test_defer_behind_focus_cycle_claim_blocks_answers_and_failure_reopens(focus, monkeypatch, case):
    from hermes_initiatives_core.review_exec import ActionFailed

    other = reviews.ReviewStore(focus["store"].path)
    original = review_settle._append_row
    seen = {}

    def during_audit(project, path, row):
        current = other.get(focus["sid"])
        seen["status"] = current["status"]
        seen["answer"] = other.answer(focus["sid"], "B", surface="tab")
        if case == "audit_failure":
            raise OSError("injected audit write failure")
        return original(project, path, row)

    monkeypatch.setattr(review_settle, "_append_row", during_audit)
    if case == "answer_during_claim":
        done = _park_focus(focus)
        assert done["status"] == "deferred"
    else:
        with pytest.raises(ActionFailed, match="did not park"):
            _park_focus(focus)
        item = focus["store"].get(focus["sid"])
        assert item["status"] == "open", "a failed audit does not leave a silent park"
        actions = focus["store"].actions(focus["sid"])
        assert [(a["action"], a["state"]) for a in actions] == [(f"settle:{FOCUS_RULE}", "failed")]
        assert "injected audit write failure" in actions[0]["error"]
        with focus["store"]._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM deferrals WHERE short_id = ?", (focus["sid"],)).fetchone()[0] == 0
        assert [r for r in _fm(focus["target"])["decisions"] if r.get("by") == "agent"] == []
    assert seen["status"] == "applying", "the claim is taken before the audit row is written"
    assert seen["answer"]["accepted"] is False, "a claimed item takes no answer"
    assert "applying" in seen["answer"]["reason"]
    assert focus["store"].answers(focus["sid"]) == []


def test_defer_behind_focus_cycle_overrule_returns_the_newest_park(wait):
    """Mixed rule history: the exploring rule parks, the cycle advances, ``sync`` returns the item, then the focus
    rule parks it. The overrule must suspend the focus rule and return the item, not an older operation."""
    store, sid = wait["store"], wait["sid"]
    _park(wait)
    _focus_rewrite(wait, "in-demo-c01", stage="decided")
    _focus_set(wait, "in-demo-c02")
    _sync(wait)
    assert store.get(sid)["status"] == "open", "the advanced cycle comes back to the user"
    store.add_version(
        sid,
        question="Start this decided cycle?",
        options=[
            {
                "id": "A",
                "label": "Start now",
                "action": "set_cycle_fields",
                "args": {"cycle": "in-demo-c01", "fields": {"stage": "shipping"}},
            },
            {"id": "B", "label": "Wait behind focus", "action": "defer", "args": {"kind": "state_change"}},
        ],
        recommend="B",
        context="The cycle advanced from exploring to decided.",
    )
    done = _park_focus(wait)
    result = store.overrule(sid, "This decided cycle should start now, not wait behind the focus cycle.")
    assert result["rule"] == FOCUS_RULE, "the newest park is the operation the overrule names"
    assert result["returned"] is True
    assert store.get(sid)["status"] == "open", "the focus park is returned"
    assert store.rule_states()[FOCUS_RULE]["state"] == "suspended"
    assert "defer_exploring_cycle" not in store.rule_states(), "the older operation is not the current park"
    overrules = [a for a in store.actions(sid) if a["action"] == "overrule"]
    assert json.loads(overrules[-1]["args_json"]) == {
        "rule": FOCUS_RULE,
        "deferral_id": done["receipt"]["deferral_id"],
    }
    with pytest.raises(reviews.ReviewError, match="already overruled"):
        store.overrule(sid, "The same focus park overruled twice.")


def test_defer_behind_focus_cycle_overrule_keeps_a_superseding_user_wait(focus):
    """An overrule names the park and suspends the rule; a wait the user chose after that park is left as it is."""
    done = _park_focus(focus)
    focus["store"].defer(focus["sid"], kind="date", until="2027-01-01", reason="The user chose his own date instead")
    result = focus["store"].overrule(focus["sid"], "The park was wrong, but my own later wait stands.")
    assert result["rule"] == FOCUS_RULE and result["returned"] is False
    assert focus["store"].rule_states()[FOCUS_RULE]["state"] == "suspended"
    item = focus["store"].get(focus["sid"])
    assert item["status"] == "deferred", "the newer user-owned wait is not reopened"
    assert item["deferral"]["until"] == "2027-01-01"
    overrules = [a for a in focus["store"].actions(focus["sid"]) if a["action"] == "overrule"]
    assert json.loads(overrules[0]["args_json"]) == {
        "rule": FOCUS_RULE,
        "deferral_id": done["receipt"]["deferral_id"],
    }
    with pytest.raises(reviews.ReviewError, match="already overruled"):
        focus["store"].overrule(focus["sid"], "The same park overruled twice.")


def test_defer_behind_focus_cycle_return_binds_to_its_own_deferral(focus, monkeypatch):
    """A deferral written while the return reads its sources supersedes the park: the rule must not reopen it."""
    _park_focus(focus)
    _focus_rewrite(focus, "in-demo-c02", stage="measuring")
    other = reviews.ReviewStore(focus["store"].path)
    original = review_settle._focus_wait_stopped

    def user_defers_while_reading(item, receipt, projects):
        reason = original(item, receipt, projects)
        other.defer(focus["sid"], kind="date", until="2027-01-01", reason="The user chose his own date meanwhile")
        return reason

    monkeypatch.setattr(review_settle, "_focus_wait_stopped", user_defers_while_reading)
    assert review_settle.return_focus_waits(focus["store"], [focus["project"]]) == []
    item = focus["store"].get(focus["sid"])
    assert item["status"] == "deferred", "the newer user wait is never reopened by the rule"
    assert item["deferral"]["until"] == "2027-01-01"
    assert [a["action"] for a in focus["store"].actions(focus["sid"])] == [f"settle:{FOCUS_RULE}"]


DONE_RULE = "plan_after_cards_done"
DONE_WHY = "Every card is done and the recommended next step belongs to the configured plan lane."


@pytest.fixture
def cards_done(env):
    with kbc.connect(board="default") as conn:
        card = kb.create_task(conn, title="Finished delivery", assignee="planner")
        kb.complete_task(conn, card, result="Finished synthetic work")
    path = env["root"] / "in-demo" / "cycles" / "in-demo-c01.md"
    write(path, cycle_md("in-demo-c01", stage="decided", decisions=DECIDED, kanban_cards=[card], next_action="Keep this text"))
    _sync(env)
    sid = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    env.update(sid=sid, target=path)
    _done_options(env)
    return env


def _done_options(env):
    env["store"].add_version(
        env["sid"], question="Plan the next step?", recommend="A", context="Synthetic finished decided cycle.",
        options=[{"id": "A", "label": "Write the next plan", "action": "kanban_create",
                  "args": {"assignee": "planner", "title": "Next plan", "body": "Do the approved next step."}},
                 {"id": "B", "label": "Wait", "action": "defer", "args": {}}],
    )


def _settle_done(env, args=None, why=DONE_WHY):
    return review_settle.Settler(env["store"], env["project"]).settle(env["sid"], DONE_RULE, args or {}, why)


def _done_snapshot(env):
    import sqlite3

    with sqlite3.connect(kb.kanban_db_path("default")) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        board = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
    return _snapshot(env), board


@pytest.mark.parametrize("owner", [None, "Alex"])
def test_plan_after_cards_done_creates_links_and_audits_one_card(cards_done, owner):
    from hermes_initiatives_core.settings import settings_path

    config = json.loads(settings_path().read_text())
    if owner is not None:
        config["owner_name"] = owner
    settings_path().write_text(json.dumps(config))
    owner_name = owner or "the user"
    env = cards_done
    before = parse_markdown(env["target"].read_text(), env["target"])
    tasks = set(_tasks())
    result = _settle_done(env)
    created = set(_tasks()) - tasks
    assert len(created) == 1
    card = created.pop()
    task = _tasks()[card]
    assert task.assignee == "planner"
    assert task.title == "Next plan"
    assert task.body.startswith("- Initiative cycle: in-demo-c01\n")
    assert "Do the approved next step." in task.body
    assert f"rule {DONE_RULE} (review {env['sid']}, recommended option A: Write the next plan)." in task.body
    assert f"Plan approval, high-risk gates, merges, and stage changes stay with {owner_name}." in task.body
    assert task.body.endswith("Synthetic finished decided cycle.")
    assert result["receipt"] == f"{card} for planner, linked to in-demo-c01"
    after = parse_markdown(env["target"].read_text(), env["target"])
    assert after.frontmatter.pop("kanban_cards") == [*before.frontmatter.pop("kanban_cards"), card]
    row = after.frontmatter["decisions"].pop()
    assert after.frontmatter == before.frontmatter and after.body == before.body
    assert {k: row[k] for k in ("by", "rule", "review", "card")} == {
        "by": "agent", "rule": DONE_RULE, "review": env["sid"], "card": card}
    assert row["decision"] == f"Created {card} for planner under the recommended option A; the cycle stays decided"
    assert env["store"].get(env["sid"])["status"] == "settled"
    assert env["store"].answers(env["sid"]) == []
    assert [(a["action"], a["state"]) for a in env["store"].actions(env["sid"])] == [(f"settle:{DONE_RULE}", "settled")]
    assert not validate(load(env["root"]), check_sources=False).errors.get("in-demo-c01")
    with pytest.raises(review_settle.SettleRefused, match="only open"):
        _settle_done(env)
    assert set(_tasks()) == tasks | {card}


@pytest.mark.parametrize("case", [
    "args", "why", "suspended", "deferred", "watching", "auto", "answered", "applying",
    "other_project", "no_project", "section", "source", "key", "cycle", "urgent", "owner_profile", "owner_job", "blocks",
    "reasons", "stage", "verdict", "disposition", "cards_empty", "cards_type", "stale_options", "no_options", "needs_options",
    "answer", "recommend_stage", "lane", "board", "extra_arg", "title_empty", "title_long", "confirm", "other_confirm",
    "marker_title", "marker_body", "kanban_unblock", "attend", "kanban_comment", "duplicate_recommend", "signature",
    "parse_error", "duplicate_id", "stale_basis", "paused", "recorded_shipping", "initiative", "freshness",
    "cycle_user", "initiative_user", "chain", "failed_action", "failed_action_same_day", "cap", "no_lane", "no_board",
])
def test_plan_after_cards_done_refuses_without_writes(cards_done, monkeypatch, case):
    from hermes_initiatives_core.docs import dump_markdown

    env, store = cards_done, cards_done["store"]
    sid = env["sid"]
    args, why = {}, DONE_WHY
    message = {
        "args": "arguments", "why": "why", "suspended": "suspended", "other_project": "project",
        "no_project": "project", "section": "gated", "source": "gated", "key": "key", "cycle": "key",
        "urgent": "urgency", "owner_profile": "urgency", "owner_job": "urgency", "blocks": "urgency",
        "reasons": "basis", "stage": "basis", "verdict": "basis", "disposition": "basis",
        "cards_empty": "basis", "cards_type": "basis", "stale_options": "current", "no_options": "current",
        "needs_options": "current", "answer": "answer", "recommend_stage": "recommended",
        "lane": "assignee", "board": "board", "extra_arg": "arguments", "title_empty": "title", "title_long": "title",
        "confirm": "confirm", "other_confirm": "confirm", "marker_title": "marker", "marker_body": "marker",
        "duplicate_recommend": "recommended", "signature": "signature", "parse_error": "malformed", "duplicate_id": "malformed",
        "stale_basis": "collector", "paused": "active", "recorded_shipping": "collector|decided",
        "initiative": "initiative", "freshness": "fresh", "cycle_user": "user decision", "initiative_user": "user decision",
        "chain": "chain", "failed_action": "chain guard", "failed_action_same_day": "chain guard", "cap": "24 hours",
        "no_lane": "plan_assignee", "no_board": "kanban_board",
        "kanban_unblock": "human gate", "attend": "human gate", "kanban_comment": "human gate",
    }.get(case, "only open")
    def column(name, value):
        with store._connect() as conn:
            conn.execute(f"UPDATE items SET {name}=? WHERE short_id=?", (value, sid))
    def options(change):
        _focus_options(env, change=change)
    def rewrite(path, change):
        doc = parse_markdown(path.read_text(), path)
        change(doc.frontmatter)
        write(path, dump_markdown(doc.frontmatter, doc.body))
    if case == "args":
        args = {"extra": True}
    elif case == "why":
        why = "Short"
    elif case == "suspended":
        store.set_rule_state(DONE_RULE, "suspended", "User stopped the rule")
    elif case in {"deferred", "watching", "auto", "answered", "applying"}:
        column("status", case)
    elif case in {
        "other_project", "no_project", "section", "source", "key", "cycle", "urgent",
        "owner_profile", "owner_job", "blocks", "initiative"
    }:
        name, value = {
            "other_project": ("project", "other"), "no_project": ("project", None), "section": ("section", "suggested"),
            "source": ("source", "idea"), "key": ("key", "wrong"), "cycle": ("cycle", "not-a-cycle"),
            "urgent": ("urgent_rule", "consent"), "owner_profile": ("owner_profile", ""), "owner_job": ("owner_job", "job"),
            "blocks": ("blocks_json", '["t_abcdef12"]'), "initiative": ("initiative", "in-other"),
        }[case]
        column(name, value)
    elif case in {"reasons", "stage", "verdict", "disposition", "cards_empty", "cards_type", "stale_options"}:
        name, value = {"reasons": ("reasons", ["cards_done", "no_cards"]), "stage": ("stage", "shipping"),
                       "verdict": ("verdict", "win"), "disposition": ("disposition", "continue"),
                       "cards_empty": ("cards_done", []), "cards_type": ("cards_done", "one"),
                       "stale_options": ("cards_done", ["t_abcdef12"])}[case]
        basis = store.get(sid)["basis"]
        basis[name] = value
        store.refresh_basis(sid, basis)
    elif case == "no_options":
        with store._connect() as conn:
            conn.execute("DELETE FROM item_versions WHERE short_id=?", (sid,))
    elif case == "needs_options":
        monkeypatch.setattr(store, "needs_options", lambda: [store.get(sid)])
    elif case == "answer":
        store.answer(sid, "B", surface="tab")
        column("status", "open")
    elif case == "recommend_stage":
        options(lambda opts: opts[0].update(action="set_cycle_fields"))
    elif case in {"lane", "board", "extra_arg", "title_empty", "title_long"}:
        key, value = {"lane": ("assignee", "coder"), "board": ("board", "other"), "extra_arg": ("extra", True),
                      "title_empty": ("title", "  "), "title_long": ("title", "x" * 201)}[case]
        options(lambda opts: opts[0]["args"].update({key: value}))
    elif case in {"confirm", "other_confirm"}:
        version = store.get(sid)["version"]
        version["options"][case == "other_confirm"]["confirm"] = True
        with store._connect() as conn:
            conn.execute("UPDATE item_versions SET options_json=? WHERE short_id=? AND n=?",
                         (json.dumps(version["options"]), sid, version["n"]))
    elif case in {"marker_title", "marker_body"}:
        field = "title" if case == "marker_title" else "body"
        options(lambda opts: opts[0]["args"].update({field: "Initiative cycle: in-other-c01"}))
    elif case in {"kanban_unblock", "attend", "kanban_comment"}:
        options(lambda opts: opts[1].update(action=case))
    elif case == "duplicate_recommend":
        with store._connect() as conn:
            version = store.get(sid)["version"]
            version["options"][1]["id"] = "A"
            conn.execute("UPDATE item_versions SET options_json=? WHERE short_id=?", (json.dumps(version["options"]), sid))
    elif case == "signature":
        monkeypatch.setattr(review_settle.review_learn, "signature", lambda *a: "wrong")
    elif case == "parse_error":
        write(env["root"] / "ideas" / "bad.md", "---\n[broken\n---\n")
    elif case == "duplicate_id":
        write(env["root"] / "in-demo" / "cycles" / "copy.md", env["target"].read_text())
    elif case == "stale_basis":
        with kbc.connect(board="default") as conn:
            conn.execute("UPDATE tasks SET status='running'")
    elif case in {"paused", "recorded_shipping", "cycle_user", "initiative_user", "chain"}:
        path = env["root"] / "in-demo" / "initiative.md" if case in {"paused", "initiative_user"} else env["target"]
        if case == "paused":
            rewrite(path, lambda fm: fm.update(status="paused"))
        elif case == "recorded_shipping":
            rewrite(path, lambda fm: fm.update(stage="shipping"))
        else:
            row = {"date": store.get(sid)["opened_at"][:10], "by": "user", "decision": "Wait for me"}
            if case == "chain":
                row.update(by="agent", rule=DONE_RULE)
            rewrite(path, lambda fm: fm["decisions"].append(row))
    elif case == "freshness":
        original = review_settle.build_detail
        def stale(*a, **kw):
            detail = original(*a, **kw)
            detail["freshness"]["kanban"] = "stale"
            return detail
        monkeypatch.setattr(review_settle, "build_detail", stale)
    elif case in {"failed_action", "failed_action_same_day"}:
        import datetime as dt

        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        with monkeypatch.context() as clock:
            clock.setattr(reviews, "now_iso", lambda: yesterday + "T12:00:00+00:00")
            store.log_action(None, sid, f"settle:{DONE_RULE}", {}, "failed", error="Create result unknown")
        if case == "failed_action_same_day":
            rewrite(env["target"], lambda fm: fm["decisions"].append({
                "date": yesterday, "by": "user", "decision": "Cannot prove ordering within the failure day",
            }))
    elif case == "cap":
        other, _ = store.upsert_item("request:other", section="gated", source="request:test", title="Other cycle", basis={})
        for _ in range(3):
            store.log_action(None, other["short_id"], f"settle:{DONE_RULE}", {}, "settled", receipt="Earlier plan")
    elif case in {"no_lane", "no_board"}:
        env["project"] = env["project"].model_copy(update={"plan_assignee" if case == "no_lane" else "kanban_board": None})
    before = _done_snapshot(env)
    with pytest.raises(review_settle.SettleRefused, match=message):
        _settle_done(env, args, why)
    assert _done_snapshot(env) == before


def test_plan_after_cards_done_chain_guard_returns_the_next_step(cards_done):
    from hermes_initiatives_core.docs import dump_markdown

    env = cards_done
    _settle_done(env)
    card = _fm(env["target"])["kanban_cards"][-1]
    _sync(env)
    assert env["store"].get_by_key("cycle:demo:in-demo-c01:gate") is None
    with kbc.connect(board="default") as conn:
        kb.complete_task(conn, card, result="Next step done")
    _sync(env)
    env["sid"] = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    _done_options(env)
    with pytest.raises(review_settle.SettleRefused, match="chain"):
        _settle_done(env)
    doc = parse_markdown(env["target"].read_text(), env["target"])
    doc.frontmatter["decisions"].append({"date": "2026-09-02", "by": "user", "decision": "Continue with another step"})
    write(env["target"], dump_markdown(doc.frontmatter, doc.body))
    result = _settle_done(env)
    assert result["receipt"] != f"{card} for planner, linked to in-demo-c01"
    assert len(_fm(env["target"])["kanban_cards"]) == 3


@pytest.mark.parametrize("later_user", [False, True])
def test_plan_after_cards_done_failure_fails_closed_to_user(cards_done, monkeypatch, later_user):
    import datetime as dt

    from hermes_initiatives_core.docs import dump_markdown

    env = cards_done
    now_iso = reviews.now_iso
    if later_user:
        failed_at = (dt.date.today() - dt.timedelta(days=2)).isoformat() + "T12:00:00+00:00"
        monkeypatch.setattr(reviews, "now_iso", lambda: failed_at)
    original = review_settle.link_cards
    def fail(*args):
        raise OSError("injected link failure")
    monkeypatch.setattr(review_settle, "link_cards", fail)
    before = set(_tasks())
    with pytest.raises(review_settle.ActionFailed, match="injected link failure"):
        _settle_done(env)
    created = set(_tasks()) - before
    assert len(created) == 1
    card = created.pop()
    assert env["store"].get(env["sid"])["status"] == "open"
    actions = env["store"].actions(env["sid"])
    assert [(a["action"], a["state"]) for a in actions] == [(f"settle:{DONE_RULE}", "failed")]
    assert card in actions[0]["error"]
    monkeypatch.setattr(reviews, "now_iso", now_iso)
    monkeypatch.setattr(review_settle, "link_cards", original)
    snapshot = _done_snapshot(env)
    with pytest.raises(review_settle.SettleRefused, match="collector"):
        _settle_done(env)
    assert _done_snapshot(env) == snapshot
    failed_sid = env["sid"]
    _sync(env)
    assert env["store"].get(failed_sid)["status"] == "resolved_elsewhere"
    with kbc.connect(board="default") as conn:
        kb.complete_task(conn, card, result="Orphaned rule card finished")
    _sync(env)
    env["sid"] = env["store"].get_by_key("cycle:demo:in-demo-c01:gate")["short_id"]
    assert env["sid"] != failed_sid
    _done_options(env)
    snapshot = _done_snapshot(env)
    with pytest.raises(review_settle.SettleRefused, match="chain guard"):
        _settle_done(env)
    assert _done_snapshot(env) == snapshot
    if later_user:
        doc = parse_markdown(env["target"].read_text(), env["target"])
        doc.frontmatter["decisions"].append({
            "date": (dt.date.today() - dt.timedelta(days=1)).isoformat(),
            "by": "user", "decision": "Continue after the failed rule card",
        })
        write(env["target"], dump_markdown(doc.frontmatter, doc.body))
        result = _settle_done(env)
        new_cards = set(_tasks()) - before - {card}
        assert len(new_cards) == 1
        assert result["receipt"] == f"{new_cards.pop()} for planner, linked to in-demo-c01"
    else:
        assert set(_tasks()) == before | {card}


def test_plan_after_cards_done_claim_blocks_a_concurrent_answer(cards_done, monkeypatch):
    env = cards_done
    other = reviews.ReviewStore(env["store"].path)
    original = review_settle.link_cards
    def during_link(*args):
        assert other.get(env["sid"])["status"] == "applying"
        answer = other.answer(env["sid"], "B", surface="tab")
        assert answer["accepted"] is False and "applying" in answer["reason"]
        return original(*args)
    monkeypatch.setattr(review_settle, "link_cards", during_link)
    _settle_done(env)
    assert env["store"].get(env["sid"])["status"] == "settled" and other.answers(env["sid"]) == []


def test_plan_after_cards_done_overrule_suspends_the_rule(cards_done):
    from hermes_initiatives_core import review_learn

    env = cards_done
    _settle_done(env)
    result = review_learn.overrule(env["store"], [env["project"]], env["sid"], "This step should wait for my decision.")
    assert result["rule"] == DONE_RULE
    assert env["store"].rule_states()[DONE_RULE]["state"] == "suspended"
    followup = env["store"].get(result["follow_up"])
    assert followup["source"] == "learn"
    assert followup["version"]["options"][1]["args"] == {"rule": DONE_RULE, "state": "active"}
