"""Status summaries: facts hash, tick input, all-or-nothing write, stale flag, and the read-only API route."""

from __future__ import annotations

import datetime as dt
import importlib.util

import pytest
from conftest import ROOT, cycle_md, write
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_initiatives_core import reviews, summaries
from hermes_initiatives_core.settings import ProjectSettings


@pytest.fixture
def project(root):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", title="First change"))
    return ProjectSettings(id="demo", label="Demo", root=root)


@pytest.fixture
def stores(tmp_path):
    return summaries.SummaryStore(tmp_path / "summaries.db"), reviews.ReviewStore(tmp_path / "reviews.db")


def _write_one(project, stores, text="Demo is exploring its first change."):
    store, rev = stores
    entry = summaries.tick_input(project, store, rev)["due"][0]
    return summaries.write_summaries(
        project, store, rev, {"summaries": {"in-demo": {"text": text, "evidence_hash": entry["evidence_hash"]}}}
    )


def test_first_tick_lists_the_initiative_with_facts_and_no_previous(project, stores):
    store, rev = stores
    out = summaries.tick_input(project, store, rev)
    assert [d["initiative"] for d in out["due"]] == ["in-demo"]
    due = out["due"][0]
    assert due["facts"]["open_cycles"][0]["id"] == "in-demo-c01" and "previous" not in due


def test_unchanged_facts_keep_the_text_and_only_confirm_it(project, stores):
    store, rev = stores
    _write_one(project, stores)
    with store._connect() as conn:
        conn.execute("UPDATE summaries SET checked_at = '2026-01-01T00:00:00+00:00'")
    again = summaries.tick_input(project, store, rev)
    assert again["due"] == [] and again["unchanged"] == ["in-demo"]
    row = store.rows("demo")["in-demo"]
    assert row["checked_at"] > "2026-01-02"
    assert row["text"] == "Demo is exploring its first change."


def test_changed_facts_come_back_with_the_previous_text(project, stores):
    store, rev = stores
    _write_one(project, stores)
    item, _ = rev.upsert_item("k", section="gated", source="kanban", title="Gate", basis={}, initiative="in-demo")
    again = summaries.tick_input(project, store, rev)
    assert again["due"][0]["previous"]["text"].startswith("Demo is exploring")
    assert again["due"][0]["facts"]["review_items"] == {"open": 1}
    assert item["short_id"]


def test_write_is_all_or_nothing_and_checks_the_limit_and_the_target(project, stores):
    store, rev = stores
    digest = summaries.tick_input(project, store, rev)["due"][0]["evidence_hash"]
    bad = {"summaries": {"in-demo": {"text": "ok", "evidence_hash": digest}, "in-ghost": {"text": "x", "evidence_hash": "h"}}}
    with pytest.raises(summaries.SummaryError, match="in-ghost"):
        summaries.write_summaries(project, store, rev, bad)
    assert store.rows("demo") == {}
    long = {"summaries": {"in-demo": {"text": "x" * (summaries.MAX_TEXT + 1), "evidence_hash": digest}}}
    with pytest.raises(summaries.SummaryError, match="limit"):
        summaries.write_summaries(project, store, rev, long)
    with pytest.raises(summaries.SummaryError):
        summaries.write_summaries(project, store, rev, {"summaries": {"in-demo": {"text": "", "evidence_hash": digest}}})


def test_hash_read_before_facts_moved_is_not_recorded_as_current(project, stores):
    store, rev = stores
    entry = summaries.tick_input(project, store, rev)["due"][0]
    rev.upsert_item("k", section="gated", source="kanban", title="Gate", basis={}, initiative="in-demo")
    summaries.write_summaries(
        project, store, rev, {"summaries": {"in-demo": {"text": "Old read.", "evidence_hash": entry["evidence_hash"]}}}
    )
    assert summaries.tick_input(project, store, rev)["due"][0]["previous"]["text"] == "Old read."


def test_view_marks_a_summary_stale_after_the_window(project, stores):
    store, _ = stores
    _write_one(project, stores)
    now = dt.datetime.now(dt.UTC)
    fresh = summaries.public_view("demo", store, now)["in-demo"]
    assert fresh["stale"] is False and fresh["age_minutes"] == 0
    later = summaries.public_view("demo", store, now + dt.timedelta(minutes=summaries.STALE_AFTER_MIN + 5))["in-demo"]
    assert later["stale"] is True


def test_inactive_initiatives_are_pruned(project, stores):
    store, rev = stores
    _write_one(project, stores)
    store.put("demo", "in-gone", "old", "h", {})
    summaries.tick_input(project, store, rev)
    assert set(store.rows("demo")) == {"in-demo"}


def test_api_route_is_read_only_and_404s_unknown_project(project, tmp_path, monkeypatch):
    import json

    (tmp_path / "plugin-data").mkdir(exist_ok=True)
    (tmp_path / "plugin-data" / "settings.json").write_text(
        json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(project.root)}]}), encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("initiatives_plugin_api_summaries", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    client = TestClient(app)
    store = summaries.SummaryStore()
    store.put("demo", "in-demo", "Fine.", "h", {})
    data = client.get("/api/projects/demo/summaries").json()
    assert data["summaries"]["in-demo"]["text"] == "Fine." and data["summaries"]["in-demo"]["stale"] is False
    assert client.get("/api/projects/nope/summaries").status_code == 404
    assert client.post("/api/projects/demo/summaries", json={}).status_code == 405
