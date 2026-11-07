"""board_path() must resolve exactly like Hermes's own resolver (no private import at runtime)."""

from __future__ import annotations

import pytest

from hermes_initiatives_core.kanban import board_path

kb = pytest.importorskip("hermes_cli.kanban_db")
dc = pytest.importorskip("agent.delegation_context")

ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_BOARD")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return monkeypatch


@pytest.mark.parametrize("board", ["default", "DEFAULT", " demo ", "Demo", "a-b_c", "x" * 64])
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"HERMES_KANBAN_HOME": "~/boards"},
        {"HERMES_KANBAN_DB": "~/pinned.db"},
        {"HERMES_KANBAN_DB": "/tmp/pinned.db", "HERMES_KANBAN_TASK": "t_1"},
        {"HERMES_KANBAN_DB": "/tmp/pinned.db", "HERMES_DELEGATED_CHILD_CONTEXT": "1"},
        {"HERMES_KANBAN_DB": "/tmp/pinned.db", "HERMES_KANBAN_TASK": "  "},
    ],
)
def test_matches_hermes(clean_env, board, env):
    for key, value in env.items():
        clean_env.setenv(key, value)
    assert board_path(board) == kb.kanban_db_path(board=board)


@pytest.mark.parametrize("board", ["", "  ", "bad slug", "--foo", "_x", "../x", "a/b", "x" * 65, "é"])
def test_rejects_what_hermes_rejects(clean_env, board):
    with pytest.raises(ValueError):
        board_path(board)
    if board.strip():
        with pytest.raises(ValueError):
            kb.kanban_db_path(board=board)


def test_in_process_delegated_child_uses_the_pin(clean_env):
    clean_env.setenv("HERMES_KANBAN_DB", "/tmp/pinned.db")
    with dc.delegated_child_context():
        assert board_path("demo") == kb.kanban_db_path(board="demo") == kb.Path("/tmp/pinned.db")
