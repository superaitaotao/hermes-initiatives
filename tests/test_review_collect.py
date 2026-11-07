"""Collector: live gates and open asks become review items. Deterministic; reads Kanban and files read-only."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import DECIDED, MEASUREMENT, cycle_md, write

from hermes_initiatives_core import review_collect, reviews
from hermes_initiatives_core.settings import ProjectSettings

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

TODAY = dt.date(2026, 10, 12)


@pytest.fixture
def board(isolated_hermes):
    kbc.init_db(board="default")
    conn = kbc.connect(board="default")
    gate = kb.create_task(conn, title="Approve the removal matrix", assignee="coder")
    kb.block_task(conn, gate, reason="BLOCKED — NEEDS YOU — approve the matrix", kind="needs_input")
    child = kb.create_task(conn, title="Implement phase B", assignee="coder", parents=[gate])
    parked = kb.create_task(conn, title="Waiting on a date", assignee="coder")
    kb.schedule_task(conn, parked, reason="retry tomorrow")
    flaky = kb.create_task(conn, title="Flaky", assignee="coder")
    kb.block_task(conn, flaky, reason="network blip", kind="transient")
    busy = kb.create_task(conn, title="Running", assignee="coder")
    conn.close()
    return {"gate": gate, "child": child, "parked": parked, "flaky": flaky, "busy": busy, "path": kb.kanban_db_path("default")}


@pytest.fixture
def project(root, board):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="exploring"))
    write(
        root / "in-demo" / "cycles" / "in-demo-c02.md",
        cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED, kanban_cards=[board["gate"]]),
    )
    write(
        root / "ideas" / "idea-20261010-faster-ci.md",
        "---\n"
        + json.dumps(
            {
                "id": "idea-20261010-faster-ci",
                "title": "Faster CI",
                "status": "proposed",
                "size": "cycle",
                "evidence": ["x.md"],
                "source": "seo/cron:1",
                "created": "2026-10-10",
            }
        )
        + "\n---\nBody\n",
    )
    return ProjectSettings(id="demo", label="Demo", root=root, kanban_board="default")


@pytest.fixture
def store(tmp_path):
    return reviews.ReviewStore(tmp_path / "reviews.db")


def _keys(result):
    return {c["key"] for c in result["candidates"]}


def gates(*cards, board="default"):
    """A gate list as the recovery tick hands it over: board -> {card: entry}."""
    return {
        board: {card: {"id": card, "signals": ["block kind: needs_input"], "waiting": f"NEEDS YOU on {card}"} for card in cards}
    }


def test_only_cards_on_the_gate_list_become_kanban_candidates(project, board):
    # The recovery tick decides what needs a human; the collector does not re-classify cards.
    listed = review_collect.collect_project(project, today=TODAY, gates=gates(board["gate"], board["flaky"]))
    assert {k for k in _keys(listed) if k.startswith("kanban:")} == {
        f"kanban:default:{board['gate']}:gate",
        f"kanban:default:{board['flaky']}:gate",
    }
    only_gate = review_collect.collect_project(project, today=TODAY, gates=gates(board["gate"]))
    assert {k for k in _keys(only_gate) if k.startswith("kanban:")} == {f"kanban:default:{board['gate']}:gate"}


def test_a_listed_card_that_is_already_done_is_not_a_gate(project, board):
    conn = kbc.connect(board="default")
    kb.complete_task(conn, board["busy"], result="done")
    conn.close()
    result = review_collect.collect_project(project, today=TODAY, gates=gates(board["busy"]))
    assert not [k for k in _keys(result) if k.startswith("kanban:")]


def test_without_a_gate_list_the_board_is_unread_and_reported(project, board):
    result = review_collect.collect_project(project, today=TODAY)
    assert "kanban" not in result["ok"] and not [k for k in _keys(result) if k.startswith("kanban:")]
    assert any("gate list" in e for e in result["errors"])


def test_gate_list_file_must_be_fresh_and_for_the_board(tmp_path):
    now = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC)
    path = tmp_path / "gates.json"
    body = {"generated_at": now.isoformat(), "board": "my-board", "cards": [{"id": "t_1", "waiting": "x", "signals": []}]}
    path.write_text(json.dumps(body))
    assert review_collect.load_gate_list(path, now=now + dt.timedelta(minutes=5)) == {"my-board": {"t_1": body["cards"][0]}}
    with pytest.raises(review_collect.GateListError, match="old"):
        review_collect.load_gate_list(path, now=now + dt.timedelta(minutes=16))
    with pytest.raises(review_collect.GateListError, match="missing|No such"):
        review_collect.load_gate_list(tmp_path / "none.json", now=now)
    path.write_text(json.dumps({"generated_at": now.isoformat(), "cards": []}))
    with pytest.raises(review_collect.GateListError, match="board"):
        review_collect.load_gate_list(path, now=now)


def test_kanban_candidate_carries_reason_and_blocked_work(project, board):
    result = review_collect.collect_project(project, today=TODAY, gates=gates(board["gate"]))
    gate = next(c for c in result["candidates"] if c["key"].startswith("kanban:"))
    assert gate["blocks"] == [board["child"]]
    assert gate["basis"]["status"] == "blocked" and gate["basis"]["block_kind"] == "needs_input"
    assert gate["initiative"] == "in-demo" and gate["cycle"] == "in-demo-c02"
    assert f"NEEDS YOU on {board['gate']}" in gate["context"], "the recovery tick's ask is shown"
    bare = review_collect.collect_project(project, today=TODAY, gates={"default": {board["gate"]: {"id": board["gate"]}}})
    assert "approve the matrix" in next(c for c in bare["candidates"] if c["key"].startswith("kanban:"))["context"], (
        "with no ask on the list, the card's own block reason is shown"
    )


def test_an_assessment_card_gate_belongs_to_its_cycle(project, board, root):
    write(
        root / "in-demo" / "cycles" / "in-demo-c03.md",
        cycle_md("in-demo-c03", stage="shipping", decisions=DECIDED, assessment_cards=[board["flaky"]]),
    )
    result = review_collect.collect_project(project, today=TODAY, gates=gates(board["flaky"]))
    gate = next(c for c in result["candidates"] if c["key"] == f"kanban:default:{board['flaky']}:gate")
    assert (gate["initiative"], gate["cycle"]) == ("in-demo", "in-demo-c03")


def test_cycle_and_idea_gates_are_candidates(project):
    keys = _keys(review_collect.collect_project(project, today=TODAY))
    assert "cycle:demo:in-demo-c01:gate" in keys
    assert "idea:demo:idea-20261010-faster-ci:triage" in keys


def test_missing_criteria_is_one_item_per_initiative(project):
    result = review_collect.collect_project(project, today=TODAY)
    item = next(c for c in result["candidates"] if c["key"] == "criteria:demo:in-demo")
    assert item["basis"]["cycles"] == ["in-demo-c02"]
    assert "cycle:demo:in-demo-c02:gate" not in _keys(result)


def _finish(card):
    conn = kbc.connect(board="default")
    if kb.get_task(conn, card).status == "blocked":
        kb.unblock_task(conn, card)
    kb.complete_task(conn, card, result="done")
    conn.close()


@pytest.mark.parametrize(
    ("stage", "today", "expected"),
    [
        ("decided", TODAY, True),  # a decided cycle with every card done needs its next card or a stage change now
        ("shipping", TODAY, False),  # a shipping cycle gets the release grace week first
        ("shipping", TODAY + dt.timedelta(days=30), True),
    ],
)
def test_cycle_whose_cards_are_all_done_comes_back(root, board, stage, today, expected):
    # Community Reviews c03: decided, its one card done, next card never created. No reason fired and the cycle
    # fell off the Review tab while the pilot waited on the user.
    _finish(board["gate"])
    # Kanban stamps completion with the wall clock; pin it to the test's TODAY so the grace window is date-independent.
    conn = kbc.connect(board="default")
    finished_at = int(dt.datetime.combine(TODAY, dt.time(12), tzinfo=dt.UTC).timestamp())
    with conn:
        conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (finished_at, board["gate"]))
    conn.close()
    write(
        root / "in-demo" / "cycles" / "in-demo-c03.md",
        cycle_md("in-demo-c03", stage=stage, decisions=DECIDED, measurement=MEASUREMENT, kanban_cards=[board["gate"]]),
    )
    project = ProjectSettings(id="demo", label="Demo", root=root, kanban_board="default")
    found = [
        c for c in review_collect.collect_project(project, today=today)["candidates"] if c["key"] == "cycle:demo:in-demo-c03:gate"
    ]
    assert bool(found) is expected
    if expected:
        assert found[0]["basis"]["reasons"] == ["cards_done"] and found[0]["basis"]["cards_done"] == [board["gate"]]
        assert "All 1 cards are done" in found[0]["context"]


AWAITING = {"verdict": "unevaluable", "evidence": []}
WAITING = {"action": "await_evidence", "follow_ups": []}
RELEASED = {"at": "2026-09-20T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}


def _awaiting_cycle(root, window):
    write(
        root / "in-demo" / "cycles" / "in-demo-c02.md",
        cycle_md(
            "in-demo-c02",
            stage="measuring",
            decisions=DECIDED,
            measurement={**MEASUREMENT, "observation_window": window},
            result=AWAITING,
            disposition=WAITING,
        ),
    )
    write(root / "in-demo" / "observations" / "in-demo-c02.jsonl", json.dumps(RELEASED) + "\n")


def test_await_evidence_answer_asks_again_when_its_window_ends(root, store):
    # Test suite c02 (D80): verdict unevaluable + await_evidence, window ended 2026-10-16. evaluation_due needs an
    # unset verdict and the applied item's basis never changed, so the cycle never came back to the Review tab.
    key = "cycle:demo:in-demo-c02:gate"
    project = ProjectSettings(id="demo", label="Demo", root=root)
    _awaiting_cycle(root, {"starts": "verified_release", "length_days": 14})  # ends 2026-10-04
    review_collect.sync(store, [project], today=TODAY)
    item = store.get_by_key(key)
    assert item["status"] == "open" and item["basis"]["reasons"] == ["reevaluation_due"]
    assert item["basis"]["evaluation_date"] == "2026-10-04"

    store.dismiss(key, evidence_hash=item["basis_hash"])  # he answers "keep waiting" with a new window
    store.set_status(item["short_id"], "applied")
    _awaiting_cycle(root, {"starts": "2026-10-12", "length_days": 14})
    review_collect.sync(store, [project], today=TODAY + dt.timedelta(days=13))
    assert store.get_by_key(key) is None  # the new window is still open

    review_collect.sync(store, [project], today=TODAY + dt.timedelta(days=14))
    item = store.get_by_key(key)
    assert item["status"] == "open" and item["basis"]["evaluation_date"] == "2026-10-26"


@pytest.mark.parametrize(
    ("result", "disposition"),
    [
        ({"verdict": "win", "evidence": []}, {"action": "continue", "follow_ups": []}),  # judged, nothing to wait for
        ({"verdict": "unset", "evidence": []}, WAITING),  # still evaluation_due, not a re-evaluation
    ],
)
def test_only_a_judged_await_evidence_cycle_is_reevaluation_due(root, result, disposition):
    write(
        root / "in-demo" / "cycles" / "in-demo-c02.md",
        cycle_md(
            "in-demo-c02",
            stage="measuring",
            decisions=DECIDED,
            measurement=MEASUREMENT,
            result=result,
            disposition=disposition,
        ),
    )
    write(root / "in-demo" / "observations" / "in-demo-c02.jsonl", json.dumps(RELEASED) + "\n")
    project = ProjectSettings(id="demo", label="Demo", root=root)
    found = [
        c for c in review_collect.collect_project(project, today=TODAY)["candidates"] if c["key"] == "cycle:demo:in-demo-c02:gate"
    ]
    assert all("reevaluation_due" not in c["basis"]["reasons"] for c in found)


SPOT_CHECK = {
    **{key: value for key, value in MEASUREMENT.items() if key != "observation_window"},
    "success_criteria": "Post-release spot-check finds no result outside ±5%",
}
VERIFIED = {"at": "2026-10-11T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}


@pytest.mark.parametrize(
    ("measurement", "result", "release", "expected"),
    [
        (SPOT_CHECK, {"verdict": "unset"}, True, True),  # property search c01: verified, criteria, no window
        (SPOT_CHECK, {"verdict": "win"}, True, False),  # the verdict is recorded
        (SPOT_CHECK, {"verdict": "unset"}, False, False),  # not released: no verdict yet
        (MEASUREMENT, {"verdict": "unset"}, True, False),  # a window decides it: evaluation_due on its date
        ({}, {"verdict": "unset"}, True, False),  # no criteria: the criteria item asks instead
    ],
)
def test_verified_spot_check_cycle_asks_for_its_verdict(root, measurement, result, release, expected):
    # Property search c01 passed its post-release spot-check, but with no observation window no evaluation date ever
    # came, and the cycle fell off the Review tab with its verdict unset.
    write(
        root / "in-demo" / "cycles" / "in-demo-c04.md",
        cycle_md("in-demo-c04", stage="shipping", decisions=DECIDED, measurement=measurement, result=result),
    )
    if release:
        write(root / "in-demo" / "observations" / "in-demo-c04.jsonl", json.dumps(VERIFIED) + "\n")
    project = ProjectSettings(id="demo", label="Demo", root=root)
    found = [
        c
        for c in review_collect.collect_project(project, today=TODAY)["candidates"]
        if c["key"] == "cycle:demo:in-demo-c04:gate" and "verdict_due" in c["basis"]["reasons"]
    ]
    assert bool(found) is expected
    if expected:
        assert found[0]["basis"]["stage"] == "measuring" and found[0]["basis"]["reasons"] == ["verdict_due"]
        assert "Evaluate the spot-check" in found[0]["context"]


def test_verified_cycle_without_its_measurement_plan_asks_for_it(root):
    # Property search c01: a release_verified row made it measuring with none of the measurement fields; nothing asked.
    write(root / "in-demo" / "cycles" / "in-demo-c05.md", cycle_md("in-demo-c05", stage="shipping", decisions=DECIDED))
    row = {"at": "2026-10-11T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}
    write(root / "in-demo" / "observations" / "in-demo-c05.jsonl", json.dumps(row) + "\n")
    project = ProjectSettings(id="demo", label="Demo", root=root)
    found = [
        c for c in review_collect.collect_project(project, today=TODAY)["candidates"] if c["key"] == "cycle:demo:in-demo-c05:gate"
    ]
    assert found and "measurement_missing" in found[0]["basis"]["reasons"]
    assert "primary_metric" in found[0]["context"] and "set_cycle_fields" in found[0]["context"]
    # An incremental repair changes the basis so a dismissed option cannot hide the remaining missing fields.
    first_basis = found[0]["basis"]
    write(
        root / "in-demo" / "cycles" / "in-demo-c05.md",
        cycle_md("in-demo-c05", stage="shipping", decisions=DECIDED, measurement={"primary_metric": "m"}),
    )
    repaired = next(
        c for c in review_collect.collect_project(project, today=TODAY)["candidates"] if c["key"] == "cycle:demo:in-demo-c05:gate"
    )
    assert repaired["basis"] != first_basis
    assert "primary_metric" not in repaired["basis"]["measurement_errors"][0]


def _judged_cycle(root, window, disposition):
    write(
        root / "in-demo" / "cycles" / "in-demo-c02.md",
        cycle_md(
            "in-demo-c02",
            stage="measuring",
            decisions=DECIDED,
            measurement={**MEASUREMENT, "observation_window": window},
            result={"verdict": "win", "evidence": []},
            disposition=disposition,
        ),
    )
    write(root / "in-demo" / "observations" / "in-demo-c02.jsonl", json.dumps(RELEASED) + "\n")


def test_verdict_without_disposition_asks_how_the_cycle_ends_when_its_window_ends(root, store):
    # Sale precompute c02 (D163): verdict win, "confirm in the next weekly run", disposition unset. The applied item's
    # basis never changed and no rule covered a judged cycle, so the conclude question never reached the Review tab.
    key = "cycle:demo:in-demo-c02:gate"
    project = ProjectSettings(id="demo", label="Demo", root=root)
    store.dismiss(key, evidence_hash="applied-verdict")  # D163 was answered and applied with an older basis
    _judged_cycle(root, {"starts": "2026-10-12", "length_days": 2}, {"action": "unset", "follow_ups": []})
    review_collect.sync(store, [project], today=TODAY + dt.timedelta(days=1))
    assert store.get_by_key(key) is None  # still inside the window

    review_collect.sync(store, [project], today=TODAY + dt.timedelta(days=2))
    item = store.get_by_key(key)
    assert item["status"] == "open" and item["basis"]["reasons"] == ["disposition_due"]
    assert item["basis"]["verdict"] == "win" and item["basis"]["evaluation_date"] == "2026-10-14"
    assert "Decide the disposition" in item["context"]


@pytest.mark.parametrize(
    ("window", "disposition", "expected"),
    [
        (None, {"action": "unset", "follow_ups": []}, True),  # no window: due at once
        ({"starts": "verified_release", "length_days": 14}, {"action": "stop", "follow_ups": []}, False),  # decided
        ({"starts": "verified_release", "length_days": 14}, WAITING, False),  # await_evidence: reevaluation_due instead
    ],
)
def test_disposition_due_only_for_a_judged_cycle_without_a_disposition(root, window, disposition, expected):
    _judged_cycle(root, window, disposition)
    project = ProjectSettings(id="demo", label="Demo", root=root)
    found = [
        c for c in review_collect.collect_project(project, today=TODAY)["candidates"] if c["key"] == "cycle:demo:in-demo-c02:gate"
    ]
    assert any("disposition_due" in c["basis"]["reasons"] for c in found) is expected


def test_cycle_with_an_open_card_is_not_a_cards_done_gate(project, board):
    write(
        project.root / "in-demo" / "cycles" / "in-demo-c03.md",
        cycle_md("in-demo-c03", stage="decided", decisions=DECIDED, kanban_cards=[board["gate"], board["busy"]]),
    )
    _finish(board["gate"])
    candidates = review_collect.collect_project(project, today=TODAY)["candidates"]
    gates = [c for c in candidates if c["key"] == "cycle:demo:in-demo-c03:gate"]
    # An open card still means no cards_done ask; admission may separately ask for this decided cycle's plan.
    assert all("cards_done" not in g["basis"]["reasons"] for g in gates)


def test_criteria_answered_only_by_a_decision_row_comes_back(project, store):
    # D10 recorded the criteria as a decision row and dismissed the gate; measurement.success_criteria stayed null,
    # so criteria_missing stayed on and the item never returned. The basis now names the field that closes it.
    key = "criteria:demo:in-demo"
    old_basis = {"initiative": "in-demo", "cycles": ["in-demo-c02"]}
    store.dismiss(key, evidence_hash=reviews.basis_hash(old_basis))
    review_collect.sync(store, [project], today=TODAY)
    assert store.get_by_key(key)["status"] == "open"


@pytest.mark.parametrize("source_status", ["done", "archived", "missing"])
def test_sync_resolves_only_completed_or_missing_sources(project, board, store, source_status):
    first = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    gate_key = f"kanban:default:{board['gate']}:gate"
    short_id = store_short(store, gate_key)
    assert gate_key in first["opened"]
    with kbc.connect(board="default") as conn:
        if source_status == "missing":
            conn.execute("DELETE FROM tasks WHERE id = ?", (board["gate"],))
        else:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (source_status, board["gate"]))
    second = review_collect.sync(store, [project], today=TODAY, gates=gates())
    assert store.get(short_id)["status"] == "resolved_elsewhere"
    assert gate_key in second["resolved"]
    assert gate_key not in review_collect.sync(store, [project], today=TODAY, gates=gates())["resolved"]


@pytest.mark.parametrize(
    "source_status,block_kind,item_status",
    [
        ("blocked", "capability", "watching"),
        ("triage", "capability", "open"),
        ("scheduled", None, "deferred"),
    ],
)
def test_omitted_nonterminal_source_keeps_state_and_identity(project, board, store, source_status, block_kind, item_status):
    with kbc.connect(board="default") as conn:
        conn.execute("UPDATE tasks SET status = ?, block_kind = ? WHERE id = ?", (source_status, block_kind, board["gate"]))
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    key = f"kanban:default:{board['gate']}:gate"
    short_id = store_short(store, key)
    if item_status == "watching":
        store.watch(short_id, "An agent owns this capability wait.")
    elif item_status == "deferred":
        store.defer(short_id, kind="indefinite", until=None, reason="Attend with the coordinator.")
    before_ids = {i["short_id"] for i in store.items()}
    result = review_collect.sync(store, [project], today=TODAY, gates=gates())
    item = store.get_by_key(key)
    assert item is not None and (item["short_id"], item["status"]) == (short_id, item_status)
    assert key not in result["resolved"]
    assert {i["short_id"] for i in store.items()} == before_ids
    assert item["basis"]["status"] == source_status
    assert "omission does not prove source completion" in item["context"]


def test_flapping_gate_list_reuses_active_short_id(project, board, store):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    key = f"kanban:default:{board['gate']}:gate"
    short_id = store_short(store, key)
    before_ids = {i["short_id"] for i in store.items()}
    omitted = review_collect.sync(store, [project], today=TODAY, gates=gates())
    returned = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    assert key not in omitted["resolved"] and key not in returned["resolved"]
    assert (store.get_by_key(key)["short_id"], store.get_by_key(key)["status"]) == (short_id, "open")
    assert {i["short_id"] for i in store.items()} == before_ids
    assert key not in returned["opened"]


@pytest.mark.parametrize("mode", ["watching", "state_change", "date", "indefinite"])
def test_omitted_source_refreshes_context_and_existing_reopen_rules(project, board, store, mode):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    key = f"kanban:default:{board['gate']}:gate"
    sid = store_short(store, key)
    if mode == "watching":
        store.watch(sid, "Agent owns the next step.")
    else:
        store.defer(sid, kind=mode, until="2026-10-20" if mode == "date" else None, reason="Keep this wait.")
    basis = store.get(sid)["basis"]
    with kbc.connect(board="default") as conn:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (board["gate"], "coordinator", "Machine wait; attended coordinator owns next action.", 1),
        )
    comment_tick = review_collect.sync(store, [project], today=TODAY, gates=gates())
    item = store.get(sid)
    assert item["basis"] == basis
    assert item["status"] == ("watching" if mode == "watching" else "deferred")
    assert "attended coordinator owns next action" in item["context"]
    assert sid not in comment_tick["reopened"]
    with kbc.connect(board="default") as conn:
        kb.unblock_task(conn, board["gate"])
        conn.execute("UPDATE tasks SET assignee = 'new-owner' WHERE id = ?", (board["gate"],))
    changed = review_collect.sync(store, [project], today=TODAY, gates=gates())
    item = store.get(sid)
    assert item["basis"]["status"] == "ready"
    assert item["basis"]["event"] != basis["event"]
    assert "new-owner" in item["context"]
    assert item["status"] == ("open" if mode in ("watching", "state_change") else "deferred")
    assert (sid in changed["reopened"]) == (mode in ("watching", "state_change"))


@pytest.mark.parametrize("fault", ["missing_board", "schema", "unreadable", "missing_basis"])
def test_omitted_unavailable_source_never_resolves(project, board, store, monkeypatch, fault):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    key = f"kanban:default:{board['gate']}:gate"
    sid = store_short(store, key)
    before = store.get(sid)
    if fault == "missing_board":
        store.refresh_basis(sid, {**before["basis"], "board": "no-such-board"})
        listed = gates(board="no-such-board") | gates()
    elif fault == "missing_basis":
        store.refresh_basis(sid, {"status": "blocked"})
        listed = gates()
    else:
        listed = gates()
        if fault == "schema":
            with kbc.connect(board="default") as conn:
                conn.execute("DROP TABLE task_runs")
        else:

            def unreadable(path):
                raise PermissionError("source unreadable")

            monkeypatch.setattr(review_collect.kanban, "_connect", unreadable)
    result = review_collect.sync(store, [project], today=TODAY, gates=listed)
    assert store.get(sid)["status"] == "open"
    assert store.get(sid)["short_id"] == before["short_id"]
    assert key not in result["resolved"]
    assert result["errors"]


def test_sync_is_idempotent(project, board, store):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    before = {i["short_id"] for i in store.items()}
    again = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    assert {i["short_id"] for i in store.items()} == before
    assert again["opened"] == []


def test_requested_items_are_not_resolved_by_sync(project, board, store):
    store.upsert_item(
        "req:calendar:evt-1",
        section="gated",
        source="request:calendar-sync",
        title="Family or personal?",
        basis={"event": "evt-1"},
    )
    review_collect.sync(store, [project], today=TODAY)
    assert store.get_by_key("req:calendar:evt-1")["status"] == "open"


def test_deferred_item_returns_when_its_basis_changes(project, board, store):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    gate_key = f"kanban:default:{board['gate']}:gate"
    store.defer(store_short(store, gate_key), kind="state_change", until=None, reason="")
    conn = kbc.connect(board="default")
    kb.unblock_task(conn, board["gate"])
    kb.block_task(conn, board["gate"], reason="NEEDS YOU again", kind="needs_input")
    conn.close()
    result = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    assert store.get_by_key(gate_key)["status"] == "open"
    assert store_short(store, gate_key) in result["reopened"]


def test_unavailable_board_is_reported_not_resolved(project, board, store, monkeypatch):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    broken = project.model_copy(update={"kanban_board": "no-such-board"})
    result = review_collect.sync(store, [broken], today=TODAY, gates=gates(board["gate"], board="no-such-board"))
    assert store.get_by_key(f"kanban:default:{board['gate']}:gate")["status"] == "open"
    # A missing gate list (the recovery step failed) also resolves nothing.
    assert review_collect.sync(store, [project], today=TODAY)["errors"]
    gate_key = f"kanban:default:{board['gate']}:gate"
    assert store.get_by_key(gate_key)["status"] == "open"
    assert result["errors"]


def test_updates_since_lists_card_and_cycle_changes_by_initiative(project, board):
    conn = kbc.connect(board="default")
    kb.complete_task(conn, board["busy"], result="done")
    conn.close()
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    updates = review_collect.updates_since(project, since)
    gate_events = [u for u in updates["by_initiative"].get("in-demo", []) if u["card"] == board["gate"]]
    assert {u["kind"] for u in gate_events} >= {"blocked"}
    assert all(u["at"] >= since[:19] for u in gate_events)
    assert updates["unlinked"], "events on cards outside any cycle are kept separately"


def store_short(store, key):
    with store._connect() as conn:
        return conn.execute("SELECT short_id FROM items WHERE key = ? ORDER BY opened_at DESC LIMIT 1", (key,)).fetchone()[0]


@pytest.mark.parametrize("ground", ["agent_owned", "waiting_on"])
def test_watch_on_a_named_card_reopens_when_that_card_finishes(project, board, store, ground):
    # D73: watched as "agent_owned: <reconcile card> is still running", the reconcile card finished, and the gate
    # card itself never changed, so the item stayed hidden. A finished owner card must bring the item back.
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    sid = store_short(store, f"kanban:default:{board['gate']}:gate")
    with kbc.connect(board="default") as conn:
        owner = kb.create_task(conn, title="Reconcile the plan", assignee="orchestrator")
        other = kb.create_task(conn, title="Second prerequisite", assignee="coder")
    store.watch(sid, f"{ground}: {owner} and {other} must finish first; the gate is {board['gate']}")
    with kbc.connect(board="default") as conn:
        assert kb.complete_task(conn, owner, result="reconciled")
    half = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    assert store.get(sid)["status"] == "watching", "one named card is still open"
    assert sid not in half["reopened"]
    with kbc.connect(board="default") as conn:
        assert kb.complete_task(conn, other, result="done")
    done = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    item = store.get(sid)
    assert item["status"] == "open" and sid in done["reopened"]
    assert item["watch_reason"].startswith(ground), "the old reason stays for the triage to read"
    assert sid in {i["short_id"] for i in store.needs_options()}
    again = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    assert sid not in again["reopened"], "a reopen happens once"


def test_watch_reopen_ignores_other_grounds_unknown_cards_and_the_gate_card(project, board, store):
    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"], board["flaky"]))
    gate_sid = store_short(store, f"kanban:default:{board['gate']}:gate")
    flaky_sid = store_short(store, f"kanban:default:{board['flaky']}:gate")
    with kbc.connect(board="default") as conn:
        owner = kb.create_task(conn, title="Owner", assignee="orchestrator")
        assert kb.complete_task(conn, owner, result="done")
    store.watch(gate_sid, f"machine: {owner} comment says nothing needed from you")
    store.watch(flaky_sid, "agent_owned: t_deadbeef (a card that does not exist) owns the next step")
    result = review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"], board["flaky"]))
    assert store.get(gate_sid)["status"] == "watching", "only agent_owned and waiting_on name an owner to check"
    assert store.get(flaky_sid)["status"] == "watching", "an unknown card proves nothing"
    assert not result["reopened"]


def test_write_refuses_a_watch_that_names_only_finished_cards(project, board, store):
    # Without this, the triage could re-watch with the same finished owner and the item would flap every tick.
    from hermes_initiatives_core import review_write

    review_collect.sync(store, [project], today=TODAY, gates=gates(board["gate"]))
    sid = store_short(store, f"kanban:default:{board['gate']}:gate")
    others = {i["short_id"]: {"watch": "machine: not part of this test"} for i in store.needs_options() if i["short_id"] != sid}
    with kbc.connect(board="default") as conn:
        owner = kb.create_task(conn, title="Owner", assignee="orchestrator")
        assert kb.complete_task(conn, owner, result="done")
    stale = review_write.apply(
        store, {"items": {**others, sid: {"watch": f"agent_owned: {owner} is still running the reconcile"}}}
    )
    assert any("only finished cards" in e for e in stale["errors"]) and store.get(sid)["status"] == "open"
    live = review_write.apply(store, {"items": {**others, sid: {"watch": f"waiting_on: {board['child']} must merge first"}}})
    assert live["errors"] == [] and store.get(sid)["status"] == "watching"


def _parked_child(prereq_title="Prepare the probe"):
    """A card created parked behind one open prerequisite, as a planner fans out an attended operation."""
    with kbc.connect(board="default") as conn:
        prereq = kb.create_task(conn, title=prereq_title, assignee="coder")
        parked = kb.create_task(conn, title="Attended pilot", assignee="operator", parents=[prereq], initial_status="blocked")
    return prereq, parked


def test_watch_on_a_parked_card_reopens_once_its_parents_finish(project, board, store):
    # OP-P1 t_33062858: created parked behind PR-P; PR-P merged, the card never moved, its watch never returned,
    # and the consent package it was to file waited a week for nobody.
    prereq, parked = _parked_child()
    review_collect.sync(store, [project], today=TODAY, gates=gates(parked))
    sid = store_short(store, f"kanban:default:{parked}:gate")
    store.watch(sid, "machine: nothing needed now; the operator files the package when the probe merges")
    early = review_collect.sync(store, [project], today=TODAY, gates=gates(parked))
    assert store.get(sid)["status"] == "watching" and sid not in early["reopened"], "a parent is still open"
    with kbc.connect(board="default") as conn:
        assert kb.complete_task(conn, prereq, result="merged")
        assert kb.get_task(conn, parked).status == "blocked", "the board itself leaves a parked card parked"
    done = review_collect.sync(store, [project], today=TODAY, gates=gates(parked))
    item = store.get(sid)
    assert item["status"] == "open" and sid in done["reopened"]
    assert sid in {i["short_id"] for i in store.needs_options()}
    receipt = store.actions(sid)[-1]
    assert receipt["action"] == "reopen_deferral" and prereq in receipt["receipt"]
    store.watch(sid, "machine: the operator was handed the package step")
    again = review_collect.sync(store, [project], today=TODAY, gates=gates(parked))
    assert store.get(sid)["status"] == "watching" and sid not in again["reopened"], "one reopen per finished parent set"


def test_parent_reopen_skips_unparked_parentless_and_deferred_items(project, board, store):
    prereq, parked = _parked_child()
    with kbc.connect(board="default") as conn:
        done_parent = kb.create_task(conn, title="Finished prerequisite", assignee="coder")
        assert kb.complete_task(conn, done_parent, result="merged")
        queued = kb.create_task(conn, title="Queued follow-up", assignee="coder", parents=[done_parent])
    listed = gates(parked, board["flaky"], queued)
    review_collect.sync(store, [project], today=TODAY, gates=listed)
    parked_sid = store_short(store, f"kanban:default:{parked}:gate")
    flaky_sid = store_short(store, f"kanban:default:{board['flaky']}:gate")
    queued_sid = store_short(store, f"kanban:default:{queued}:gate")
    store.defer(parked_sid, kind="state_change", until=None, reason="attend: open an interactive session")
    store.watch(flaky_sid, "machine: transient retry")
    store.watch(queued_sid, "machine: the dispatcher picks it up")
    with kbc.connect(board="default") as conn:
        assert kb.complete_task(conn, prereq, result="merged")
    result = review_collect.sync(store, [project], today=TODAY, gates=listed)
    assert store.get(parked_sid)["status"] == "deferred", "an attendance deferral keeps its own rule"
    assert store.get(flaky_sid)["status"] == "watching", "a card with no parents has nothing to finish"
    assert store.get(queued_sid)["status"] == "watching", "a card that is not parked runs on its own"
    assert not {parked_sid, flaky_sid, queued_sid} & set(result["reopened"])


ADMISSION_OUTCOME = {
    "enabled": True,
    "owner": "planner",
    "release_surface": "production",
    "collector": "Read the release report",
    "review_by": None,
    "target_date": None,
}


def _items(root, cycle_id, suffix="measure"):
    project = ProjectSettings(id="demo", label="Demo", root=root)
    candidates = review_collect.collect_project(project, today=TODAY)["candidates"]
    return [c for c in candidates if c["key"] == f"cycle:demo:{cycle_id}:{suffix}"]


def test_admission_asks_for_the_measurement_plan_before_delivery(root):
    # The reported defect: a cycle was admitted and shipped with no metric/source/baseline and nothing asked until its
    # release was verified. Admission itself must ask, in its own item.
    write(
        root / "in-demo" / "cycles" / "in-demo-c06.md",
        cycle_md("in-demo-c06", stage="shipping", decisions=DECIDED, measurement={"success_criteria": "clicks +10%"}),
    )
    (gate,) = _items(root, "in-demo-c06")
    assert gate["basis"]["reasons"] == ["measurement_missing"]
    # Criteria are set, so a null window is the legitimate spot-check case; the metric three are not.
    assert all(
        any(field in error for error in gate["basis"]["measurement_errors"])
        for field in ("primary_metric", "source", "baseline")
    )
    assert "Admission needs a measurement plan" in gate["context"] and "set_cycle_fields" in gate["context"]


def test_admission_spot_check_contract_is_not_asked_for(root):
    measurement = {k: v for k, v in MEASUREMENT.items() if k != "observation_window"}
    measurement["observation_window"] = None
    write(
        root / "in-demo" / "cycles" / "in-demo-c07.md",
        cycle_md("in-demo-c07", stage="decided", decisions=DECIDED, measurement=measurement),
    )
    assert _items(root, "in-demo-c07") == []


def test_admission_keeps_the_card_gate_basis_exact(root):
    # Settle rules match the card gate's reasons strictly (['cards_done'], ['no_cards']); the admission ask must not
    # widen them, or plan_after_cards_done and defer_behind_focus_cycle refuse every item.
    write(
        root / "in-demo" / "cycles" / "in-demo-c09.md",
        cycle_md("in-demo-c09", stage="decided", decisions=DECIDED),
    )
    (gate,) = _items(root, "in-demo-c09", suffix="gate")
    assert gate["basis"]["reasons"] == ["no_cards"]
    (measure,) = _items(root, "in-demo-c09")
    assert measure["basis"]["reasons"] == ["measurement_missing"]


def test_admission_asks_even_when_the_outcome_review_is_enabled(root):
    # An outcome-enabled cycle with nothing to assess yet used to be skipped by the collector entirely.
    write(
        root / "in-demo" / "cycles" / "in-demo-c08.md",
        cycle_md(
            "in-demo-c08",
            stage="shipping",
            decisions=DECIDED,
            outcome_review=ADMISSION_OUTCOME,
            measurement={"success_criteria": "clicks +10%"},
        ),
    )
    (gate,) = _items(root, "in-demo-c08")
    assert gate["basis"]["reasons"] == ["measurement_missing"]
