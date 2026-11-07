"""Generator output validation, the watching state, and the Telegram digest."""

from __future__ import annotations

import pytest
from conftest import SUMMARY

from hermes_initiatives_core import review_digest, review_write, reviews


@pytest.fixture
def store(tmp_path):
    return reviews.ReviewStore(tmp_path / "reviews.db")


OPTS = [
    {"id": "A", "label": "Approve the matrix", "action": "kanban_unblock", "args": {}},
    {"id": "B", "label": "Keep parked", "action": "defer", "args": {}},
]


def _setup(store, n=2):
    ids = []
    for i in range(n):
        item, _ = store.upsert_item(
            f"kanban:b:t_{i}:gate",
            section="gated",
            source="kanban",
            title=f"Card {i}",
            basis={"e": i},
            blocks=[f"t_c{i}_{j}" for j in range(i + 1)],
        )
        ids.append(item["short_id"])
    review = store.create_review(trigger="cron", model="provider/plan", window_start=None)
    store.link_items(review["id"], ids)
    return review["id"], ids


def _payload(review_id, ids, **extra):
    items = {
        sid: {"question": f"Decide {sid}?", "options": OPTS, "recommend": "A", "why": "ctx", "summary": SUMMARY} for sid in ids
    }
    return {"review_id": review_id, "items": items, "summaries": {}, "suggestions": [], **extra}


def test_accepts_complete_output_and_stores_versions(store):
    review_id, ids = _setup(store)
    result = review_write.apply(store, _payload(review_id, ids))
    assert result["errors"] == []
    assert all(store.get(s)["version"]["question"] == f"Decide {s}?" for s in ids)


def test_rejects_missing_and_unknown_items_and_writes_nothing(store):
    review_id, ids = _setup(store)
    payload = _payload(review_id, ids[:1])
    payload["items"]["D99"] = payload["items"][ids[0]]
    result = review_write.apply(store, payload)
    assert any("D99" in e for e in result["errors"])
    assert any(ids[1] in e for e in result["errors"])
    assert store.get(ids[0])["version"] is None


def test_rejects_bad_action_with_item_id_in_error(store):
    review_id, ids = _setup(store, n=1)
    payload = _payload(review_id, ids)
    payload["items"][ids[0]]["options"] = [{"id": "A", "label": "x", "action": "shell", "args": {}}, OPTS[1]]
    result = review_write.apply(store, payload)
    assert result["errors"] and ids[0] in result["errors"][0] and "unknown action" in result["errors"][0]


def test_criteria_item_refuses_a_decision_row_answer(store):
    item, _ = store.upsert_item("criteria:p:in-x", section="gated", source="criteria", title="Criteria", basis={"v": 1})
    review = store.create_review(trigger="cron", model="m", window_start=None)
    store.link_items(review["id"], [item["short_id"]])
    payload = _payload(review["id"], [item["short_id"]])
    entry = payload["items"][item["short_id"]]
    entry["options"] = [
        {"id": "A", "label": "Record it", "action": "record_decision", "args": {"cycle": "in-x-c01", "decision": "x"}},
        {"id": "B", "label": "Later", "action": "defer", "args": {}},
    ]
    errors = review_write.apply(store, payload)["errors"]
    assert errors and "set_cycle_fields" in errors[0]
    entry["options"][0] = {
        "id": "A",
        "label": "Set it",
        "action": "set_cycle_fields",
        "args": {"cycle": "in-x-c01", "fields": {"success_criteria": "x"}},
    }
    assert review_write.apply(store, payload)["errors"] == []


def test_watch_needs_a_reason_and_reopens_when_basis_changes(store):
    review_id, ids = _setup(store, n=1)
    for reason in (" ", "an agent owns it", "machine:"):
        bad = _payload(review_id, [])
        bad["items"] = {ids[0]: {"watch": reason}}
        assert review_write.apply(store, bad)["errors"], f"a watch without grounds and evidence is refused: {reason!r}"
        assert store.get(ids[0])["status"] == "open"
    good = _payload(review_id, [])
    good["items"] = {ids[0]: {"watch": "machine: card says nothing needed from you."}}
    assert review_write.apply(store, good)["errors"] == []
    item = store.get(ids[0])
    assert item["status"] == "watching" and "nothing needed" in item["watch_reason"]
    store.upsert_item(item["key"], section="gated", source="kanban", title="Card 0", basis={"e": 0})
    assert store.reopen_changed_watches() == []
    store.upsert_item(item["key"], section="gated", source="kanban", title="Card 0", basis={"e": "moved"})
    assert store.reopen_changed_watches() == [ids[0]]
    assert store.get(ids[0])["status"] == "open"


def test_suggestions_are_capped_and_need_evidence(store):
    review_id, ids = _setup(store, n=1)
    payload = _payload(review_id, ids)
    suggestion = {"key": "suggest:x", "title": "T", "question": "Q?", "options": OPTS, "recommend": "A", "evidence": []}
    payload["suggestions"] = [suggestion]
    assert any("evidence" in e for e in review_write.apply(store, payload)["errors"])
    payload["suggestions"] = [{**suggestion, "key": f"suggest:{i}", "evidence": ["a.md"]} for i in range(4)]
    assert any("at most 3" in e for e in review_write.apply(store, payload)["errors"])


def test_dismissed_suggestion_with_same_evidence_is_skipped(store):
    review_id, ids = _setup(store, n=1)
    store.dismiss("suggest:x", evidence_hash=reviews.basis_hash(["a.md"]))
    payload = _payload(review_id, ids)
    payload["suggestions"] = [
        {"key": "suggest:x", "title": "T", "question": "Q?", "options": OPTS, "recommend": "A", "evidence": ["a.md"]}
    ]
    result = review_write.apply(store, payload)
    assert result["errors"] == [] and result["skipped_suggestions"] == ["suggest:x"]
    assert store.get_by_key("suggest:x") is None


def test_digest_puts_decisions_first_and_splits_under_the_limit(store):
    review_id, ids = _setup(store, n=2)
    payload = _payload(review_id, ids)
    payload["summaries"] = {f"in-{i}": "Goal moving. " * 40 for i in range(30)}
    review_write.apply(store, payload)
    messages = review_digest.render(store, review_id, limit=1500)
    assert len(messages) > 1 and all(len(m) <= 1500 for m in messages)
    assert messages[0].index(ids[1]) < messages[0].index(ids[0]), "more blocked work ranks first"
    assert "Updates" not in messages[0].split(ids[0])[0]
    joined = "\n".join(messages)
    assert "Reply here" in joined and "A (recommended)" in joined


def test_groups_split_decisions_and_updates_by_initiative(store):
    keys = [("t_1", "in-a", 3), ("t_2", "in-b", 1), ("t_3", None, 1), ("t_4", "in-a", 1)]
    ids = []
    for card, initiative, blocked in keys:
        item, _ = store.upsert_item(
            f"kanban:b:{card}:gate",
            section="gated",
            source="kanban",
            title=f"Card {card}",
            basis={"c": card},
            initiative=initiative,
            blocks=[f"{card}_{j}" for j in range(blocked)],
        )
        ids.append(item["short_id"])
    review = store.create_review(trigger="cron", model="m", window_start=None)
    payload = _payload(review["id"], ids[:3])
    payload["items"][ids[3]] = {"watch": "machine: card t_x says nothing is needed"}
    payload["summaries"] = {"in-a": "A is shipping.", "in-b": "B is exploring.", "in-c": "C is quiet."}
    payload["updates"] = {"in-a": "PR merged."}
    review_write.apply(store, payload)
    store.update_review(review["id"], facts_json='{"unchanged": ["in-c"]}')
    groups = review_digest.groups(store, review["id"])
    assert [g["initiative"] for g in groups] == ["in-a", "in-b", "", "in-c"], "ranked by their top decision; quiet last"
    a = groups[0]
    assert [i["short_id"] for i in a["decisions"]] == [ids[0]] and [i["short_id"] for i in a["watching"]] == [ids[3]]
    assert a["summary"] == "A is shipping." and a["update"] == "PR merged."
    assert groups[3]["update"] == "No change since the last review." and not groups[3]["decisions"]
    joined = "\n".join(review_digest.render(store, review["id"]))
    assert joined.index("== in-a") < joined.index(ids[0]) < joined.index("== in-b") < joined.index(ids[1])
    assert joined.index("== No initiative") < joined.index(ids[2])
    assert "Since last review: PR merged." in joined.split("== in-b")[0]
    assert "C is quiet." in joined.split("== in-c")[1], "an unchanged initiative still shows its summary"
    assert f"Watching {ids[3]}" in joined.split("== in-a")[1].split("== in-b")[0]


def test_digest_lists_coverage_gaps_and_watching(store):
    review_id, ids = _setup(store, n=2)
    payload = _payload(review_id, ids[:1])
    payload["items"][ids[1]] = {"watch": "machine: card t_x says nothing is needed"}
    review_write.apply(store, payload)
    store.update_review(review_id, gaps_json='[{"source": "job x", "ref": "t_dead", "line": "NEEDS YOU: approve"}]')
    joined = "\n".join(review_digest.render(store, review_id))
    assert "Coverage gaps" in joined and "t_dead" in joined
    assert "Watching" in joined and ids[1] in joined


def test_option_that_would_leave_a_cycle_invalid_is_refused(store, root, monkeypatch, tmp_path):
    # The generator offered "move c02 to measuring" without the measurement fields; the answer then broke the cycle.
    import json

    from conftest import DECIDED, cycle_md, write

    data = tmp_path / "plugin-data"
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_INITIATIVES_DATA_DIR", str(data))
    (data / "settings.json").write_text(json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(root)}]}))
    write(root / "in-demo" / "cycles" / "in-demo-c02.md", cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    log = root / "in-demo" / "observations" / "in-demo-c02.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"at": "2026-10-11T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r"}) + "\n")
    item, _ = store.upsert_item(
        "cycle:demo:in-demo-c02:gate",
        section="gated",
        source="cycle",
        title="c02",
        basis={"v": 1},
        project="demo",
        cycle="in-demo-c02",
    )
    review = store.create_review(trigger="cron", model="m", window_start=None)
    store.link_items(review["id"], [item["short_id"]])
    payload = _payload(review["id"], [item["short_id"]])
    entry = payload["items"][item["short_id"]]
    entry["options"] = [
        {
            "id": "A",
            "label": "Measure",
            "action": "set_cycle_fields",
            "args": {"cycle": "in-demo-c02", "fields": {"stage": "measuring"}},
        },
        {"id": "B", "label": "Later", "action": "defer", "args": {}},
    ]
    errors = review_write.apply(store, payload)["errors"]
    assert errors and "measurement fields" in errors[0] and any("same option" in e for e in errors)
    fields = {"stage": "measuring", "primary_metric": "m", "source": "s", "baseline": "b", "success_criteria": "c"}
    entry["options"][0]["args"]["fields"] = fields
    assert review_write.apply(store, payload)["errors"] == []
