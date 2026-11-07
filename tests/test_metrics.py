"""Kanban health metrics: time buckets, failure causes, review rounds, token join, hourly snapshots, and the API route."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sqlite3

import pytest
from conftest import ROOT
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_initiatives_core import metrics

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

PR = "https://github.com/acme/app/pull/7"
HOUR = 3600
DAY = 86400


def _now() -> float:
    """Local noon yesterday-ish, fixed per run so buckets are deterministic in any timezone."""
    tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime(2026, 10, 20, 12, 0, tzinfo=tz).timestamp()


@pytest.fixture
def board(isolated_hermes):
    """A board where each card's timestamps are rewritten to known instants inside the 7-day window."""
    now = _now()
    kbc.init_db(board="default")
    conn = kbc.connect(board="default")
    shipped = kb.create_task(conn, title="Shipped with PR", assignee="coder")
    kb.complete_task(conn, shipped, result="ok", metadata={"published_pr": PR})
    local = kb.create_task(conn, title="Local only", assignee="planner")
    kb.complete_task(conn, local, result="ok")
    old = kb.create_task(conn, title="Done last week", assignee="coder")
    kb.complete_task(conn, old, result="ok")
    stuck = kb.create_task(conn, title="Needs a decision", assignee="coder")
    conn.close()
    path = kb.kanban_db_path("default")
    raw = sqlite3.connect(path)
    with raw:
        raw.execute(
            "UPDATE tasks SET created_at = ?, completed_at = ? WHERE id = ?", (now - 2 * DAY - 10 * HOUR, now - 2 * DAY, shipped)
        )
        raw.execute(
            "UPDATE tasks SET created_at = ?, completed_at = ? WHERE id = ?", (now - 1 * HOUR - 4 * HOUR, now - 1 * HOUR, local)
        )
        raw.execute("UPDATE tasks SET created_at = ?, completed_at = ? WHERE id = ?", (now - 9 * DAY, now - 8 * DAY, old))
        raw.execute(
            "UPDATE tasks SET created_at = ?, status = 'blocked', block_kind = 'needs_input' WHERE id = ?", (now - 3 * DAY, stuck)
        )
        # Replace the runs the helpers made with a known set, all started in the current window except one.
        raw.execute("DELETE FROM task_runs")
        raw.execute("DELETE FROM task_events WHERE kind IN ('review_requested', 'changes_requested', 'blocked')")
        runs = [
            (shipped, "coder", "rate_limited", now - 3 * DAY, now - 3 * DAY + 10, {}),
            (shipped, "coder", "crashed", now - 3 * DAY + HOUR, now - 3 * DAY + 2 * HOUR, {"protocol_violation": True}),
            (shipped, "coder", "review_requested", now - 3 * DAY + 3 * HOUR, now - 3 * DAY + 5 * HOUR, {"published_pr": PR}),
            (shipped, "reviewer", "changes_requested", now - 2 * DAY - 6 * HOUR, now - 2 * DAY - 5 * HOUR, {}),
            (shipped, "reviewer", "completed", now - 2 * DAY - HOUR, now - 2 * DAY, {}),
            (local, "planner", "completed", now - 2 * HOUR, now - HOUR, {}),
            (stuck, "coder", "blocked", now - 3 * DAY, now - 3 * DAY + HOUR, {}),
            (stuck, "coder", "crashed", now - 2 * HOUR, now - HOUR, {}),
            (old, "coder", "timed_out", now - 9 * DAY, now - 9 * DAY + HOUR, {}),
        ]
        for task, profile, outcome, start, end, meta in runs:
            raw.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) "
                "VALUES (?,?,?,?,?,?,?)",
                (task, profile, outcome, outcome, start, end, json.dumps(meta)),
            )
        for task, kind, at in [
            (shipped, "review_requested", now - 3 * DAY + 5 * HOUR),
            (shipped, "changes_requested", now - 2 * DAY - 5 * HOUR),
            (shipped, "review_requested", now - 2 * DAY - 2 * HOUR),
            (stuck, "blocked", now - 3 * DAY + HOUR),
            (stuck, "respawn_guarded", now - 2 * DAY),
        ]:
            raw.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, '{}', ?)", (task, kind, at))
    raw.close()
    return {"now": now, "shipped": shipped, "local": local, "old": old, "stuck": stuck, "path": path}


def _lane_sessions(home, profile, rows):
    db = home / "profiles" / profile / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL, input_tokens INT, output_tokens INT, "
            "cache_read_tokens INT, cache_write_tokens INT)"
        )
        conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", rows)
    conn.close()


def test_totals_and_buckets_over_a_seven_day_window(board, isolated_hermes):
    now = board["now"]
    _lane_sessions(
        isolated_hermes,
        "coder",
        [
            ("s1", "kanban", now - 3 * DAY + 3 * HOUR, 1000, 200, 50_000, 0),
            ("s2", "cli", now - 3 * DAY, 9_999_999, 0, 0, 0),  # not a Kanban session
            ("s3", "kanban", now - 20 * DAY, 777, 0, 0, 0),  # outside both windows
        ],
    )
    result = metrics.board_metrics("default", 7, now=now)
    assert result["window"]["bucket"] == "day" and len(result["series"]) == 7
    # Every bucket is labelled with its own local start and end; the last one ends at generation time.
    assert result["series"][-1]["end"] == result["generated_at"] == result["window"]["end"]
    assert all(b["end"] == n["start"] for b, n in zip(result["series"], result["series"][1:], strict=False))
    tot = result["totals"]
    assert tot["delivered"] == 2 and tot["delivered_pr"] == 1
    assert tot["lead_p50_h"] == 7.0  # median of 10 h and 4 h
    assert tot["runs"] == 8 and tot["runs_failed"] == 3
    assert tot["failed_by_cause"]["rate_limited"] == 1
    assert tot["failed_by_cause"]["protocol_violation"] == 1
    assert tot["failed_by_cause"]["crashed"] == 1
    assert tot["reviewed_deliveries"] == 1 and tot["first_pass_rate"] == 0.0
    assert tot["changes_requested"] == 1 and tot["respawn_guarded"] == 1
    assert tot["runs_per_delivery_p50"] == 3.0  # 5 runs and 1 run
    assert tot["tokens_fresh"] == 1200 and tot["tokens_cache"] == 50_000 and tot["sessions"] == 1
    assert tot["tokens_fresh_per_delivery"] == 600
    # The delivered card lands in the bucket of its completion time.
    day_of = [i for i, b in enumerate(result["series"]) if b["delivered_pr"]]
    assert len(day_of) == 1
    lo = dt.datetime.fromisoformat(result["series"][day_of[0]]["start"]).timestamp()
    hi = dt.datetime.fromisoformat(result["series"][day_of[0]]["end"]).timestamp()
    assert lo <= now - 2 * DAY < hi
    # The previous equal window is separate: last week's timed-out run and delivery are counted there only.
    prev = result["previous"]["totals"]
    assert prev["delivered"] == 1 and prev["runs"] == 1 and prev["failed_by_cause"]["timed_out"] == 1
    lanes = {lane["profile"]: lane for lane in result["lanes"]}
    assert lanes["coder"]["runs"] == 5 and lanes["coder"]["runs_failed"] == 3 and lanes["coder"]["tokens_fresh"] == 1200
    assert set(result["token_missing"]) == {"planner", "reviewer"}


def test_now_lists_blocked_cards_with_age_and_who_they_wait_on(board):
    result = metrics.board_metrics("default", 7, now=board["now"])
    state = result["now"]
    assert state["needs_you"] == 1 and state["blocked_by_kind"] == {"needs_input": 1}
    oldest = state["oldest_blocked"][0]
    assert oldest["id"] == board["stuck"] and oldest["age_h"] == 71.0


def test_weekly_buckets_start_on_monday(board):
    result = metrics.board_metrics("default", 90, now=board["now"])
    assert result["window"]["bucket"] == "week"
    assert all(dt.datetime.fromisoformat(b["start"]).weekday() == 0 for b in result["series"])
    assert result["totals"]["delivered"] == 3


def test_invalid_window_is_refused(board):
    with pytest.raises(ValueError):
        metrics.board_metrics("default", 5, now=board["now"])
    with pytest.raises(ValueError):
        metrics.board_metrics("default", 7, "month", now=board["now"])


def test_board_database_is_opened_read_only(board, monkeypatch):
    seen = []
    real = metrics._connect

    def spy(path):
        conn = real(path)
        seen.append(conn)
        return conn

    monkeypatch.setattr(metrics, "_connect", spy)
    metrics.board_metrics("default", 7, now=board["now"])
    with pytest.raises(sqlite3.ProgrammingError):
        seen[0].execute("SELECT 1")  # closed after use
    conn = real(board["path"])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE tasks SET title = 'x'")
    conn.close()


def test_snapshots_are_hourly_and_form_a_history(board, tmp_path):
    store = metrics.SnapshotStore(tmp_path / "metrics.db")
    now = board["now"]
    first = metrics.board_metrics("default", 7, now=now, snapshots=store)
    assert first["snapshot_recorded"] is True and len(first["history"]) == 1
    again = metrics.board_metrics("default", 7, now=now + 600, snapshots=store)
    assert again["snapshot_recorded"] is False and len(again["history"]) == 1
    later = metrics.board_metrics("default", 7, now=now + HOUR, snapshots=store)
    assert later["snapshot_recorded"] is True
    assert [h["blocked"] for h in later["history"]] == [1, 1]
    assert later["history"][0]["at"] == first["generated_at"]


def _client():
    spec = importlib.util.spec_from_file_location("initiatives_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/initiatives")
    return TestClient(app)


def test_metrics_route(board, root, tmp_path):
    settings = {"projects": [{"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default"}]}
    (tmp_path / "plugin-data").mkdir(exist_ok=True)
    (tmp_path / "plugin-data" / "settings.json").write_text(json.dumps(settings))
    client = _client()
    ok = client.get("/api/plugins/initiatives/projects/demo/metrics?days=14")
    assert ok.status_code == 200
    body = ok.json()
    assert body["window"]["days"] == 14 and len(body["series"]) == 14 and body["history"]
    assert (tmp_path / "plugin-data" / "metrics.db").is_file()
    assert client.get("/api/plugins/initiatives/projects/demo/metrics?days=3").status_code == 400
    settings["projects"][0]["kanban_board"] = "missing-board"
    (tmp_path / "plugin-data" / "settings.json").write_text(json.dumps(settings))
    assert client.get("/api/plugins/initiatives/projects/demo/metrics").status_code == 503
