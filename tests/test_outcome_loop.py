"""Closed-loop outcome assessment: the input epoch, readiness, and dispatch against a real isolated board.

``derive`` is exercised as a pure function of dicts. ``dispatch`` runs the real ``hermes kanban`` CLI against the
board conftest pins under ``tmp_path`` (HERMES_HOME and HERMES_KANBAN_HOME), so the idempotency key, the link
write and the read-back are proven end to end and nothing touches a live board.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import shlex
import sys
from pathlib import Path

import pytest
from conftest import cycle_md, write

from hermes_initiatives_core import outcome_loop
from hermes_initiatives_core.docs import dump_markdown, parse_markdown
from hermes_initiatives_core.kanban import cycle_markers, read_cards
from hermes_initiatives_core.settings import ProjectSettings, Settings

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

TODAY = dt.date(2026, 10, 23)
DUE = dt.date(2026, 10, 30)  # after the 28-day window that starts on the 2026-10-01 release
LATER = dt.date(2026, 11, 20)
MEASUREMENT = {
    "primary_metric": "clicks",
    "source": "GSC",
    "baseline": "Sep 2026",
    "observation_window": {"starts": "verified_release", "length_days": 28},
    "success_criteria": "clicks +10%",
    "guardrails": "CTR not down",
    "insufficient_data": "fewer than 100 impressions",
}
STATES = {
    "disabled",
    "delivery_open",
    "waiting",
    "assessment_needed",
    "assessment_running",
    "assessment_failed",
    "review_ready",
    "source_unavailable",
}


def contract(**overrides) -> dict:
    data = {
        "enabled": True,
        "owner": "planner",
        "release_surface": "production",
        "collector": "Run the GSC query for the release week and compare clicks with the baseline week.",
        "review_by": "2026-11-30",
        "target_date": None,
    }
    data.update(overrides)
    return data


def cycle_frontmatter(cycle_id: str = "in-demo-c01", **overrides) -> dict:
    fm = {
        "title": f"Cycle {cycle_id}",
        "stage": "measuring",
        "decisions": [{"date": "2026-09-01", "by": "user", "decision": "Run it"}],
        "kanban_cards": [],
        "measurement": dict(MEASUREMENT),
        "result": {"verdict": "unset", "evidence": []},
        "disposition": {"action": "unset", "follow_ups": []},
        "outcome_review": contract(),
    }
    fm.update(overrides)
    return fm


def card(card_id: str = "t_00000001", status: str = "done", completed: str | None = "2026-10-10", **extra) -> dict:
    return {"id": card_id, "state": "fresh", "status": status, "completed_at": completed, **extra}


def release(date: str = "2026-10-01") -> dict:
    return {"at": f"{date}T09:00:00Z", "kind": "release_verified", "evidence": "/reports/release.md", "evidence_date": date}


def receipt(epoch: str, status: str = "met", at: str = "2026-10-20", **overrides) -> dict:
    row = {
        "at": f"{at}T10:00:00Z",
        "kind": "assessment",
        "epoch": epoch,
        "status": status,
        "evidence": "/reports/report.md",
        "proposal": "/reports/proposal.md",
        "detail": "measured clicks over four weeks",
    }
    row.update(overrides)
    return row


def assessment_card(epoch: str, card_id: str = "t_0000000a", status: str = "doing", title: str | None = None) -> dict:
    return {
        "id": card_id,
        "state": "fresh",
        "status": status,
        "completed_at": None,
        "title": title if title is not None else f"Assess outcomes of in-demo-c01 [epoch {epoch}]",
    }


# --- pure derive --------------------------------------------------------------------------------------------


def test_without_a_contract_the_loop_is_disabled():
    state = outcome_loop.derive(cycle_frontmatter(outcome_review=None), [], [card()], [], TODAY)
    assert state["enabled"] is False
    assert state["state"] == "disabled"
    assert state["epoch"] is None


def test_derive_returns_the_agreed_contract_and_never_mutates_its_inputs():
    fm = cycle_frontmatter()
    before = copy.deepcopy(fm)
    state = outcome_loop.derive(fm, [release()], [card()], [], DUE)
    assert fm == before
    assert set(state) == {
        "enabled",
        "epoch",
        "state",
        "due_date",
        "reasons",
        "assessment",
        "assessment_card",
        "contract_gaps",
        "next_action",
    }
    assert state["state"] in STATES
    assert isinstance(state["reasons"], list) and all(isinstance(reason, str) for reason in state["reasons"])


def test_contract_gaps_name_every_missing_piece():
    fm = cycle_frontmatter(outcome_review={"enabled": True}, measurement={})
    state = outcome_loop.derive(fm, [], [], [], TODAY)
    gaps = state["contract_gaps"]
    assert "outcome_review.owner missing" in gaps
    assert "outcome_review.release_surface invalid: missing" in gaps
    assert "outcome_review.collector missing" in gaps
    assert "measurement.success_criteria missing" in gaps
    assert "outcome_review.review_by and target_date both missing" in gaps
    assert any(reason.startswith("contract_gap: measurement.success_criteria") for reason in state["reasons"])


def test_a_future_window_waits_and_a_matured_window_asks_for_the_assessment():
    fm = cycle_frontmatter()
    rows = [release("2026-10-01")]
    before = outcome_loop.derive(fm, rows, [card()], [], TODAY)
    assert before["state"] == "waiting"
    assert before["due_date"] == "2026-10-29"
    assert "waiting_for: 2026-10-29" in before["reasons"]
    after = outcome_loop.derive(fm, rows, [card()], [], dt.date(2026, 10, 29))
    assert after["state"] == "assessment_needed"
    assert "window_ended: 2026-10-29" in after["reasons"]


def test_window_end_is_not_delayed_by_a_later_target_or_review_date():
    fm = cycle_frontmatter(outcome_review=contract(review_by="2026-11-15", target_date="2026-12-01"))
    rows = [release("2026-10-01")]
    due = outcome_loop.derive(fm, rows, [card()], [], dt.date(2026, 11, 10))
    assert due["state"] == "assessment_needed"
    assert due["due_date"] == "2026-10-29"
    assert "window_ended: 2026-10-29" in due["reasons"]


def test_future_window_without_delivery_cards_or_review_date_waits_until_mature():
    fm = cycle_frontmatter(outcome_review=contract(review_by=None))
    before = outcome_loop.derive(fm, [release()], [], [], TODAY)
    assert before["state"] == "waiting"
    assert before["due_date"] == "2026-10-29"
    assert outcome_loop.derive(fm, [release()], [], [], DUE)["state"] == "assessment_needed"
    judged = {**fm, "result": {"verdict": "win"}}
    assert outcome_loop.derive(judged, [release()], [], [], TODAY)["state"] == "waiting"
    assert outcome_loop.derive(judged, [release()], [], [], DUE)["state"] == "review_ready"


def test_early_deadline_receipt_cannot_discharge_mature_measurement():
    fm = cycle_frontmatter(outcome_review=contract(review_by="2026-10-20"))
    early = outcome_loop.derive(fm, [release()], [card()], [], TODAY)
    assert early["state"] == "assessment_needed"
    rows = [release(), receipt(early["epoch"], at="2026-10-23", status="insufficient_data")]
    assert outcome_loop.derive(fm, rows, [card()], [], TODAY)["state"] == "review_ready"
    mature = outcome_loop.derive(fm, rows, [card()], [], DUE)
    assert mature["state"] == "assessment_needed"
    assert mature["epoch"] != early["epoch"]
    assert outcome_loop.derive(fm, rows, [card()], [], LATER)["epoch"] == mature["epoch"]


def test_missing_dates_do_not_fabricate_a_review_deadline():
    fm = cycle_frontmatter(measurement={}, outcome_review=contract(review_by=None))
    state = outcome_loop.derive(fm, [], [], [], TODAY)
    assert state["state"] == "assessment_needed"
    assert state["due_date"] is None


def test_open_delivery_is_delivery_open_and_the_review_deadline_still_triggers():
    fm = cycle_frontmatter()
    open_card = card("t_00000002", status="doing", completed="2026-10-10")
    state = outcome_loop.derive(fm, [], [card(), open_card], [], TODAY)
    assert state["state"] == "delivery_open"
    assert "delivery_open: 1 of 2 cards open" in state["reasons"]
    forced = outcome_loop.derive(cycle_frontmatter(outcome_review=contract(review_by="2026-10-20")), [], [open_card], [], TODAY)
    assert forced["state"] == "assessment_needed"
    assert "review_by_passed: 2026-10-20" in forced["reasons"]


def test_exploring_plans_delivery_and_is_never_dispatched_as_an_assessment():
    state = outcome_loop.derive(cycle_frontmatter(stage="exploring", kanban_cards=[]), [], [], [], TODAY)
    assert state["state"] == "delivery_open"
    assert "exploring" in state["reasons"]
    assert state["next_action"] == "Plan the delivery cards for this cycle"
    overdue = outcome_loop.derive(
        cycle_frontmatter(stage="exploring", outcome_review=contract(review_by="2026-10-01")), [], [], [], TODAY
    )
    assert overdue["state"] == "delivery_open", "exploration owes a plan, not an outcome assessment"
    assert "review_by_passed: 2026-10-01" in overdue["reasons"]


def test_a_missing_delivery_card_fails_closed():
    listed = cycle_frontmatter(kanban_cards=["t_00000001"])
    state = outcome_loop.derive(listed, [], [{"id": "t_00000001", "state": "not_found"}], [], TODAY)
    assert state["state"] == "source_unavailable"
    assert any("t_00000001" in reason for reason in state["reasons"])

    unread = outcome_loop.derive(listed, [], [], [], TODAY)
    assert unread["state"] == "source_unavailable", "a listed card that was not read is not evidence"


def test_a_missing_assessment_card_fails_closed():
    fm = cycle_frontmatter(assessment_cards=["t_0000000a"])
    state = outcome_loop.derive(fm, [], [card()], [{"id": "t_0000000a", "state": "unavailable"}], TODAY)
    assert state["state"] == "source_unavailable"


def test_a_receipt_needs_evidence_a_proposal_and_the_current_epoch():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card()])
    cases = [
        receipt(epoch, proposal=""),
        receipt(epoch, evidence=""),
        receipt("0123456789ab"),
        receipt(epoch, status="looking_good"),
        {"at": "2026-10-20T10:00:00Z", "kind": "note", "epoch": epoch, "evidence": "/r.md", "proposal": "/p.md"},
    ]
    for row in cases:
        assert outcome_loop.derive(fm, [release(), row], [card()], [], DUE)["state"] == "assessment_needed", row
    good = receipt(epoch, status="missed")
    state = outcome_loop.derive(fm, [release(), good], [card()], [], DUE)
    assert state["state"] == "review_ready"
    assert state["assessment"]["status"] == "missed"
    assert state["assessment"]["evidence"] == "/reports/report.md"
    assert state["assessment"]["proposal"] == "/reports/proposal.md"


def test_a_receipt_from_before_the_last_card_finished_is_history():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card(completed="2026-10-10")])
    early = receipt(epoch, at="2026-10-05")
    state = outcome_loop.derive(fm, [release(), early], [card(completed="2026-10-10")], [], DUE)
    assert state["state"] == "assessment_needed", "the receipt describes a delivery that was not finished yet"
    late = receipt(epoch, at="2026-10-12")
    assert outcome_loop.derive(fm, [release(), late], [card(completed="2026-10-10")], [], DUE)["state"] == "review_ready"


def test_an_active_current_epoch_card_is_running():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card()])
    state = outcome_loop.derive(fm, [release()], [card()], [assessment_card(epoch)], DUE)
    assert state["state"] == "assessment_running"
    assert state["assessment_card"] == "t_0000000a"


def test_running_interim_card_retains_ownership_after_window_maturity():
    fm = cycle_frontmatter(outcome_review=contract(review_by="2026-10-20"))
    early = outcome_loop.derive(fm, [release()], [card()], [], TODAY)
    working = assessment_card(early["epoch"])
    mature = outcome_loop.derive(fm, [release()], [card()], [working], DUE)
    assert mature["state"] == "assessment_running"
    assert mature["assessment_card"] == working["id"]
    assert mature["epoch"] != early["epoch"]


def test_a_finished_card_without_a_receipt_is_a_failure_not_a_replacement():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card()])
    done = assessment_card(epoch, status="done")
    state = outcome_loop.derive(fm, [release()], [card()], [done], DUE)
    assert state["state"] == "assessment_failed"
    assert "assessment_done_without_receipt: t_0000000a" in state["reasons"]
    assert "no replacement card is created" in state["next_action"]


def test_a_card_from_another_epoch_is_history():
    fm = cycle_frontmatter()
    stale = assessment_card("000000000000", status="done")
    state = outcome_loop.derive(fm, [release()], [card()], [stale], DUE)
    assert state["state"] == "assessment_needed"
    assert state["assessment_card"] is None


def test_a_receipt_before_the_window_matures_is_visible_but_not_review_ready():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card()])
    state = outcome_loop.derive(fm, [release(), receipt(epoch, at="2026-10-24")], [card()], [], TODAY)
    assert state["state"] == "waiting", "no early review while the observation window is still running"
    assert "assessment_receipt_early: met" in state["reasons"]
    matured = outcome_loop.derive(fm, [release(), receipt(epoch, at="2026-10-24")], [card()], [], DUE)
    assert matured["state"] == "review_ready"


def test_a_verdict_with_a_terminal_disposition_closes_the_loop():
    for action in ("stop", "iterate", "continue"):
        fm = cycle_frontmatter(result={"verdict": "win", "evidence": []}, disposition={"action": action, "follow_ups": []})
        state = outcome_loop.derive(fm, [release()], [card()], [], DUE)
        assert state["state"] == "disabled", action
        assert f"cycle_concluded: verdict win, disposition {action}" in state["reasons"]
    concluded = outcome_loop.derive(cycle_frontmatter(stage="concluded"), [release()], [card()], [], DUE)
    assert concluded["state"] == "disabled"
    assert "cycle_closed: stage concluded" in concluded["reasons"]


def test_a_verdict_awaiting_evidence_keeps_the_loop_and_the_disposition_does_not():
    await_fm = cycle_frontmatter(result={"verdict": "win", "evidence": []}, disposition={"action": "await_evidence"})
    state = outcome_loop.derive(await_fm, [release()], [card()], [], TODAY)
    assert state["state"] == "waiting", "the user asked for more data, so the new window governs"
    assert "await_evidence" in state["reasons"]
    assert outcome_loop.derive(await_fm, [release()], [card()], [], DUE)["state"] == "assessment_needed"

    judged = cycle_frontmatter(result={"verdict": "win", "evidence": []})
    pending_disposition = outcome_loop.derive(judged, [release()], [card()], [], DUE)
    assert pending_disposition["state"] == "review_ready"
    assert "verdict_recorded: win" in pending_disposition["reasons"]
    assert pending_disposition["assessment"] is None


def test_the_epoch_ignores_what_the_worker_observes_and_records():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card()])
    rows = [
        release("2026-10-01"),
        {"at": "2026-10-02T10:00:00Z", "kind": "metric", "metric": "clicks", "value": 12, "evidence": "/r/x.md"},
        {"at": "2026-10-03T10:00:00Z", "kind": "note", "detail": "looked at the dashboard"},
        receipt(epoch),
    ]
    running = [assessment_card(epoch, status="done")]
    state = outcome_loop.derive(fm, rows, [card()], running, DUE)
    assert state["epoch"] == epoch, "the worker's own release check, metrics and receipt keep the epoch"
    assert outcome_loop.input_epoch({**fm, "assessment_cards": ["t_0000000a"]}, [card()]) == epoch


def test_the_epoch_moves_when_an_input_changes():
    fm = cycle_frontmatter()
    base = outcome_loop.input_epoch(fm, [card()])
    assert outcome_loop.input_epoch(fm, [card(completed="2026-10-11")]) != base, "a later completion is new delivery"
    assert outcome_loop.input_epoch(fm, [card(status="archived")]) == base, "archiving a finished card changes nothing"
    assert outcome_loop.input_epoch(fm, [card(status="doing", completed=None)]) != base
    decisions = [*fm["decisions"], {"date": "2026-10-20", "by": "user", "decision": "Extend the window"}]
    assert outcome_loop.input_epoch({**fm, "decisions": decisions}, [card()]) != base
    assert outcome_loop.input_epoch({**fm, "measurement": {**MEASUREMENT, "success_criteria": "+20%"}}, [card()]) != base
    assert outcome_loop.input_epoch({**fm, "outcome_review": contract(review_by="2026-12-15")}, [card()]) != base
    assert outcome_loop.input_epoch({**fm, "stage": "shipping"}, [card()]) != base
    assert outcome_loop.input_epoch({**fm, "result": {"verdict": "win", "evidence": []}}, [card()]) != base
    assert outcome_loop.input_epoch({**fm, "disposition": {"action": "stop", "follow_ups": []}}, [card()]) != base


def test_a_card_is_matched_to_its_epoch_by_its_body_not_by_list_order():
    fm = cycle_frontmatter()
    epoch = outcome_loop.input_epoch(fm, [card()])
    body = f"Assessment cycle: in-demo-c01\nEpoch: {epoch}\nMode: outcome\n"
    by_body = {
        "id": "t_0000000b",
        "state": "fresh",
        "status": "doing",
        "completed_at": None,
        "body": body,
        "title": "Assess outcomes of in-demo-c01 [epoch 000000000000]",
    }
    state = outcome_loop.derive(fm, [release()], [card()], [by_body], DUE)
    assert state["state"] == "assessment_running"
    assert state["assessment_card"] == "t_0000000b", "the body's epoch wins over a stale title"


# --- dispatch against a real, isolated board -----------------------------------------------------------------


@pytest.fixture
def env(isolated_hermes, root, tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "hermes"
    write(cli, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m hermes_cli.main "$@"\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    kbc.init_db(board="default")
    return {
        "root": root,
        "settings": Settings(projects=[ProjectSettings(id="demo", label="Demo", root=root, kanban_board="default")]),
    }


def _cycle(root: Path, cycle_id: str = "in-demo-c01", **overrides) -> Path:
    initiative = cycle_id.rsplit("-c", 1)[0]
    return write(root / initiative / "cycles" / f"{cycle_id}.md", cycle_md(cycle_id, **cycle_frontmatter(cycle_id, **overrides)))


def _board():
    return kbc.connect(board="default")


def _delivery_card(title: str = "Deliver the change") -> str:
    conn = _board()
    try:
        created = kb.create_task(conn, title=title, assignee="coder")
        kb.complete_task(conn, created, result="shipped", force=True)
        return created
    finally:
        conn.close()


def _open_delivery_card(title: str = "Still building") -> str:
    conn = _board()
    try:
        return kb.create_task(conn, title=title, assignee="coder")
    finally:
        conn.close()


def _assessment_cards() -> list:
    conn = _board()
    try:
        return [
            task
            for task in kb.list_tasks(conn, include_archived=True)
            if task.title.startswith(("Assess outcomes of", "Assess the contract for"))
        ]
    finally:
        conn.close()


def _card(card_id: str):
    conn = _board()
    try:
        return kb.get_task(conn, card_id)
    finally:
        conn.close()


def _frontmatter(path: Path) -> dict:
    return parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter


def test_dispatch_creates_one_card_links_it_and_leaves_delivery_alone(env):
    delivery = _delivery_card()
    path = _cycle(env["root"], kanban_cards=[delivery])
    result = outcome_loop.dispatch(env["settings"], today=DUE)

    assert result["errors"] == [] and result["pending"] == []
    assert len(result["created"]) == 1
    entry = result["created"][0]
    assert set(entry) == {"project", "cycle", "epoch", "state", "card", "linked", "reused"}
    assert entry["project"] == "demo" and entry["cycle"] == "in-demo-c01" and entry["linked"] is True
    assert entry["reused"] is False
    assert len(entry["epoch"]) == 12 and entry["card"].startswith("t_")

    created = _card(entry["card"])
    assert created.assignee == "planner"
    assert "Assessment cycle: in-demo-c01" in created.body
    assert "Initiative cycle:" not in created.body, "an assessment card must never read as delivery"
    markers = cycle_markers("default")
    assert markers["state"] == "fresh"
    assert entry["card"] not in [card for cards in markers["markers"].values() for card in cards]

    assert _frontmatter(path)["assessment_cards"] == [entry["card"]]
    assert _frontmatter(path)["kanban_cards"] == [delivery]
    assert _card(delivery).status == "done"


def test_a_second_tick_creates_nothing(env):
    _cycle(env["root"], kanban_cards=[_delivery_card()])
    first = outcome_loop.dispatch(env["settings"], today=DUE)
    second = outcome_loop.dispatch(env["settings"], today=DUE)
    assert len(first["created"]) == 1
    assert second["created"] == []
    assert len(_assessment_cards()) == 1


def test_dispatch_caps_active_assessments_per_project(env):
    for number in range(1, 5):
        _cycle(env["root"], f"in-demo-c0{number}", kanban_cards=[_delivery_card(f"Deliver {number}")])
    result = outcome_loop.dispatch(env["settings"], today=DUE, max_active=3)
    assert len(result["created"]) == 3
    assert [pending["reason"] for pending in result["pending"]].count("cap") == 1
    assert len(_assessment_cards()) == 3
    cap = next(p for p in result["pending"] if p["reason"] == "cap")
    assert set(cap["active_cards"]) == {entry["card"] for entry in result["created"]}
    capped = outcome_loop.dispatch(env["settings"], today=DUE, max_active=0)
    assert capped["created"] == []
    assert [pending["reason"] for pending in capped["pending"]] == ["cap"], "only the cycle still owing a card is capped"
    assert [pending["cycle"] for pending in capped["pending"]] == ["in-demo-c04"]


def test_dispatch_refuses_to_guess_a_missing_owner(env):
    settings = Settings(
        projects=[ProjectSettings(id="demo", label="Demo", root=env["root"], kanban_board="default", plan_assignee="planner")]
    )
    _cycle(env["root"], kanban_cards=[_delivery_card()], outcome_review=contract(owner=None))
    result = outcome_loop.dispatch(settings, today=DUE)
    assert result["created"] == []
    assert any("outcome_review.owner missing" in error for error in result["errors"])
    assert [pending["reason"] for pending in result["pending"]] == ["owner_missing"]
    assert _assessment_cards() == [], "plan_assignee is not a fallback for the contract owner"


def test_dispatch_refuses_an_unknown_release_surface(env):
    _cycle(env["root"], kanban_cards=[_delivery_card()], outcome_review=contract(release_surface="staging"))
    result = outcome_loop.dispatch(env["settings"], today=DUE)
    assert result["created"] == []
    assert any("release_surface 'staging'" in error for error in result["errors"])
    assert [pending["reason"] for pending in result["pending"]] == ["release_surface_invalid"]


def test_dispatch_fails_closed_when_the_board_is_unreadable(env):
    settings = Settings(projects=[ProjectSettings(id="demo", label="Demo", root=env["root"], kanban_board="not-a-board")])
    _cycle(env["root"], kanban_cards=[_delivery_card()])
    result = outcome_loop.dispatch(settings, today=DUE)
    assert result["created"] == []
    assert any("Kanban unavailable" in error for error in result["errors"])
    assert [pending["reason"] for pending in result["pending"]] == ["source_unavailable"]


def test_dispatch_leaves_exploring_disabled_paused_and_closed_cycles_alone(env):
    _cycle(env["root"], "in-demo-c01", stage="exploring", kanban_cards=[])
    _cycle(env["root"], "in-demo-c02", kanban_cards=[_delivery_card("Deliver two")], outcome_review={"enabled": False})
    _cycle(
        env["root"],
        "in-demo-c03",
        kanban_cards=[_delivery_card("Deliver three")],
        result={"verdict": "win", "evidence": []},
        disposition={"action": "iterate", "follow_ups": []},
    )
    write(
        env["root"] / "in-paused" / "initiative.md",
        """
        ---
        id: in-paused
        title: Paused initiative
        status: paused
        rank: 2
        goal: Nothing
        ---
        Paused brief.
    """,
    )
    _cycle(env["root"], "in-paused-c01", kanban_cards=[_delivery_card("Deliver four")])
    result = outcome_loop.dispatch(env["settings"], today=DUE)
    assert result["created"] == []
    assert result["errors"] == []
    assert _assessment_cards() == []


def test_linking_appends_without_moving_a_user_field(env):
    path = _cycle(env["root"], kanban_cards=[_delivery_card()])
    before = _frontmatter(path)
    result = outcome_loop.dispatch(env["settings"], today=DUE)
    card_id = result["created"][0]["card"]
    after = _frontmatter(path)
    for key in before:
        if key == "assessment_cards":
            continue
        assert after.get(key) == before[key], key
    assert after["assessment_cards"] == [card_id]
    assert outcome_loop.link_assessment_card(env["root"], "in-demo-c01", card_id) == [card_id]
    assert len(_assessment_cards()) == 1


def test_a_lost_link_is_recovered_by_the_body_marker(env):
    path = _cycle(env["root"], kanban_cards=[_delivery_card()])
    first = outcome_loop.dispatch(env["settings"], today=DUE)
    card_id = first["created"][0]["card"]
    document = parse_markdown(path.read_text(encoding="utf-8"), path)
    path.write_text(dump_markdown({**document.frontmatter, "assessment_cards": []}, document.body), encoding="utf-8")

    recovered = outcome_loop.dispatch(env["settings"], today=DUE)
    assert [entry["card"] for entry in recovered["created"]] == [card_id], "the marker adopts the orphan card"
    assert recovered["created"][0]["reused"] is True
    assert _frontmatter(path)["assessment_cards"] == [card_id]
    assert len(_assessment_cards()) == 1


def test_a_lost_link_and_marker_is_recovered_by_the_idempotency_key(env):
    path = _cycle(env["root"], kanban_cards=[_delivery_card()])
    card_id = outcome_loop.dispatch(env["settings"], today=DUE)["created"][0]["card"]
    document = parse_markdown(path.read_text(encoding="utf-8"), path)
    path.write_text(dump_markdown({**document.frontmatter, "assessment_cards": []}, document.body), encoding="utf-8")
    conn = _board()
    try:
        assert kb.edit_task(conn, card_id, body="Body edited by hand: the marker is gone.")
    finally:
        conn.close()

    recovered = outcome_loop.dispatch(env["settings"], today=DUE)
    assert [entry["card"] for entry in recovered["created"]] == [card_id], "the epoch key returns the same card"
    assert recovered["created"][0]["reused"] is False
    assert _frontmatter(path)["assessment_cards"] == [card_id]
    assert len(_assessment_cards()) == 1


def test_unlinked_marker_cards_are_adopted_and_count_against_the_cap(env):
    for number in range(1, 5):
        _cycle(env["root"], f"in-demo-c0{number}", kanban_cards=[_delivery_card(f"Deliver {number}")])
    orphan_path = env["root"] / "in-demo" / "cycles" / "in-demo-c04.md"
    orphan_fm = _frontmatter(orphan_path)
    delivery_row = read_cards("default", [orphan_fm["kanban_cards"][0]])["cards"][orphan_fm["kanban_cards"][0]]
    epoch = outcome_loop.input_epoch(orphan_fm, [delivery_row])
    conn = _board()
    try:
        orphan = kb.create_task(
            conn,
            title=f"Assess outcomes of in-demo-c04 [epoch {epoch}]",
            assignee="planner",
            body=f"Assessment cycle: in-demo-c04\nEpoch: {epoch}\nMode: outcome\n",
        )
    finally:
        conn.close()

    result = outcome_loop.dispatch(env["settings"], today=DUE, max_active=3)
    adopted = [entry for entry in result["created"] if entry["reused"]]
    assert [(entry["cycle"], entry["card"]) for entry in adopted] == [("in-demo-c04", orphan)]
    assert len(result["created"]) == 3, "the adopted card already holds one of the three slots"
    assert [pending["cycle"] for pending in result["pending"] if pending["reason"] == "cap"] == ["in-demo-c03"]
    assert len(_assessment_cards()) == 3
    assert _frontmatter(orphan_path)["assessment_cards"] == [orphan]


def test_a_done_assessment_card_without_a_receipt_is_not_replaced(env):
    _cycle(env["root"], kanban_cards=[_delivery_card()])
    first = outcome_loop.dispatch(env["settings"], today=DUE)
    card_id = first["created"][0]["card"]
    conn = _board()
    try:
        assert kb.complete_task(conn, card_id, result="report at /reports/x.md", force=True)
    finally:
        conn.close()
    again = outcome_loop.dispatch(env["settings"], today=DUE + dt.timedelta(days=1))
    assert again["created"] == []
    assert len(_assessment_cards()) == 1, "no replacement loop: a missing receipt is surfaced, not retried"


def test_missing_contract_pieces_get_one_diagnostic_assessment_while_delivery_is_open(env):
    delivery = _open_delivery_card()
    _cycle(
        env["root"],
        kanban_cards=[delivery],
        measurement={"primary_metric": "clicks", "source": "GSC"},
        outcome_review=contract(review_by=None, target_date=None),
    )
    result = outcome_loop.dispatch(env["settings"], today=TODAY)
    assert result["errors"] == []
    assert len(result["created"]) == 1
    created = _card(result["created"][0]["card"])
    assert "Mode: contract" in created.body
    assert "insufficient_data" in created.body
    assert "Assessment cycle: in-demo-c01" in created.body
    assert _card(delivery).status != "done", "the diagnostic card never holds delivery"
    assert outcome_loop.dispatch(env["settings"], today=TODAY)["created"] == []


def test_a_cycle_with_no_delivery_record_and_no_deadline_gets_a_diagnostic_card(env):
    _cycle(env["root"], kanban_cards=[], outcome_review=contract(review_by=None, target_date=None))
    result = outcome_loop.dispatch(env["settings"], today=TODAY)
    assert len(result["created"]) == 1
    body = _card(result["created"][0]["card"]).body
    assert "No delivery card is linked to this cycle." in body
    assert "Mode: contract" in body


def test_the_card_body_carries_the_surface_the_report_paths_and_the_receipt_command(env):
    _cycle(
        env["root"],
        kanban_cards=[_delivery_card()],
        outcome_review=contract(release_surface="main_ci", collector="Compare the bundle size of the CI run on main."),
    )
    result = outcome_loop.dispatch(env["settings"], today=DUE)
    entry = result["created"][0]
    body = _card(entry["card"]).body
    assert f"Epoch: {entry['epoch']}" in body
    assert "main_ci" in body and "CI run on main" in body
    assert "record_assessment.py" in body
    assert "--epoch <epoch printed by --inspect>" in body
    assert "benefit, cost, risk, timeline" in body
    assert str(env["root"] / "in-demo" / "reports" / "in-demo-c01") in body
    assert "Write only new files." in body
    assert "Do not edit the cycle frontmatter" in body
    assert "Initiative cycle:" not in body
