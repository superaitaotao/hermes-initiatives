"""Stale sweep: review items nobody looked at for 3 days are re-checked by the review job (user decision 2026-11-01).

Each one is kept, refreshed, resolved, or (low or medium risk, recommended option only) applied. Kanban writes run
the real ``hermes kanban`` CLI against the isolated test board (``env`` from test_review_auto).
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import SUMMARY
from test_review_auto import _cycle_options, _fm
from test_review_auto import env as env  # noqa: F401 (the fixture, shared with test_review_auto)

from hermes_initiatives_core import review_collect, review_digest, review_learn, review_sweep, review_write
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

WHY = "Re-read the cycle file and the card: nothing changed since the options, and the recommendation still fits."


def _now(days=0):
    return dt.datetime.now(dt.UTC) + dt.timedelta(days=days)


def _age(store, sid, days=5):
    """Make every trace of *sid* (opened, options, actions, deferrals) *days* old."""
    then = (_now(-days)).replace(microsecond=0).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE items SET opened_at = ? WHERE short_id = ?", (then, sid))
        conn.execute("UPDATE item_versions SET created_at = ? WHERE short_id = ?", (then, sid))
        conn.execute("UPDATE actions SET at = ? WHERE short_id = ?", (then, sid))
        conn.execute("UPDATE deferrals SET at = ? WHERE short_id = ?", (then, sid))
        conn.execute("UPDATE answers SET answered_at = ? WHERE short_id = ?", (then, sid))
    return sid


def _gate(env):
    return env["store"].get_by_key(f"kanban:default:{env['gate']}:gate")["short_id"]


def _sweep(env, entry, now=None):
    return review_sweep.handle(env["store"], {"why": WHY, **entry}, project=env["project"], now=now)


def _stale(store, now=None):
    return [i["short_id"] for i in store.stale_items(now=now)]


def test_only_items_idle_for_three_days_are_listed_and_keep_restarts_the_clock(env):
    store = env["store"]
    sid = _cycle_options(store)
    assert sid not in _stale(store), "fresh options are not stale"
    _age(store, sid)
    assert sid in _stale(store)
    with pytest.raises(review_sweep.SweepRefused, match="why"):
        _sweep(env, {"short_id": sid, "do": "keep", "why": "fine"})
    assert _sweep(env, {"short_id": sid, "do": "keep"})["state"] == "open"
    assert store.get(sid)["status"] == "open"
    assert sid not in _stale(store), "a keep counts as a look"
    assert sid in _stale(store, now=_now(4)), "it is checked again 3 days later"


def test_items_that_are_not_stale_are_refused(env):
    sid = _cycle_options(env["store"])
    with pytest.raises(review_sweep.SweepRefused, match="not idle"):
        _sweep(env, {"short_id": sid, "do": "keep"})


def test_apply_answers_with_the_recommended_option_and_settles_under_stale_sweep(env):
    store = env["store"]
    sid = _age(store, _cycle_options(store))
    result = _sweep(env, {"short_id": sid, "do": "apply", "option": "A", "risk": "low"})
    assert result["state"] == "settled", result
    item = store.get(sid)
    assert item["status"] == "settled" and item["closed_note"].startswith("stale_sweep: A: Keep exploring")
    row = _fm(env)["decisions"][-1]
    assert (row["by"], row["rule"], row["review"], row["decision"]) == ("agent", "stale_sweep", sid, "Keep exploring")
    assert "user delegation 2026-11-01" in row["reason"] and "risk low" in row["reason"]
    assert [r["by"] for r in _fm(env)["decisions"]].count("agent") == 1, "no delegation row: no auto mark exists"
    assert not validate(load(env["root"]), check_sources=False).errors.get("in-demo-c01")
    assert [i["short_id"] for i in store.recent_settled()] == [sid]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"option": "A", "risk": "high"}, "risk"),
        ({"option": "A"}, "risk"),
        ({"option": "C", "risk": "low"}, "recommended"),
    ],
)
def test_apply_refuses_outside_the_delegation_and_writes_nothing(env, entry, message):
    store = env["store"]
    sid = _age(store, _cycle_options(store))
    before = (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes()
    with pytest.raises(review_sweep.SweepRefused, match=message):
        _sweep(env, {"short_id": sid, "do": "apply", **entry})
    assert store.get(sid)["status"] == "open" and store.answers(sid) == []
    assert (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes() == before


@pytest.mark.parametrize(
    ("action", "args", "message"),
    [
        ("attend", {"board": "default", "card": "t_x", "you": "Put the token file on the VM"}, "stays with the user"),
        ("defer", {"kind": "state_change"}, "keep"),
        ("idea_reject", {}, "cannot be undone"),
    ],
)
def test_apply_refuses_user_steps_parking_and_irreversible_options(env, action, args, message):
    store = env["store"]
    sid = _gate(env)
    store.add_version(
        sid,
        question="Will you do it?",
        recommend="A",
        context="",
        options=[
            {"id": "A", "label": "Do the step", "action": action, "args": args},
            {"id": "B", "label": "Unblock", "action": "kanban_unblock", "args": {"board": "default", "card": env["gate"]}},
        ],
    )
    _age(store, sid)
    with pytest.raises(review_sweep.SweepRefused, match=message):
        _sweep(env, {"short_id": sid, "do": "apply", "option": "A", "risk": "low"})
    assert store.get(sid)["status"] == "open" and store.answers(sid) == []


def test_apply_refuses_deferred_items_and_the_deep_merge_gate(env):
    store = env["store"]
    sid = _cycle_options(store)
    store.defer(sid, kind="indefinite", until=None, reason="The user parked it")
    _age(store, sid)
    with pytest.raises(review_sweep.SweepRefused, match="only open"):
        _sweep(env, {"short_id": sid, "do": "apply", "option": "A", "risk": "low"})
    gate = _gate(env)
    store.set_context(gate, "REVIEW PASSED — DEEP: NEEDS YOUR MERGE APPROVAL for head abc")
    store.add_version(
        gate,
        question="Merge?",
        recommend="A",
        context="",
        options=[
            {"id": "A", "label": "Unblock", "action": "kanban_unblock", "args": {"board": "default", "card": env["gate"]}},
            {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "state_change"}},
        ],
    )
    _age(store, gate)
    with pytest.raises(review_sweep.SweepRefused, match="merge"):
        _sweep(env, {"short_id": gate, "do": "apply", "option": "A", "risk": "low"})


def test_refresh_gets_new_options_that_reach_the_digest_marked_refreshed_but_are_not_pushed(env):
    store = env["store"]
    sid = _cycle_options(store)
    store.watch(sid, "machine: the planner owns the next step")
    _age(store, sid)
    assert _sweep(env, {"short_id": sid, "do": "refresh"})["state"] == "open"
    assert sid in {i["short_id"] for i in store.needs_options()}
    options = [
        {"id": "A", "label": "Keep exploring", "action": "record_decision", "args": {"cycle": "in-demo-c01", "decision": "Keep"}},
        {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "state_change"}},
    ]
    entry = {"question": "The plan moved on; what next?", "why": "w", "recommend": "A", "summary": SUMMARY, "options": options}
    others = {i["short_id"]: {"watch": "machine: not part of this test"} for i in store.needs_options() if i["short_id"] != sid}
    assert review_write.apply(store, {"items": {**others, sid: entry}})["errors"] == []
    assert store.get(sid)["refreshed"] is True
    assert sid not in {i["short_id"] for i in store.to_push()}, "a refreshed item waits for the digest"
    blocks = [review_digest._item_block(i, _now()) for i in store.queue() if i["short_id"] == sid]
    assert blocks and "refreshed" in blocks[0].splitlines()[1]
    # A later change of its options is news again and is pushed as usual.
    store.add_version(sid, question="Changed again?", recommend="A", context="", options=options)
    assert store.get(sid)["refreshed"] is False
    assert sid in {i["short_id"] for i in store.to_push()}


def test_refresh_reopens_failed_and_deferred_items(env):
    store = env["store"]
    sid = _cycle_options(store)
    store.set_status(sid, "failed")
    _age(store, sid)
    assert _sweep(env, {"short_id": sid, "do": "refresh"})["state"] == "open"
    store.defer(sid, kind="indefinite", until=None, reason="The user parked it")
    assert _sweep(env, {"short_id": sid, "do": "refresh"}, now=_now(4))["state"] == "open"
    assert sid in {i["short_id"] for i in store.needs_options()}


def test_resolve_settles_and_an_overrule_suspends_resolve_and_apply_but_not_keep(env):
    store = env["store"]
    sid = _cycle_options(store)
    store.set_status(sid, "awaiting_confirm")
    _age(store, sid)
    assert _sweep(env, {"short_id": sid, "do": "resolve"})["state"] == "settled"
    assert store.get(sid)["status"] == "settled"
    done = review_learn.overrule(store, load_settings()[0].projects, sid, "That decision was still mine to make.")
    assert done["rule"] == "stale_sweep" and store.rule_states()["stale_sweep"]["state"] == "suspended"
    other = _age(store, _gate(env))
    with pytest.raises(review_sweep.SweepRefused, match="suspended"):
        _sweep(env, {"short_id": other, "do": "resolve"})
    assert _sweep(env, {"short_id": other, "do": "keep"})["state"] == "open", "keep decides nothing"


def test_apply_has_a_daily_cap(env, monkeypatch):
    monkeypatch.setattr(review_sweep, "MAX_APPLIES_PER_DAY", 0)
    sid = _age(env["store"], _cycle_options(env["store"]))
    with pytest.raises(review_sweep.SweepRefused, match="today"):
        _sweep(env, {"short_id": sid, "do": "apply", "option": "A", "risk": "low"})


@pytest.mark.parametrize("status", ["failed", "awaiting_confirm"])
def test_sync_closes_failed_and_unconfirmed_items_whose_card_finished(env, status):
    # D135/D107/D211: an answer failed (or waited for a confirm), then the card finished; the item stayed open.
    store = env["store"]
    sid = _gate(env)
    store.set_status(sid, status)
    review_collect.sync(store, [env["project"]], gates=env["gates"])
    assert store.get(sid)["status"] == status, "the card is still a gate"
    with kbc.connect(board="default") as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (env["gate"],))
    review_collect.sync(store, [env["project"]], gates={"default": {}})
    assert store.get(sid)["status"] == "resolved_elsewhere"
