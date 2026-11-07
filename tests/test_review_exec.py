"""Review executor: answers become real, read-back writes; stale and resolved sources are never written.

Kanban actions run the real ``hermes kanban`` CLI against the isolated test board (conftest pins
HERMES_HOME and HERMES_KANBAN_HOME under tmp_path).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest
from conftest import DECIDED, cycle_md, write

from hermes_initiatives_core import review_collect, review_exec, review_hook, reviews
from hermes_initiatives_core.docs import parse_markdown
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

TARGET = "telegram:123456789:4242"


@pytest.fixture
def env(isolated_hermes, root, tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "hermes"
    write(cli, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m hermes_cli.main "$@"\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    assert str(tmp_path) in os.environ["HERMES_KANBAN_HOME"], "Kanban must point at the test board"
    kbc.init_db(board="default")
    conn = kbc.connect(board="default")
    gate = kb.create_task(conn, title="Approve the removal matrix", assignee="coder")
    kb.block_task(conn, gate, reason="BLOCKED — NEEDS YOU — approve the matrix", kind="needs_input")
    conn.close()
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="exploring", hypothesis="H"))
    write(root / "in-demo" / "cycles" / "in-demo-c02.md", cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(
        json.dumps(
            {"projects": [{"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default"}], "review_target": TARGET}
        )
    )
    store = reviews.ReviewStore()
    project = load_settings()[0].projects[0]
    review_collect.sync(store, [project], gates={"default": {gate: {"id": gate, "waiting": "approve the matrix"}}})
    return {"store": store, "gate": gate, "root": root, "project": project}


def _offer(store, key, options, recommend="A"):
    item = store.get_by_key(key)
    store.add_version(item["short_id"], question="What now?", options=options, recommend=recommend, context="ctx")
    return item["short_id"]


def _card(card):
    conn = kbc.connect(board="default")
    try:
        return kb.get_task(conn, card).status, [c.body for c in kb.list_comments(conn, card)]
    finally:
        conn.close()


def _gate_options(card):
    return [
        {"id": "A", "label": "Approve as written", "action": "kanban_unblock", "args": {"board": "default", "card": card}},
        {"id": "B", "label": "Archive it", "action": "kanban_archive", "args": {"board": "default", "card": card}},
        {"id": "C", "label": "Merge now", "action": "pr_merge", "args": {}},
    ]


def test_parse_takes_only_messages_made_of_commands():
    assert review_exec.parse_message("D3 A") == [review_exec.Command("answer", "D3", option="A")]
    parsed = review_exec.parse_message("d3 b: only for HDB\nkeep rows 4-6\nconfirm D7\ndefer D9 until 2026-10-20")
    assert [(c.kind, c.short_id) for c in parsed] == [("answer", "D3"), ("confirm", "D7"), ("defer", "D9")]
    assert parsed[0].note == "only for HDB\nkeep rows 4-6" and parsed[2].until == "2026-10-20"
    assert review_exec.parse_message("defer D2 until change")[0].defer_kind == "state_change"
    for chat in ("ok thanks", "D3 is weird, why?", "D3 A\nand also check the CI", "D3 E", ""):
        assert review_exec.parse_message(chat) is None, chat


def test_unblock_comments_the_note_unchanged_and_reads_back(env):
    sid = _offer(env["store"], f"kanban:default:{env['gate']}:gate", _gate_options(env["gate"]))
    note = "Approve, but keep rows 4–6 as they are.\nCheck the stale inventory again."
    result = review_exec.Executor(env["store"]).submit(sid, "a", note=note, surface="telegram")
    assert result["state"] == "applied", result
    status, comments = _card(env["gate"])
    assert status not in ("blocked", "scheduled") and any(note in c for c in comments)
    assert env["store"].get(sid)["status"] == "applied"
    assert [a["state"] for a in env["store"].actions(sid)] == ["applying", "applied"]
    second = review_exec.Executor(env["store"]).submit(sid, "B", surface="tab")
    assert second["state"] == "refused" and _card(env["gate"])[0] != "archived"


def test_source_resolved_before_apply_writes_nothing(env):
    sid = _offer(env["store"], f"kanban:default:{env['gate']}:gate", _gate_options(env["gate"]))
    conn = kbc.connect(board="default")
    kb.unblock_task(conn, env["gate"])
    conn.close()
    result = review_exec.Executor(env["store"]).submit(sid, "A", note="go", surface="tab")
    # The card moved on, so its state no longer matches the options: nothing is applied. The recovery tick
    # drops it from the gate list and the next sync closes the item.
    assert result["state"] == "stale"
    assert not any("go" in c for c in _card(env["gate"])[1])
    conn = kbc.connect(board="default")
    kb.complete_task(conn, env["gate"], result="done")
    conn.close()
    env["store"].set_status(sid, "open")
    again = review_exec.Executor(env["store"]).submit(sid, "A", note="go", surface="tab")
    assert again["state"] in ("resolved_elsewhere", "refused"), again


def test_changed_source_is_stale_and_reopens(env):
    sid = _offer(env["store"], f"kanban:default:{env['gate']}:gate", _gate_options(env["gate"]))
    conn = kbc.connect(board="default")
    kb.unblock_task(conn, env["gate"])
    kb.block_task(conn, env["gate"], reason="BLOCKED — NEEDS YOU — a new question", kind="needs_input")
    conn.close()
    result = review_exec.Executor(env["store"]).submit(sid, "A", note="approve", surface="tab")
    assert result["state"] == "stale"
    status, comments = _card(env["gate"])
    assert status in ("blocked", "triage") and not any("approve" in c for c in comments)
    assert env["store"].get(sid)["status"] == "open"
    assert sid in {i["short_id"] for i in env["store"].needs_options()}


def test_irreversible_waits_for_confirm(env):
    sid = _offer(env["store"], f"kanban:default:{env['gate']}:gate", _gate_options(env["gate"]))
    executor = review_exec.Executor(env["store"])
    assert executor.submit(sid, "B", surface="telegram")["state"] == "awaiting_confirm"
    assert _card(env["gate"])[0] == "blocked"
    assert executor.confirm(sid)["state"] == "applied"
    assert _card(env["gate"])[0] == "archived"


def test_unsupported_merge_is_refused_before_recording(env):
    sid = _offer(env["store"], f"kanban:default:{env['gate']}:gate", _gate_options(env["gate"]))
    assert review_exec.Executor(env["store"]).submit(sid, "C", surface="tab")["state"] == "refused"
    assert env["store"].answers(sid) == [] and env["store"].get(sid)["status"] == "open"


def test_cycle_fields_and_decision_row_keep_the_rest_of_the_file(env):
    key = "cycle:demo:in-demo-c01:gate"
    sid = _offer(
        env["store"],
        key,
        [
            {
                "id": "A",
                "label": "Abandon c01",
                "action": "set_cycle_fields",
                "args": {
                    "cycle": "in-demo-c01",
                    "fields": {"stage": "abandoned", "disposition": {"action": "stop", "reason": "No data licence."}},
                },
            },
            {
                "id": "B",
                "label": "Keep exploring",
                "action": "record_decision",
                "args": {"cycle": "in-demo-c01", "decision": "Keep exploring"},
            },
        ],
    )
    path = env["root"] / "in-demo" / "cycles" / "in-demo-c01.md"
    result = review_exec.Executor(env["store"]).submit(sid, "A", note="No data licence.", surface="tab")
    assert result["state"] == "applied", result
    fm = parse_markdown(path.read_text(), path).frontmatter
    assert fm["stage"] == "abandoned" and fm["hypothesis"] == "H"
    assert fm["decisions"][-1] == {
        "date": fm["decisions"][-1]["date"],
        "by": "user",
        "decision": "Abandon c01",
        "review": sid,
        "note": "No data licence.",
    }


def test_applied_decision_does_not_come_back_until_the_source_changes(env):
    key = "cycle:demo:in-demo-c01:gate"
    sid = _offer(
        env["store"],
        key,
        [
            {
                "id": "A",
                "label": "Keep exploring",
                "action": "record_decision",
                "args": {"cycle": "in-demo-c01", "decision": "Keep exploring"},
            },
            {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "state_change"}},
        ],
    )
    assert review_exec.Executor(env["store"]).submit(sid, "A", surface="tab")["state"] == "applied"
    review_collect.sync(env["store"], [env["project"]], gates={"default": {}})
    assert env["store"].get_by_key(key) is None


def test_criteria_answer_sets_the_field_and_closes_the_gate(env):
    key = "criteria:demo:in-demo"
    sid = _offer(
        env["store"],
        key,
        [
            {
                "id": "A",
                "label": "c02 passes when the release is live",
                "action": "set_cycle_fields",
                "args": {"cycle": "in-demo-c02", "fields": {"success_criteria": "Released behind the flag"}},
            },
            {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "indefinite"}},
        ],
    )
    assert review_exec.Executor(env["store"]).submit(sid, "A", surface="tab")["state"] == "applied"
    path = env["root"] / "in-demo" / "cycles" / "in-demo-c02.md"
    fm = parse_markdown(path.read_text(), path).frontmatter
    assert fm["measurement"]["success_criteria"] == "Released behind the flag"
    assert fm["decisions"][-1]["by"] == "user"
    review_collect.sync(env["store"], [env["project"]], gates={"default": {}})
    assert env["store"].get_by_key(key) is None


def test_moving_to_measuring_without_the_fields_fails_and_leaves_the_cycle_unchanged(env):
    # D163 moved c02 to measuring with no observation_window and no release row; the cycle went invalid silently.
    key = "cycle:demo:in-demo-c01:gate"
    move = {
        "id": "A",
        "label": "Measure",
        "action": "set_cycle_fields",
        "args": {"cycle": "in-demo-c02", "fields": {"stage": "measuring"}},
    }
    sid = _offer(env["store"], key, [move, {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "indefinite"}}])
    path = env["root"] / "in-demo" / "cycles" / "in-demo-c02.md"
    before = path.read_text()
    result = review_exec.Executor(env["store"]).submit(sid, "A", surface="tab")
    assert result["state"] == "failed" and "measurement fields" in result["message"]
    assert path.read_text() == before, "a refused answer writes neither the stage nor a decision row"
    assert review_exec.cycle_fields_problems(env["project"], "in-demo-c02", {"stage": "measuring"})
    spot = {"stage": "measuring", "primary_metric": "m", "source": "s", "baseline": "b", "success_criteria": "c"}
    problems = review_exec.cycle_fields_problems(env["project"], "in-demo-c02", spot)
    assert problems == ["stage 'measuring' needs a release_verified observation"], problems


IDEA = """
---
id: {id}
title: Run the tool tests in CI
status: proposed
size: {size}
related_initiative: in-demo
problem: Four test files never run in CI.
expected_outcome: A broken runner fails CI.
evidence:
- notes/audit.md
source: backfill/demo
similar_to: []
created: '2026-10-11'
promoted_to: null
merged_into: null
review_on: null
reason: null
---
Body kept.
"""


def _idea(env, idea_id, size="task"):
    write(env["root"] / "ideas" / f"{idea_id}.md", IDEA.format(id=idea_id, size=size))
    review_collect.sync(env["store"], [env["project"]], gates={"default": {}})
    return env["store"].get_by_key(f"idea:demo:{idea_id}:triage")["short_id"]


def _idea_fm(env, idea_id):
    path = env["root"] / "ideas" / f"{idea_id}.md"
    return parse_markdown(path.read_text(encoding="utf-8"), path)


def _tasks():
    conn = kbc.connect(board="default")
    try:
        return {t.id: t for t in kb.list_tasks(conn)}
    finally:
        conn.close()


def _promote(idea_id, **extra):
    args = {"idea": idea_id, "status": "promoted", **extra}
    return [
        {"id": "A", "label": "Promote the idea so it gets a delivery card", "action": "idea_status", "args": args},
        {"id": "B", "label": "Park it", "action": "defer", "args": {}},
    ]


def test_promotion_creates_the_delivery_card_and_records_it(env):
    idea = "idea-20261011-run-the-tool-tests"
    sid = _idea(env, idea)
    _offer(env["store"], f"idea:demo:{idea}:triage", _promote(idea, assignee="planner"))
    before = set(_tasks())
    note = "Small, backend only.\nDo it after w01."
    result = review_exec.Executor(env["store"]).submit(sid, "A", note=note, surface="tab")
    assert result["state"] == "applied", result
    created = set(_tasks()) - before
    assert len(created) == 1, "exactly one delivery card"
    card = created.pop()
    task = _tasks()[card]
    assert task.assignee == "planner" and "Run the tool tests in CI" in task.title
    assert idea in task.body and note in task.body, "the card names the idea and carries the note unchanged"
    doc = _idea_fm(env, idea)
    assert doc.frontmatter["status"] == "promoted" and doc.frontmatter["promoted_to"] == card
    assert doc.body.strip() == "Body kept."
    assert card in result["message"]
    assert validate(load(env["root"]), check_sources=False).errors.get(idea) is None


def test_promotion_into_an_existing_cycle_creates_no_card(env):
    idea = "idea-20261011-fold-into-c01"
    sid = _idea(env, idea, size="cycle")
    _offer(env["store"], f"idea:demo:{idea}:triage", _promote(idea, into="in-demo-c01"))
    before = set(_tasks())
    assert review_exec.Executor(env["store"]).submit(sid, "A", surface="tab")["state"] == "applied"
    assert set(_tasks()) == before
    assert _idea_fm(env, idea).frontmatter["promoted_to"] == "in-demo-c01"


def test_promotion_without_a_destination_fails_and_leaves_the_idea_open(env):
    idea = "idea-20261011-nowhere-to-go"
    sid = _idea(env, idea)
    _offer(env["store"], f"idea:demo:{idea}:triage", _promote(idea, into="in-demo-c99"))
    result = review_exec.Executor(env["store"]).submit(sid, "A", surface="tab")
    assert result["state"] == "failed" and "in-demo-c99" in result["message"]
    fm = _idea_fm(env, idea).frontmatter
    assert fm["status"] == "proposed" and fm["promoted_to"] is None, "a failed promotion writes nothing"


def test_promotion_into_a_closed_cycle_fails_and_writes_nothing(env):
    idea = "idea-20261011-into-a-stopped-cycle"
    write(env["root"] / "in-demo" / "cycles" / "in-demo-c03.md", cycle_md("in-demo-c03", stage="abandoned", decisions=DECIDED))
    sid = _idea(env, idea)
    _offer(env["store"], f"idea:demo:{idea}:triage", _promote(idea, into="in-demo-c03"))
    result = review_exec.Executor(env["store"]).submit(sid, "A", surface="tab")
    assert result["state"] == "failed" and "abandoned" in result["message"]
    assert _idea_fm(env, idea).frontmatter["status"] == "proposed"


def test_repair_completes_a_promotion_applied_without_a_destination(env):
    idea = "idea-20261011-half-promoted"
    sid = _idea(env, idea)
    _offer(env["store"], f"idea:demo:{idea}:triage", _promote(idea))
    # The pre-fix executor: the answer was recorded and the status set, with no destination.
    answer = env["store"].answer(sid, "A", note="go", surface="tab")
    env["store"].set_status(sid, "applied", expect=("answered",))
    env["store"].log_action(
        answer["answer_id"], sid, "idea_status", {"idea": idea, "status": "promoted"}, "applied", receipt=f"{idea} -> promoted"
    )
    path = env["root"] / "ideas" / f"{idea}.md"
    path.write_text(path.read_text().replace("status: proposed", "status: promoted"))
    assert "needs promoted_to" in " ".join(validate(load(env["root"]), check_sources=False).errors.get(idea, []))

    executor = review_exec.Executor(env["store"])
    plan = executor.complete_promotions(env["project"], assignee="planner", dry_run=True)
    assert [(p["short_id"], p["idea"]) for p in plan] == [(sid, idea)] and _idea_fm(env, idea).frontmatter["promoted_to"] is None
    done = executor.complete_promotions(env["project"], assignee="planner")
    card = done[0]["promoted_to"]
    assert _idea_fm(env, idea).frontmatter["promoted_to"] == card and _tasks()[card].assignee == "planner"
    assert "go" in _tasks()[card].body, "the original note rides on the card"
    assert [a["option"] for a in env["store"].answers(sid)] == ["A"], "the original answer is kept, not re-asked"
    assert env["store"].actions(sid)[-1]["state"] == "applied" and card in env["store"].actions(sid)[-1]["receipt"]
    assert executor.complete_promotions(env["project"], assignee="planner") == [], "idempotent"
    assert validate(load(env["root"]), check_sources=False).errors.get(idea) is None


def test_handoff_waits_for_the_owner_ack(env):
    item, _ = env["store"].upsert_item(
        "req:calendar-sync:evt-1",
        section="gated",
        source="calendar-sync",
        title="NLB charge",
        basis={"e": 1},
        owner_job="a1b2c3d4e5f6",
    )
    sid = item["short_id"]
    _offer(
        env["store"],
        "req:calendar-sync:evt-1",
        [
            {"id": "A", "label": "Pay it", "action": "handoff", "args": {}},
            {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "indefinite"}},
        ],
    )
    assert (
        review_exec.Executor(env["store"]).submit(sid, "A", note="use the family card", surface="tab")["state"]
        == "ready_for_owner"
    )
    ready = env["store"].for_owner("calendar-sync")
    assert [(r["short_id"], r["note"]) for r in ready] == [(sid, "use the family card")]
    assert env["store"].acknowledge(sid, "paid S$28.09") and env["store"].get(sid)["status"] == "acked"
    assert env["store"].for_owner("calendar-sync") == []


def test_hook_takes_answers_only_in_the_review_topic(env):
    env["store"].upsert_item(
        "req:owner:topic-answer", section="gated", source="owner", title="Acknowledge the topic answer", basis={"event": 1}
    )
    sid = _offer(
        env["store"],
        "req:owner:topic-answer",
        [
            {"id": "A", "label": "Acknowledge", "action": "acknowledge", "args": {}},
            {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "indefinite"}},
        ],
    )
    topic = {"chat_id": "123456789", "thread_id": "4242", "user_id": "123456789"}
    assert review_hook.handle("telegram", topic, "1", "what is D3 about?") is None
    assert review_hook.handle("telegram", {**topic, "thread_id": "999"}, "2", f"{sid} A") is None
    # The gateway admitted this sender, but they are not the review approver: the answer is not theirs to give.
    assert review_hook.handle("telegram", {**topic, "user_id": "555"}, "2", f"{sid} A") is None
    assert env["store"].get(sid)["status"] != "applied"
    reply = review_hook.handle("telegram", topic, "3", f"{sid} A: ship it")
    assert reply["action"] == "handled" and reply["reply"].startswith(f"✅ {sid}")
    assert env["store"].get(sid)["status"] == "applied"
    answer = env["store"].answers(sid)[0]
    assert (answer["surface"], answer["option"], answer["note"]) == ("telegram", "A", "ship it")
    assert len(env["store"].answers(sid)) == 1


def _session_request(store, key="req:operator:ui-review", owner_job=None, project="demo"):
    item, _ = store.upsert_item(
        key,
        section="gated",
        source="request:operator",
        title="Which build do you review?",
        basis={"e": 1},
        blocks=["t_12345678"],
        project=project,
        owner_profile="operator",
        owner_job=owner_job,
    )
    _offer(
        store,
        key,
        [
            {"id": "A", "label": "Review the C1 build", "action": "handoff", "args": {}},
            {"id": "B", "label": "Later", "action": "defer", "args": {"kind": "indefinite"}},
        ],
    )
    return item["short_id"]


def test_session_handoff_gets_one_card_for_its_profile(env):
    from hermes_initiatives_core import review_handoff

    store = env["store"]
    sid = _session_request(store)
    config = load_settings()[0]
    assert review_handoff.dispatch(store, config) == {"created": [], "errors": []}, "unanswered: no card"
    assert review_exec.Executor(store).submit(sid, "A", note="go with the C1 build", surface="tab")["state"] == "ready_for_owner"

    first = review_handoff.dispatch(store, config)
    assert first["errors"] == [] and [c["short_id"] for c in first["created"]] == [sid]
    card = first["created"][0]["card"]
    conn = kbc.connect(board="default")
    try:
        task = kb.get_task(conn, card)
    finally:
        conn.close()
    assert task.assignee == "operator" and task.status in ("ready", "todo")
    assert "go with the C1 build" in task.body and "Review the C1 build" in task.body
    assert f"--ack {sid}" in task.body and "t_12345678" in task.body

    assert review_handoff.dispatch(store, config) == {"created": [], "errors": []}, "a later tick makes no second card"
    assert store.get(sid)["status"] == "ready_for_owner", "the card does not acknowledge for the owner"
    assert store.acknowledge(sid, "reviewed the C1 build") and store.get(sid)["status"] == "acked"


def test_handoff_to_a_job_or_unknown_board_makes_no_card(env):
    from hermes_initiatives_core import review_handoff

    store = env["store"]
    job = _session_request(store, key="req:operator:job", owner_job="a1b2c3d4e5f6")
    lost = _session_request(store, key="req:operator:lost", project="nowhere")
    by_board = _session_request(store, key="req:operator:board", project="default")
    for sid in (job, lost, by_board):
        assert review_exec.Executor(store).submit(sid, "A", surface="tab")["state"] == "ready_for_owner"
    out = review_handoff.dispatch(store, load_settings()[0])
    assert [c["short_id"] for c in out["created"]] == [by_board], "a project named by its board still gets its card"
    assert [e.split(":")[0] for e in out["errors"]] == [lost], "the job reads its own answers; the lost one is reported"
