"""Regression: a worker's `OWNER DECISION:` comment must wake the recovery cron.

Workers are fenced from reassigning other cards, so the planner records the owner as a comment and the
recovery cron applies it. The gate ignored every worker comment, so that hand-off waited for an unrelated
event or the 3 h backstop.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "autonomy/kanban_recovery_wake_gate.py"


def _load_gate(db: Path):
    spec = importlib.util.spec_from_file_location("wake_gate_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.BOARD_DB = db
    mod.WORKERS = {"planner", "coder"}
    return mod


def _board(tmp_path: Path, comments: list[tuple[str, str]]) -> Path:
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE tasks (id TEXT, status TEXT, last_heartbeat_at REAL);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at REAL);"
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at REAL);"
    )
    c.execute("INSERT INTO tasks VALUES ('t_b', 'blocked', NULL)")
    for author, body in comments:
        c.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t_b', ?, ?, 0)", (author, body))
    c.commit()
    c.close()
    return db


def test_worker_owner_decision_comment_wakes(tmp_path):
    gate = _load_gate(_board(tmp_path, [("planner", "OWNER DECISION: coder — repo change")]))
    _, reasons = gate.board_state({"operator"}, 0, 0)
    assert any("owner decision" in r and "t_b" in r for r in reasons), reasons


def test_ordinary_worker_comment_stays_quiet(tmp_path):
    gate = _load_gate(_board(tmp_path, [("coder", "progress: tests green at abc12345")]))
    _, reasons = gate.board_state({"operator"}, 0, 0)
    assert reasons == []
