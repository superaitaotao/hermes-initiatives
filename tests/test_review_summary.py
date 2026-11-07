"""Blocker summaries: every decision opens with what is blocked and why, in plain words.

The tick must write one with every set of options; items written before summaries existed get one through
``daily_review.py summarize`` without a new version (so nothing is pushed again).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SUMMARY

from hermes_initiatives_core import review_digest, review_write, reviews

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"
OPTS = [
    {"id": "A", "label": "Unblock", "action": "kanban_unblock", "args": {"board": "b", "card": "t_00000001"}},
    {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
]


@pytest.fixture
def env(isolated_hermes, root):
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(root)}]}))
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def _daily(env, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "daily_review.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _gates(tmp_path):
    """An empty, current gate list: the tick reads no Kanban board and keeps the stored item."""
    path = tmp_path / "gates.json"
    path.write_text(json.dumps({"board": "b", "cards": [], "generated_at": dt.datetime.now(dt.UTC).isoformat()}))
    return str(path)


def _gate(store, key="kanban:b:t_00000001:gate"):
    item, _ = store.upsert_item(key, section="gated", source="kanban", title="t_00000001 Smoke script", basis={"e": 1})
    return item["short_id"]


def _entry(**extra):
    return {"question": "Unblock the smoke script?", "options": OPTS, "recommend": "A", "why": "ctx", **extra}


def test_write_refuses_options_without_a_plain_summary(isolated_hermes):
    store = reviews.ReviewStore()
    sid = _gate(store)
    errors = review_write.apply(store, {"items": {sid: _entry()}})["errors"]
    assert errors == [f"{sid}: summary must be an object with what and why"]
    short = review_write.apply(store, {"items": {sid: _entry(summary={"what": "Card t_1", "why": SUMMARY["why"]})}})["errors"]
    assert short == [f"{sid}: summary.what is required: one or two plain sentences"]
    long = review_write.apply(store, {"items": {sid: _entry(summary={"what": SUMMARY["what"], "why": "x" * 701})}})["errors"]
    assert long == [f"{sid}: summary.why is 701 characters; keep it under 700"]
    assert store.get(sid)["version"] is None, "nothing is written when a check fails"


def test_summary_is_stored_and_leads_the_push_and_the_digest(isolated_hermes):
    store = reviews.ReviewStore()
    sid = _gate(store)
    messy = {"what": "  The smoke script card\n is blocked. ", "why": SUMMARY["why"]}
    assert review_write.apply(store, {"items": {sid: _entry(summary=messy)}})["errors"] == []
    version = store.get(sid)["version"]
    assert version["summary"] == {"what": "The smoke script card is blocked.", "why": SUMMARY["why"]}

    push = review_digest.push_message(store.get(sid), {}).splitlines()
    assert push[3:5] == ["What: The smoke script card is blocked.", f"Why: {SUMMARY['why']}"]
    block = review_digest._item_block(store.get(sid), dt.datetime.now(dt.UTC))
    assert block.index("What: The smoke script card") < block.index("Why: ") < block.index("ctx") < block.index(": Unblock")


def test_summarize_backfills_old_options_without_a_new_version(env, tmp_path):
    store = reviews.ReviewStore()
    sid = _gate(store)
    store.add_version(sid, question="Old?", options=OPTS, recommend="A", context="")  # written before summaries
    assert store.get(sid)["version"]["summary"] is None

    tick = _daily(env, "tick", "--gates", _gates(tmp_path))
    listed = json.loads(tick.stdout)["unsummarised"]
    assert [i["short_id"] for i in listed] == [sid]

    path = tmp_path / "summaries.json"
    path.write_text(json.dumps({"summaries": {sid.lower(): SUMMARY, "D999": SUMMARY}}))
    run = _daily(env, "summarize", "--file", str(path))
    out = json.loads(run.stdout)
    assert run.returncode == 1 and out["summarised"] == [sid] and out["errors"] == ["D999: D999 has no options to summarise"]
    version = store.get(sid)["version"]
    assert version["n"] == 1 and version["summary"] == SUMMARY, "same version: the decision is not pushed again"

    tick = _daily(env, "tick", "--gates", _gates(tmp_path))
    assert json.loads(tick.stdout)["unsummarised"] == []
