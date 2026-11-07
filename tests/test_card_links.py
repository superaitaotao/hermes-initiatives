"""Cycle card links: agents append through ``link_cards``; the page derives cards from a body marker line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import DECIDED, ROOT, cycle_md, write

from hermes_initiatives_core import kanban, store, views, writers
from hermes_initiatives_core.settings import ProjectSettings
from hermes_initiatives_core.validate import validate

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

SCRIPT = ROOT / "skills" / "initiatives" / "scripts" / "link_cards.py"


@pytest.fixture
def board(isolated_hermes):
    kbc.init_db(board="default")
    conn = kbc.connect(board="default")
    ids = {
        "listed": kb.create_task(conn, title="Listed", body="- Initiative cycle: in-demo-c01\n", assignee="coder"),
        "marked": kb.create_task(conn, title="Marked", body="Work.\n- Initiative cycle: `in-demo-c01` (record x)\n"),
        "prose": kb.create_task(conn, title="Prose", body="Plan review for initiative cycle `in-demo-c01`.\n"),
        "other": kb.create_task(conn, title="Other", body="- Initiative cycle: in-demo-c010\n"),
        "archived": kb.create_task(conn, title="Old", body="- Initiative cycle: in-demo-c01\n"),
        "plain": kb.create_task(conn, title="Plain", body="No marker.\n"),
    }
    kb.archive_task(conn, ids["archived"])
    conn.close()
    return {**ids, "path": Path(kb.kanban_db_path("default"))}


@pytest.fixture
def cycle(root, board):
    return write(
        root / "in-demo" / "cycles" / "in-demo-c01.md",
        cycle_md("in-demo-c01", stage="shipping", decisions=DECIDED, kanban_cards=[board["listed"]]),
    )


def _cards(root):
    return store.load(root).cycles["in-demo-c01"].frontmatter.get("kanban_cards")


def _notes(root):
    log = root / "in-demo" / "observations" / "in-demo-c01.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --- W1: the writer -----------------------------------------------------------------------------------------


def test_link_appends_in_order_and_is_idempotent(root, board, cycle, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "orchestrator")
    linked = writers.link_cards(root, "default", "in-demo-c01", [board["marked"], board["plain"]])
    assert linked == [board["marked"], board["plain"]]
    assert _cards(root) == [board["listed"], board["marked"], board["plain"]]
    assert writers.link_cards(root, "default", "in-demo-c01", [board["plain"], board["listed"]]) == []
    assert _cards(root) == [board["listed"], board["marked"], board["plain"]]
    notes = _notes(root)
    assert len(notes) == 1 and notes[0]["kind"] == "note" and notes[0]["by"] == "orchestrator"
    assert board["marked"] in notes[0]["detail"]
    assert validate(store.load(root), check_sources=False).ok


def test_link_never_removes_an_existing_entry(root, board):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", kanban_cards=["t_00000000"]))
    writers.link_cards(root, "default", "in-demo-c01", [board["plain"]])
    assert _cards(root) == ["t_00000000", board["plain"]]


@pytest.mark.parametrize(
    ("cycle_id", "cards", "message"),
    [
        ("in-demo-c01", ["T_123"], "card id"),
        ("in-demo-c09", ["t_00000001"], "cycle file not found"),
        ("not-a-cycle", ["t_00000001"], "not a cycle id"),
        ("in-demo-c01", ["t_ffffffff"], "not on board"),
        ("in-demo-c01", [], "no card"),
    ],
)
def test_link_refuses_bad_input_and_writes_nothing(root, board, cycle, cycle_id, cards, message):
    before = cycle.read_text()
    with pytest.raises(writers.WriteRefused, match=message):
        writers.link_cards(root, "default", cycle_id, cards)
    assert cycle.read_text() == before and _notes(root) == []


def test_link_refuses_an_excluded_card(root, board):
    write(
        root / "in-demo" / "cycles" / "in-demo-c01.md",
        cycle_md("in-demo-c01", kanban_cards=[], excluded_cards=[board["marked"]]),
    )
    with pytest.raises(writers.WriteRefused, match="excluded"):
        writers.link_cards(root, "default", "in-demo-c01", [board["marked"]])


def test_link_fails_closed_without_a_board(root, cycle):
    with pytest.raises(writers.WriteRefused, match="board"):
        writers.link_cards(root, "no-such-board", "in-demo-c01", ["t_00000001"])


def test_excluded_cards_must_be_card_ids(root):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", excluded_cards=["nope"]))
    assert not validate(store.load(root), check_sources=False).ok


# --- W2: derived cards on the page --------------------------------------------------------------------------


def test_markers_need_the_exact_body_line(board):
    result = kanban.cycle_markers("default")
    assert result["state"] == "fresh"
    assert result["markers"]["in-demo-c01"] == [board["listed"], board["marked"]]
    assert result["markers"]["in-demo-c010"] == [board["other"]]


def test_missing_board_gives_no_markers(isolated_hermes):
    result = kanban.cycle_markers("no-such-board")
    assert result["state"] == "unavailable" and result["markers"] == {}


def _project(root):
    return ProjectSettings(id="demo", label="Demo", root=root, kanban_board="default")


def test_detail_shows_derived_cards_and_flags_them(root, board, cycle):
    detail = views.build_detail(_project(root), "in-demo")
    view = next(c for c in detail["cycles"] if c["id"] == "in-demo-c01")
    assert [(c["id"], c["link"]) for c in view["cards"]] == [(board["listed"], "listed"), (board["marked"], "derived")]
    assert "cards_unlinked" in view["flags"]
    assert view["next_action"] == "Finish delivery: 2 of 2 cards open"
    summary = views.build_list(_project(root))["initiatives"][0]
    assert "cards_unlinked" in summary["flags"]


def test_linked_or_excluded_cards_raise_no_flag(root, board):
    write(
        root / "in-demo" / "cycles" / "in-demo-c01.md",
        cycle_md(
            "in-demo-c01",
            stage="shipping",
            decisions=DECIDED,
            kanban_cards=[board["listed"]],
            excluded_cards=[board["marked"]],
        ),
    )
    view = views.build_detail(_project(root), "in-demo")["cycles"][0]
    assert [c["id"] for c in view["cards"]] == [board["listed"]]
    assert "cards_unlinked" not in view["flags"]


# --- W3: sweep and script -----------------------------------------------------------------------------------


def test_sweep_links_every_derived_card_once(root, board, cycle):
    assert writers.sweep_links(root, "default") == {"in-demo-c01": [board["marked"]]}
    assert _cards(root) == [board["listed"], board["marked"]]
    assert writers.sweep_links(root, "default") == {}


def _run(*args):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_PYTHON"] = sys.executable
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


def test_script_links_cards_and_sweeps(root, board, cycle):
    base = ["--root", str(root), "--board", "default"]
    proc = _run(*base, "--cycle", "in-demo-c01", "--card", board["plain"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True, "cycle": "in-demo-c01", "linked": [board["plain"]]}
    proc = _run(*base, "--sweep")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ok": True, "linked": {"in-demo-c01": [board["marked"]]}}
    proc = _run(*base, "--cycle", "in-demo-c01", "--card", "t_ffffffff")
    assert proc.returncode == 2 and "not on board" in proc.stderr


def test_daily_collect_sweeps_links_first(root, board, cycle):
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    project = {"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default"}
    (data / "settings.json").write_text(json.dumps({"projects": [project]}))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    script = ROOT / "skills" / "initiatives" / "scripts" / "daily_review.py"
    proc = subprocess.run(
        [sys.executable, str(script), "collect", "--trigger", "manual"], env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["facts"]["linked_cards"] == {"demo": {"in-demo-c01": [board["marked"]]}}
    assert _cards(root) == [board["listed"], board["marked"]]
