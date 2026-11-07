"""Learned rules: an auto mark appends a delegated rule (with why the item was not automatic), and later ticks settle
items like it under that rule with ``settle``.

Kanban writes run the real ``hermes kanban`` CLI against the isolated test board (conftest pins HERMES_HOME and
HERMES_KANBAN_HOME under tmp_path).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from conftest import cycle_md, write
from test_review_auto import SCRIPTS, WHY, _cycle_options, _fm
from test_review_auto import env as env  # noqa: F401 (the fixture, shared with test_review_auto)

from hermes_initiatives_core import review_auto, review_collect, review_exec, review_learn, review_learned
from hermes_initiatives_core.review_settle import Settler, SettleRefused
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

RULE = {
    "name": "keep_exploring",
    "why_not_auto": "no_rule: no delegated rule covers an exploring cycle whose plan says keep going",
    "when": "An exploring cycle in in-demo whose plan has no stop signal: keep exploring.",
    "match": {"initiative": "in-demo"},
}
NAME = "learned_keep_exploring"


def _keep_options(store, sid, cycle):
    store.add_version(
        sid,
        question=f"What next for {cycle}?",
        recommend="A",
        context="ctx",
        options=[
            {
                "id": "A",
                "label": "Keep exploring",
                "action": "record_decision",
                "args": {"cycle": cycle, "decision": "Keep exploring"},
            },
            {"id": "B", "label": "Ship it", "action": "record_decision", "args": {"cycle": cycle, "decision": "Ship it"}},
            {"id": "C", "label": "Abandon", "action": "idea_reject", "args": {}},
        ],
    )


def _learn(env):
    """D3 (in-demo-c01) marked auto and applied; the auto step appends the rule."""
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, "Keep exploring.", surface="tab")
    done = review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": RULE})
    assert done["state"] == "settled" and done["rule_learned"] == NAME, done


def _new_cycle(env, cycle="in-demo-c03"):
    write(env["root"] / "in-demo" / "cycles" / f"{cycle}.md", cycle_md(cycle, stage="exploring", hypothesis="H"))
    review_collect.sync(env["store"], [env["project"]], gates=env["gates"])
    sid = env["store"].get_by_key(f"cycle:demo:{cycle}:gate")["short_id"]
    _keep_options(env["store"], sid, cycle)
    return sid


def _settle(env, sid, args=None, rule=NAME, why=WHY):
    return Settler(env["store"], env["project"]).settle(sid, rule, {"option": "A"} if args is None else args, why)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (None, 'needs "rule"'),
        ({"skip": "because"}, "rule.skip starts with"),
        ({**RULE, "name": "x"}, "rule.name is a short snake_case name"),
        ({**RULE, "why_not_auto": "it was hard to tell"}, "why_not_auto starts with"),
        ({**RULE, "match": {}}, "match must narrow"),
        ({**RULE, "match": {"title_contains": ["c0"]}}, "at least 4 characters"),
        ({**RULE, "match": {"initiative": "other"}}, "does not fit"),
        ({**RULE, "match": {"stage": "exploring"}}, "match keys must be among"),
    ],
)
def test_auto_apply_needs_a_grounded_rule_and_writes_nothing_without_one(env, rule, message):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    before = (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes()
    entry = {"short_id": sid, "do": "apply", "option": "A", "why": WHY, **({"rule": rule} if rule else {})}
    with pytest.raises(review_auto.AutoRefused, match=message):
        review_auto.handle(store, entry)
    assert store.get(sid)["status"] == "auto" and store.answers(sid) == []
    assert (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes() == before
    assert review_learned.all_rules(store) == []


def test_the_rule_is_appended_with_the_gap_and_the_code_signature(env):
    _learn(env)
    store = env["store"]
    [rule] = review_learned.all_rules(store)
    assert rule["rule"] == NAME and rule["state"] == "active" and rule["from_short_id"] == "D3"
    assert rule["why_not_auto"].startswith("no_rule:") and rule["match"] == {"initiative": "in-demo"}
    assert rule["signature"] == "cycle:exploring:record_decision:keep"
    # The same rule again is not duplicated; a different match under a taken name gets a suffix.
    sid = _new_cycle(env)
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    again = review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": RULE})
    assert again["rule_learned"] == NAME and len(review_learned.all_rules(store)) == 1
    other = _new_cycle(env, "in-demo-c04")
    review_exec.Executor(store).mark_auto(other, surface="tab")
    narrow = {**RULE, "match": {"cycle": "in-demo-c04"}}
    done = review_auto.handle(store, {"short_id": other, "do": "apply", "option": "A", "why": WHY, "rule": narrow})
    assert done["rule_learned"] == f"{NAME}_2"


def test_a_later_item_like_it_is_listed_and_settled_under_the_rule_without_a_mark(env):
    _learn(env)
    store = env["store"]
    sid = _new_cycle(env)
    hits = review_learned.matches(store)
    assert [(h["short_id"], h["rule"], h["args"]) for h in hits] == [(sid, NAME, {"option": "A"})], hits
    assert _settle(env, sid)["rule"] == NAME
    item = store.get(sid)
    assert item["status"] == "settled" and item["closed_note"].startswith(f"{NAME}: A: Keep exploring")
    [row] = [r for r in _fm(env, "in-demo-c03")["decisions"] if r.get("review") == sid]
    assert row["by"] == "agent" and row["rule"] == NAME and row["learned_from"] == "D3"
    assert row["decision"] == "Keep exploring"
    assert not validate(load(env["root"]), check_sources=False).errors.get("in-demo-c03")
    assert review_learned.uses(store, NAME) == 1
    assert sid in [i["short_id"] for i in store.recent_settled()]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"option": "B"}, "does not cover"),  # a different decision (ship, not keep)
        ({"option": "C"}, "cannot be undone"),
        ({"resolve": True}, "covers apply, not resolve"),
        ({}, "takes args"),
        ({"option": "A", "extra": 1}, "takes args"),
    ],
)
def test_a_learned_rule_covers_only_its_own_step(env, args, message):
    _learn(env)
    sid = _new_cycle(env)
    with pytest.raises(SettleRefused, match=message):
        _settle(env, sid, args)
    assert env["store"].get(sid)["status"] == "open" and env["store"].answers(sid) == []


def test_unknown_learned_rules_and_short_reasons_are_refused(env):
    _learn(env)
    sid = _new_cycle(env)
    with pytest.raises(SettleRefused, match="unknown rule 'learned_nope'"):
        _settle(env, sid, rule="learned_nope")
    with pytest.raises(SettleRefused, match="why must say"):
        _settle(env, sid, why="ok")


def test_a_learned_rule_does_not_cover_other_sources(env):
    _learn(env)
    store = env["store"]
    gate = store.get_by_key(f"kanban:default:{env['gate']}:gate")["short_id"]
    with pytest.raises(SettleRefused):
        _settle(env, gate)
    assert all(h["short_id"] != gate for h in review_learned.matches(store))


def test_overruling_a_learned_settlement_suspends_that_rule_only(env):
    _learn(env)
    store = env["store"]
    sid = _new_cycle(env)
    _settle(env, sid)
    projects = load_settings()[0].projects
    done = review_learn.overrule(store, projects, sid, "c03 needed a stop review first.")
    assert done["rule"] == NAME and store.rule_states()[NAME]["state"] == "suspended"
    assert store.rule_states().get("auto_delegated", {}).get("state", "active") == "active", "auto marks keep working"
    later = _new_cycle(env, "in-demo-c04")
    assert review_learned.matches(store) == []
    with pytest.raises(SettleRefused, match="is suspended"):
        _settle(env, later)
    follow = store.get(done["follow_up"])
    restore = next(o for o in follow["version"]["options"] if o["args"].get("state") == "active")
    assert restore["args"]["rule"] == NAME
    assert review_exec.Executor(store).submit(done["follow_up"], restore["id"], surface="tab")["state"] == "applied"
    assert store.rule_states()[NAME]["state"] == "active"
    assert [h["short_id"] for h in review_learned.matches(store)] == [later]


def test_learned_rules_have_a_daily_cap(env, monkeypatch):
    _learn(env)
    monkeypatch.setattr(review_learned, "MAX_USES_PER_DAY", 1)
    first, second = _new_cycle(env), _new_cycle(env, "in-demo-c04")
    _settle(env, first)
    assert review_learned.matches(env["store"]) == []
    with pytest.raises(SettleRefused, match="items today"):
        _settle(env, second)


def test_settlements_under_learned_rules_never_teach_rules(env):
    _learn(env)
    for cycle in ("in-demo-c03", "in-demo-c04", "in-demo-c05"):
        _settle(env, _new_cycle(env, cycle))
    assert review_learn.mine(env["store"]) == []


def test_cli_lists_rules_and_matches_and_settles_under_a_learned_rule(env, tmp_path):
    _learn(env)
    sid = _new_cycle(env)
    gates = write(
        tmp_path / "gates.json",
        json.dumps(
            {
                "board": "default",
                "cards": [{"id": env["gate"], "waiting": "approve the matrix"}],
                "generated_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
            }
        ),
    )

    def run(*args, stdin=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "daily_review.py"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    tick = run("tick", "--gates", str(gates))
    assert tick.returncode == 0, tick.stderr
    assert [(h["short_id"], h["rule"]) for h in json.loads(tick.stdout)["learned_rules"]] == [(sid, NAME)]
    payload = {"settle": [{"short_id": sid, "rule": NAME, "args": {"option": "A"}, "why": WHY}]}
    done = run("settle", "--file", "-", stdin=json.dumps(payload))
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["results"][0]["rule"] == NAME
    rules = {r["rule"]: r for r in json.loads(run("rules").stdout)["rules"]}
    assert rules["auto_delegated"]["kind"] == "coded"
    assert rules[NAME]["kind"] == "learned" and rules[NAME]["uses"] == 1 and rules[NAME]["state"] == "active"
    assert json.loads(run("rules", "--matches").stdout)["matches"] == []
