"""Coverage gaps: asks that alert jobs delivered in the window but that have no review item."""

from __future__ import annotations

import datetime as dt
import json

from hermes_initiatives_core import review_gaps, reviews
from hermes_initiatives_core.settings import load_settings, settings_path


def _output(home, profile, job, stamp, body):
    base = home / ("profiles/" + profile if profile else "") / "cron" / "output" / job
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{stamp}.md").write_text(f"# Cron Job: {job}\n\n## Prompt\n\nignored NEEDS YOU t_prompt\n\n## Response\n\n{body}\n")


def test_only_configured_alert_jobs_are_scanned(tmp_path):
    home = tmp_path / ".hermes"
    _output(home, "worker", "review-watch", "2026-10-12_12-00-00", "t_bbbbbbb2 NEEDS YOU pick A or B")
    _output(home, "worker", "other-job", "2026-10-12_12-00-00", "t_ccccccc3 NEEDS YOU approve")
    store = reviews.ReviewStore(tmp_path / "r.db")
    since = dt.datetime(2026, 10, 11, tzinfo=dt.UTC)
    assert review_gaps.scan(store, home, since=since, alert_jobs=load_settings()[0].alert_jobs) == []
    settings_path().write_text(json.dumps({"alert_jobs": [
        {"profile": "worker", "job": "review-watch", "label": "Board watch"}
    ]}))
    gaps = review_gaps.scan(store, home, since=since, alert_jobs=load_settings()[0].alert_jobs)
    assert [(g["source"], g["ref"], g["match"]) for g in gaps] == [("Board watch", "t_bbbbbbb2", "exact")]


def test_finds_ask_lines_with_card_ids_and_matches_exactly(tmp_path):
    home = tmp_path / ".hermes"
    _output(
        home,
        "worker",
        "review-watch",
        "2026-10-12_01-00-00",
        "- t_aaaaaaa1: **BLOCKED — NEEDS YOU** — approve the matrix\n- t_bbbbbbb2: NEEDS YOU pick A or B\n- done things",
    )
    _output(home, "worker", "review-watch", "2026-10-12_02-00-00", "[SILENT]")
    _output(home, None, "calendar-watch", "2026-10-12_01-05-00", "**New item for review**\n- Example charge $28.09")
    _output(home, "worker", "review-watch", "2026-10-10_01-00-00", "t_ccccccc3 NEEDS YOU old")
    store = reviews.ReviewStore(tmp_path / "r.db")
    store.upsert_item("kanban:my-board:t_aaaaaaa1:gate", section="gated", source="kanban", title="x", basis={})
    since = dt.datetime(2026, 10, 11, 1, 0, tzinfo=dt.UTC)
    config = {"alert_jobs": [{"profile": "worker", "job": "review-watch", "label": "Board watch"},
                             {"job": "calendar-watch", "label": "Calendar watch"}]}
    settings_path().write_text(json.dumps(config))
    gaps = review_gaps.scan(store, home, since=since, alert_jobs=load_settings()[0].alert_jobs)
    refs = [(g["ref"], g["match"]) for g in gaps]
    assert ("t_bbbbbbb2", "exact") in refs
    assert all(r != "t_aaaaaaa1" for r, _ in refs), "covered by an item"
    assert all(r != "t_ccccccc3" for r, _ in refs), "outside the window"
    assert all(r != "t_prompt" for r, _ in refs), "prompt text is not delivered output"
    assert any(g["match"] == "heuristic" and "New item for review" in g["line"] for g in gaps)
