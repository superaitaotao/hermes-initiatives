"""Existing options must not hide routine work from the next review tick.

Owner boundary: real ReviewStore -> tick input, without executing any proposed action.
The regression is a current, already-present recommendation being skipped forever because
needs_options() is empty. Existing tick tests cover only new/changed options and pushes.
"""

from hermes_initiatives_core import reviews
from hermes_initiatives_core.review_routine import candidates


def test_existing_routine_candidates_return_without_reopening_or_answering(tmp_path):
    store = reviews.ReviewStore(tmp_path / "reviews.db")
    ids = {}
    for name, action, args in [
        ("plan", "kanban_create", {"assignee": "planner", "title": "Diagnose the gap"}),
        ("contract", "set_cycle_fields", {"cycle": "in-demo-c02", "fields": {"measurement": {"source": "Approved"}}}),
        ("verdict", "set_cycle_fields", {"cycle": "in-demo-c03", "fields": {"result": {"verdict": "win"}}}),
        ("human", "attend", {"you": "Review the UI", "next": "Record the answer"}),
    ]:
        item, _ = store.upsert_item(
            f"cycle:demo:in-demo-c0{len(ids) + 1}:gate",
            section="gated",
            source="cycle",
            title=name,
            project="demo",
            initiative="in-demo",
            cycle=f"in-demo-c0{len(ids) + 1}",
            basis={"reasons": ["outcome_review_ready"]},
        )
        sid = item["short_id"]
        ids[name] = sid
        store.add_version(
            sid,
            question="Next?",
            options=[
                {"id": "A", "label": name, "action": action, "args": args},
                {"id": "B", "label": "Wait", "action": "defer", "args": {}},
            ],
            recommend="A",
            context="Evidence to inspect",
        )
    assert store.needs_options() == []
    before = {sid: store.get(sid) for sid in ids.values()}
    result = candidates(store)
    assert {(r["short_id"], r["rule"]) for r in result} == {
        (ids["plan"], "plan_outcome_followup"),
        (ids["contract"], "normalize_outcome_contract"),
    }
    assert all(r["basis"] == {"reasons": ["outcome_review_ready"]} for r in result)
    assert {sid: store.get(sid) for sid in ids.values()} == before
    assert all(store.answers(sid) == [] for sid in ids.values())
    store.watch(ids["plan"], "agent_owned: t_00000001 is preparing the plan")
    store.defer(ids["contract"], kind="indefinite", until=None, reason="User chose to wait")
    assert candidates(store) == []


def test_malformed_basis_is_skipped_without_crashing(tmp_path):
    """A basis that is not a mapping cannot claim the ready marker; it must not raise AttributeError."""
    store = reviews.ReviewStore(tmp_path / "reviews.db")
    for index, basis in enumerate([["outcome_review_ready"], "outcome_review_ready", 3]):
        item, _ = store.upsert_item(
            f"cycle:demo:in-demo-c9{index}:gate",
            section="gated",
            source="cycle",
            title=f"malformed {index}",
            project="demo",
            initiative="in-demo",
            cycle=f"in-demo-c9{index}",
            basis=basis,
        )
        store.add_version(
            item["short_id"],
            question="Next?",
            options=[
                {"id": "A", "label": "Plan", "action": "kanban_create", "args": {"assignee": "planner", "title": "Plan"}},
                {"id": "B", "label": "Wait", "action": "defer", "args": {}},
            ],
            recommend="A",
            context="Evidence to inspect",
        )
    assert candidates(store) == []
