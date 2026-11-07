"""Daily review store: stable keys and short ids, versions, first answer wins, deferral, ranking."""

from __future__ import annotations

import datetime as dt

import pytest

from hermes_initiatives_core import reviews


@pytest.fixture
def store(tmp_path):
    return reviews.ReviewStore(tmp_path / "reviews.db")


def _gate(store, key="kanban:b:t_00000001:gate", basis=None, **extra):
    return store.upsert_item(key, section="gated", source="kanban", title="Gate", basis=basis or {"event": 1}, **extra)


def test_same_key_keeps_its_short_id_and_updates_basis(store):
    first, created = _gate(store)
    again, created_again = _gate(store, basis={"event": 2})
    assert created and not created_again
    assert again["short_id"] == first["short_id"] == "D1"
    assert again["basis"] == {"event": 2} and again["basis_hash"] != first["basis_hash"]


def test_short_ids_are_permanent_and_never_reused(store):
    one, _ = _gate(store, key="a")
    store.close_item(one["short_id"], "resolved_elsewhere", note="gone")
    two, _ = _gate(store, key="a")
    assert one["short_id"] == "D1" and two["short_id"] == "D2"


def test_versions_keep_the_exact_options_shown(store):
    item, _ = _gate(store)
    options = [
        {"id": "A", "label": "Resume", "action": "kanban_unblock", "args": {}},
        {"id": "B", "label": "Keep parked", "action": "defer", "args": {}},
    ]
    store.add_version(item["short_id"], question="Resume C2?", options=options, recommend="B", context="ctx")
    store.add_version(item["short_id"], question="Resume C2 now?", options=options, recommend="A", context="ctx2")
    current = store.get(item["short_id"])
    assert current["version"]["question"] == "Resume C2 now?" and current["version"]["n"] == 2
    assert [v["question"] for v in store.versions(item["short_id"])] == ["Resume C2?", "Resume C2 now?"]


def test_version_rejects_unknown_actions_and_bad_recommendation(store):
    item, _ = _gate(store)
    bad = [{"id": "A", "label": "x", "action": "rm_rf", "args": {}}, {"id": "B", "label": "y", "action": "defer", "args": {}}]
    with pytest.raises(reviews.ReviewError, match="unknown action"):
        store.add_version(item["short_id"], question="q", options=bad, recommend="A", context="")
    good = [
        {"id": "A", "label": "x", "action": "acknowledge", "args": {}},
        {"id": "B", "label": "y", "action": "defer", "args": {}},
    ]
    with pytest.raises(reviews.ReviewError, match="recommend"):
        store.add_version(item["short_id"], question="q", options=good, recommend="C", context="")
    with pytest.raises(reviews.ReviewError, match="2 to 4"):
        store.add_version(item["short_id"], question="q", options=good[:1], recommend="A", context="")


def test_answer_to_outdated_options_is_refused_and_not_stored(store):
    item, _ = store.upsert_item("criteria:p:in-x", section="gated", source="criteria", title="t", basis={"cycles": [1, 2]})
    opts = [{"id": "A", "label": "a", "action": "defer", "args": {}}, {"id": "B", "label": "b", "action": "defer", "args": {}}]
    store.add_version(item["short_id"], question="Q", options=opts, recommend="A", context="")
    store.refresh_basis(item["short_id"], {"cycles": [1]})
    with pytest.raises(reviews.ReviewError, match="out of date"):
        store.answer(item["short_id"], "A", surface="tab")
    assert store.answers(item["short_id"]) == [] and store.get(item["short_id"])["status"] == "open"


def test_reopen_returns_a_deferred_item_to_the_queue(store):
    item, _ = store.upsert_item("criteria:p:in-y", section="gated", source="criteria", title="t", basis={"v": 1})
    store.defer(item["short_id"], kind="state_change", until=None, reason="")
    assert store.reopen(item["short_id"]) is True
    assert store.get(item["short_id"])["status"] == "open"
    assert store.reopen(item["short_id"]) is False, "an open item is not reopened again"


def test_first_answer_wins(store):
    item, _ = _gate(store)
    options = [
        {"id": "A", "label": "x", "action": "acknowledge", "args": {}},
        {"id": "B", "label": "y", "action": "defer", "args": {}},
    ]
    store.add_version(item["short_id"], question="q", options=options, recommend="A", context="")
    first = store.answer(item["short_id"], "A", note="from tab", surface="tab")
    second = store.answer(item["short_id"], "B", note="from telegram", surface="telegram")
    assert first["accepted"] and not second["accepted"]
    assert second["reason"] == "already answered"
    assert store.get(item["short_id"])["status"] == "answered"
    assert [a["option"] for a in store.answers(item["short_id"])] == ["A"]


def test_answer_records_the_version_it_answered(store):
    item, _ = _gate(store)
    options = [
        {"id": "A", "label": "x", "action": "acknowledge", "args": {}},
        {"id": "B", "label": "y", "action": "defer", "args": {}},
    ]
    store.add_version(item["short_id"], question="q1", options=options, recommend="A", context="")
    store.answer(item["short_id"], "B", note="", surface="tab")
    assert store.answers(item["short_id"])[0]["version"] == 1


def test_answer_needs_a_valid_option(store):
    item, _ = _gate(store)
    options = [
        {"id": "A", "label": "x", "action": "acknowledge", "args": {}},
        {"id": "B", "label": "y", "action": "defer", "args": {}},
    ]
    store.add_version(item["short_id"], question="q", options=options, recommend="A", context="")
    with pytest.raises(reviews.ReviewError, match="no option"):
        store.answer(item["short_id"], "Z", note="", surface="tab")


def test_deferral_hides_until_date_and_returns(store):
    item, _ = _gate(store)
    store.defer(item["short_id"], kind="date", until="2026-10-20", reason="after holiday")
    assert store.get(item["short_id"])["status"] == "deferred"
    assert store.due_deferrals(today=dt.date(2026, 10, 19), basis_hashes={}) == []
    assert store.due_deferrals(today=dt.date(2026, 10, 20), basis_hashes={}) == [item["short_id"]]


def test_deferral_until_change_returns_when_the_basis_changes(store):
    item, _ = _gate(store)
    store.defer(item["short_id"], kind="state_change", until=None, reason="")
    same = {item["key"]: item["basis_hash"]}
    assert store.due_deferrals(today=dt.date(2026, 10, 12), basis_hashes=same) == []
    changed = {item["key"]: "different"}
    assert store.due_deferrals(today=dt.date(2026, 10, 12), basis_hashes=changed) == [item["short_id"]]


def test_indefinite_deferral_needs_a_reason(store):
    item, _ = _gate(store)
    with pytest.raises(reviews.ReviewError, match="reason"):
        store.defer(item["short_id"], kind="indefinite", until=None, reason="  ")


def test_queue_ranks_blocked_work_times_age(store):
    now = dt.datetime(2026, 10, 12, tzinfo=dt.UTC)
    old, _ = _gate(store, key="old", blocks=["t_1"], opened_at="2026-09-15T00:00:00+00:00")
    wide, _ = _gate(store, key="wide", blocks=["t_1", "t_2", "t_3"], opened_at="2026-10-10T00:00:00+00:00")
    fresh, _ = _gate(store, key="fresh", blocks=[], opened_at="2026-10-11T00:00:00+00:00")
    assert [i["short_id"] for i in store.queue(now=now)] == [old["short_id"], wide["short_id"], fresh["short_id"]]


def test_dismissed_suggestion_returns_only_when_evidence_changes(store):
    store.dismiss("suggest:faster-ci", evidence_hash="e1")
    assert store.is_dismissed("suggest:faster-ci", "e1")
    assert not store.is_dismissed("suggest:faster-ci", "e2")


def test_review_completion_marks_the_update_window(store):
    item, _ = _gate(store)
    review = store.create_review(trigger="cron", model="provider/plan", window_start="2026-10-11T00:30:00+00:00")
    store.link_items(review["id"], [item["short_id"]])
    assert store.last_completed_at() is None
    store.close_item(item["short_id"], "resolved_elsewhere", note="")
    store.refresh_completion()
    assert store.last_completed_at() is not None
