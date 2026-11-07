"""Waiting on you: a step only the user can do never hides behind a parked Review item.

Regression for D75: the tick wired every option, including the approval, to ``defer``. The user's answer parked the
item, posted nothing to the card, and the card left his Review tab while it still waited on him.
Kanban runs on the real board (conftest pins HERMES_HOME and HERMES_KANBAN_HOME under tmp_path).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest
from conftest import SUMMARY, write

from hermes_initiatives_core import (
    kanban,
    review_attention,
    review_collect,
    review_digest,
    review_exec,
    review_write,
    reviews,
)
from hermes_initiatives_core.settings import load_settings

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

HANDOFF_STEP = (
    "**FYI** — Your recorded answer did not reach this card.\n\n"
    "**You:** set up the TEST smoke login in an attended session.\n**Next:** the coder resumes after that.\n"
)
HANDOFF_NOTHING = "**BLOCKED — MACHINE** — waiting on CI.\n\n**You:** nothing needed now.\n**Next:** recovery retries.\n"


@pytest.fixture
def env(isolated_hermes, root, tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "hermes"
    write(cli, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m hermes_cli.main "$@"\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    kbc.init_db(board="default")
    conn = kbc.connect(board="default")
    step = kb.create_task(conn, title="Release smoke script", assignee="coder")
    kb.block_task(conn, step, reason="needs a TEST login", kind="needs_input")
    kb.add_comment(conn, step, "operator", HANDOFF_STEP)
    machine = kb.create_task(conn, title="Waiting on CI", assignee="coder")
    kb.block_task(conn, machine, reason="CI", kind="needs_input")
    kb.add_comment(conn, machine, "orchestrator", HANDOFF_NOTHING)
    conn.close()
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(
        json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default"}]})
    )
    store = reviews.ReviewStore()
    return {"store": store, "step": step, "machine": machine, "project": load_settings()[0].projects[0]}


def _gate(store, card):
    """Collect the card as a real gate, so the executor's source recheck matches its basis."""
    project = load_settings()[0].projects[0]
    review_collect.sync(store, [project], gates={"default": {card: {"id": card}}})
    return store.get_by_key(f"kanban:default:{card}:gate")["short_id"]


ALL_DEFER = [
    {"id": "A", "label": "Approve the amendment; you set up a TEST login", "action": "defer", "args": {}},
    {"id": "B", "label": "Plan a backend change instead", "action": "defer", "args": {}},
    {"id": "C", "label": "Keep it parked", "action": "defer", "args": {}},
]


def test_parse_handoff_reads_header_and_step():
    assert kanban.parse_handoff(HANDOFF_STEP) == {
        "header": "FYI",
        "you": "set up the TEST smoke login in an attended session.",
    }
    assert kanban.parse_handoff("plain text") == {"header": None, "you": None}
    assert not review_attention.needs_user("nothing needed now. Review the receipt later.")
    assert not review_attention.needs_user("`nothing` on this card")
    assert review_attention.needs_user("answer Review item D85.")


def test_card_parked_by_its_answer_is_waiting_on_you(env):
    store = env["store"]
    sid = _gate(store, env["step"])
    store.add_version(sid, question="Approve?", options=ALL_DEFER, recommend="A", context="")
    result = review_exec.Executor(store).submit(sid, "A", surface="tab")
    assert result["state"] == "deferred" and store.get(sid)["status"] == "deferred"

    waiting = review_attention.collect(store, [env["project"]])
    assert waiting["errors"] == []
    entry = next(i for i in waiting["items"] if i["card"] == env["step"])
    assert entry["you"].startswith("set up the TEST smoke login")
    assert entry["item"]["short_id"] == sid and entry["item"]["status"] == "deferred"
    assert entry["item"]["answer"]["option"] == "A" and entry["item"]["answer"]["action"] == "defer"
    # A machine block whose handoff says "nothing needed" is not the user's work.
    assert env["machine"] not in {i["card"] for i in waiting["items"]}


def test_card_with_no_review_item_still_shows(env):
    waiting = review_attention.collect(env["store"], [env["project"]])
    assert [i["card"] for i in waiting["items"]] == [env["step"]]
    assert waiting["items"][0]["item"] is None


def test_open_decision_is_not_listed_twice(env):
    store = env["store"]
    sid = _gate(store, env["step"])
    store.add_version(
        sid,
        question="Approve?",
        options=[
            {"id": "A", "label": "Unblock", "action": "kanban_unblock", "args": {}},
            {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="",
    )
    assert env["step"] not in {i["card"] for i in review_attention.collect(store, [env["project"]])["items"]}
    # A session can still be started about it.
    assert review_attention.find(store, env["project"], env["step"])["item"]["short_id"] == sid


def test_write_refuses_options_that_only_park_a_gate(env):
    store = env["store"]
    sid = _gate(store, env["step"])
    payload = {"items": {sid: {"question": "Approve?", "options": ALL_DEFER, "recommend": "A", "why": "x", "summary": SUMMARY}}}
    errors = review_write.apply(store, payload)["errors"]
    assert any("every option only parks" in e for e in errors)
    assert any("option A approves but its action is defer" in e for e in errors)
    assert store.get(sid)["version"] is None, "nothing is written when a check fails"


def test_write_refuses_attend_without_the_step(env):
    store = env["store"]
    sid = _gate(store, env["step"])
    options = [
        {"id": "A", "label": "You set up the login", "action": "attend", "args": {}},
        {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
    ]
    errors = review_write.apply(
        store, {"items": {sid: {"question": "Q", "options": options, "recommend": "A", "summary": SUMMARY}}}
    )["errors"]
    assert any("attend needs args.you" in e for e in errors)


def test_attend_posts_the_answer_and_keeps_the_card_on_the_waiting_list(env):
    store = env["store"]
    sid = _gate(store, env["step"])
    you = "Set up the TEST smoke login in an attended session."
    options = [
        {
            "id": "A",
            "label": "Approve the amendment; you set up the TEST login",
            "action": "attend",
            "args": {"board": "default", "card": env["step"], "you": you, "next": "the planner amends the plan"},
        },
        {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
    ]
    assert (
        review_write.apply(store, {"items": {sid: {"question": "Q", "options": options, "recommend": "A", "summary": SUMMARY}}})[
            "errors"
        ]
        == []
    )

    result = review_exec.Executor(store).submit(sid, "A", note="Use the staging project.", surface="tab")
    assert result["state"] == "deferred", result
    conn = kbc.connect(board="default")
    try:
        bodies = [c.body for c in kb.list_comments(conn, env["step"])]
        assert kb.get_task(conn, env["step"]).status == "blocked", "attend never unblocks the card"
    finally:
        conn.close()
    posted = next(b for b in bodies if "ATTEND — NEEDS YOU" in b)
    assert f"**You:** {you}" in posted and "Use the staging project." in posted and "the planner amends" in posted

    entry = review_attention.find(store, env["project"], env["step"])
    assert entry is not None and entry["you"] == you and entry["item"]["status"] == "deferred"


def test_digest_lists_waiting_cards(env):
    store = env["store"]
    review = store.create_review(trigger="cron", model=None, window_start=None)
    titles = [title for title, _ in review_digest.sections(store, review["id"])]
    assert any(t.startswith("Waiting on you — 1 card") for t in titles)
    text = "\n".join(review_digest.render(store, review["id"]))
    assert env["step"] in text and "You: set up the TEST smoke login" in text
