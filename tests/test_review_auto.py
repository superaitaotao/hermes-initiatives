"""Auto items: the user hands one review item to the agents; the recovery tick applies, resolves, or returns it.

Kanban writes run the real ``hermes kanban`` CLI against the isolated test board (conftest pins HERMES_HOME and
HERMES_KANBAN_HOME under tmp_path).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import DECIDED, SUMMARY, cycle_md, write

from hermes_initiatives_core import review_auto, review_collect, review_exec, review_learn, review_write, reviews
from hermes_initiatives_core.docs import parse_markdown
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"
WHY = "The cycle file shows no newer decision; the recommended option matches the user's note."
ONE_OFF = {"skip": "one_off: the user's note was about this cycle only"}  # rule part of an auto entry
KEY = "cycle:demo:in-demo-c01:gate"


@pytest.fixture
def env(isolated_hermes, root, tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "hermes"
    write(cli, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m hermes_cli.main "$@"\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
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
        json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default"}]})
    )
    store = reviews.ReviewStore()
    project = load_settings()[0].projects[0]
    gates = {"default": {gate: {"id": gate, "waiting": "approve the matrix"}}}
    review_collect.sync(store, [project], gates=gates)
    return {"store": store, "gate": gate, "root": root, "project": project, "gates": gates}


def _cycle_options(store):
    sid = store.get_by_key(KEY)["short_id"]
    store.add_version(
        sid,
        question="What next for c01?",
        recommend="A",
        context="ctx",
        options=[
            {
                "id": "A",
                "label": "Keep exploring",
                "action": "record_decision",
                "args": {"cycle": "in-demo-c01", "decision": "Keep exploring"},
            },
            {"id": "B", "label": "Abandon c01", "action": "idea_reject", "args": {}},
            {"id": "C", "label": "Later", "action": "defer", "args": {"kind": "state_change"}},
        ],
    )
    return sid


def _fm(env, cycle="in-demo-c01"):
    path = env["root"] / "in-demo" / "cycles" / f"{cycle}.md"
    return parse_markdown(path.read_text(), path).frontmatter


def test_mark_auto_and_take_back_are_user_only_and_keep_the_options(env):
    store = env["store"]
    sid = _cycle_options(store)
    executor = review_exec.Executor(store)
    marked = executor.mark_auto(sid, "Keep it going unless the plan says stop.", surface="tab")
    assert marked["state"] == "auto", marked
    item = store.get(sid)
    assert item["status"] == "auto" and item["auto"]["note"] == "Keep it going unless the plan says stop."
    assert store.queue() == [] or sid not in [i["short_id"] for i in store.queue()], "auto items leave the queue"
    with pytest.raises(reviews.ReviewError, match="only the user"):
        store.mark_auto(sid, surface="agent")
    assert executor.unmark_auto(sid, surface="tab")["state"] == "open"
    item = store.get(sid)
    assert item["status"] == "open" and reviews.current(item), "the same options come back"
    assert sid not in {i["short_id"] for i in store.needs_options()}


def test_learn_and_urgent_items_cannot_be_marked_auto(env):
    store = env["store"]
    learn, _ = store.upsert_item("learn:rule:x", section="gated", source="learn", title="Rule?", basis={})
    urgent, _ = store.upsert_item(
        "request:j:1", section="gated", source="request:j", title="Consent", basis={}, urgent_rule="consent"
    )
    executor = review_exec.Executor(store)
    assert executor.mark_auto(learn["short_id"], surface="tab")["state"] == "refused"
    assert executor.mark_auto(urgent["short_id"], surface="tab")["state"] == "refused"
    assert store.get(learn["short_id"])["status"] == "open"


def test_topic_commands_mark_and_take_back(env):
    sid = _cycle_options(env["store"])
    commands = review_exec.parse_message(f"auto {sid}: keep exploring\nonly if nothing changed")
    assert commands == [review_exec.Command("auto", sid, note="keep exploring\nonly if nothing changed")]
    results = review_exec.Executor(env["store"]).run_commands(commands, surface="telegram")
    assert results[0]["state"] == "auto"
    assert env["store"].get(sid)["auto"]["note"] == "keep exploring\nonly if nothing changed"
    results = review_exec.Executor(env["store"]).run_commands(review_exec.parse_message(f"manual {sid}"), surface="telegram")
    assert results[0]["state"] == "open"


def test_agent_applies_an_option_with_delegation_and_agent_rows_and_settles(env):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, "Keep exploring.", surface="tab")
    [pending] = review_auto.pending(store)
    assert pending["short_id"] == sid and pending["options_current"] and pending["user_note"] == "Keep exploring."
    result = review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF})
    assert result["state"] == "settled", result
    item = store.get(sid)
    assert item["status"] == "settled" and item["closed_note"].startswith("auto_delegated: A: Keep exploring")
    rows = _fm(env)["decisions"][-2:]
    assert rows[0]["by"] == "user" and rows[0]["review"] == sid and rows[0]["note"] == "Keep exploring."
    assert rows[1] == {
        "date": rows[1]["date"],
        "by": "agent",
        "rule": "auto_delegated",
        "review": sid,
        "decision": "Keep exploring",
        "reason": WHY,
    }
    assert store.answers(sid)[-1]["surface"] == "agent"
    assert [i["short_id"] for i in store.recent_settled()] == [sid]
    assert not validate(load(env["root"]), check_sources=False).errors.get("in-demo-c01")
    # The same gate with the same basis does not come back at the next sync.
    review_collect.sync(store, [env["project"]], gates=env["gates"])
    assert store.get_by_key(KEY) is None


def test_agent_answers_never_teach_rules(env):
    store = env["store"]
    for n in range(3):
        item, _ = store.upsert_item(f"idea:demo:i{n}", section="gated", source="idea", title=f"Idea {n}", basis={"n": n})
        store.add_version(
            item["short_id"],
            question="Park?",
            recommend="A",
            context="",
            options=[
                {"id": "A", "label": "Park", "action": "idea_status", "args": {"status": "parked"}},
                {"id": "B", "label": "Later", "action": "defer", "args": {}},
            ],
        )
        store.answer(item["short_id"], "A", surface="agent")
    assert review_learn.mine(store) == []


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"do": "apply", "option": "B"}, "cannot be undone"),
        ({"do": "apply", "option": "Z"}, "no option Z"),
        ({"do": "apply", "option": "A", "why": "short"}, "why must say"),
        ({"do": "watch", "option": "A"}, "do must be one of"),
    ],
)
def test_apply_refuses_outside_the_delegation_and_writes_nothing(env, entry, message):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    before = (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes()
    with pytest.raises(review_auto.AutoRefused, match=message):
        review_auto.handle(store, {"short_id": sid, "why": WHY, "rule": ONE_OFF, **entry})
    assert store.get(sid)["status"] == "auto" and store.answers(sid) == []
    assert (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes() == before


def test_unmarked_items_and_outdated_options_are_refused(env):
    store = env["store"]
    sid = _cycle_options(store)
    with pytest.raises(review_auto.AutoRefused, match="only items the user marked auto"):
        review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF})
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    store.refresh_basis(sid, {"changed": True})
    with pytest.raises(review_auto.AutoRefused, match="no options for its current source"):
        review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF})
    # New options can be written while it stays auto, then applied.
    entry = {
        "question": "Again?",
        "summary": SUMMARY,
        "why": "w",
        "recommend": "A",
        "options": [
            {"id": "A", "label": "Keep exploring", "action": "record_decision", "args": {"decision": "Keep exploring"}},
            {"id": "B", "label": "Later", "action": "defer", "args": {}},
        ],
    }
    others = {i["short_id"]: {"watch": "agent_owned: fixture owner handles the rest"} for i in store.needs_options()}
    watched = review_write.apply(store, {"items": {**others, sid: {"watch": "agent_owned: nobody really"}}})["errors"]
    assert watched == [f"{sid}: the user marked it auto; apply, resolve, or return it (auto step), do not watch it"]
    assert review_write.apply(store, {"items": {**others, sid: entry}})["errors"] == []
    assert store.get(sid)["status"] == "auto"
    # The fake basis differs from the live cycle file: the executor's recheck refuses, and the item stays with the
    # agents (not the user) with no file write.
    before = (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes()
    assert (
        review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF})["state"]
        == "stale"
    )
    assert store.get(sid)["status"] == "auto"
    assert (env["root"] / "in-demo" / "cycles" / "in-demo-c01.md").read_bytes() == before


def test_return_gives_it_back_with_fresh_options_and_resolve_settles(env):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    done = review_auto.handle(store, {"short_id": sid, "do": "return", "why": "The note asks for a stop; the plan says go."})
    assert done["state"] == "open" and store.get(sid)["status"] == "open"
    assert sid in {i["short_id"] for i in store.needs_options()}, "he gets fresh options that see the reason"
    gate = store.get_by_key(f"kanban:default:{env['gate']}:gate")["short_id"]
    review_exec.Executor(store).mark_auto(gate, surface="tab")
    done = review_auto.handle(
        store, {"short_id": gate, "do": "resolve", "why": "The matrix was approved in a card comment.", "rule": ONE_OFF}
    )
    assert done["state"] == "settled" and store.get(gate)["closed_note"].startswith("auto_delegated: resolved")


def test_overrule_suspends_auto_handling_until_restored(env):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF})
    projects = load_settings()[0].projects
    done = review_learn.overrule(store, projects, sid, "I wanted to stop this cycle.")
    assert done["rule"] == "auto_delegated" and store.rule_states()["auto_delegated"]["state"] == "suspended"
    gate = store.get_by_key(f"kanban:default:{env['gate']}:gate")["short_id"]
    assert review_exec.Executor(store).mark_auto(gate, surface="tab")["state"] == "refused"
    restore = review_exec.Executor(store).submit(done["follow_up"], "B", surface="tab")
    assert restore["state"] == "applied" and store.rule_states()["auto_delegated"]["state"] == "active"
    assert review_exec.Executor(store).mark_auto(gate, surface="tab")["state"] == "auto"


def test_user_can_still_answer_an_auto_item(env):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, surface="tab")
    result = review_exec.Executor(store).submit(sid, "A", note="Mine.", surface="tab")
    assert result["state"] == "applied"
    assert _fm(env)["decisions"][-1]["by"] == "user"
    with pytest.raises(review_auto.AutoRefused):
        review_auto.handle(store, {"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF})


def test_cli_tick_lists_auto_items_and_auto_applies_them(env, tmp_path):
    store = env["store"]
    sid = _cycle_options(store)
    review_exec.Executor(store).mark_auto(sid, "Keep exploring.", surface="tab")
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
    out = json.loads(tick.stdout)
    assert [i["short_id"] for i in out["auto"]] == [sid] and sid not in [i["short_id"] for i in out["items"]]
    payload = {"auto": [{"short_id": sid, "do": "apply", "option": "A", "why": WHY, "rule": ONE_OFF}]}
    done = run("auto", "--file", "-", stdin=json.dumps(payload))
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["results"][0]["state"] == "settled"
    refused = run("auto", "--file", "-", stdin=json.dumps(payload))
    assert refused.returncode == 2 and "only items the user marked auto" in refused.stdout
