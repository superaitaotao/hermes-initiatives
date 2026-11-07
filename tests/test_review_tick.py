"""The merged 30-minute tick: per-tick options, one push per new decision, and a once-a-day digest.

`daily_review.py tick` syncs every source (Kanban from the recovery tick's gate list) and prints only the items
that need options; it creates no review row. `notify` pushes each new decision once per basis, inside the configured push window.
`daily` creates the review row and digest once per configured local day, at or after the digest time,
and is retried until sent.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from conftest import SUMMARY

from hermes_initiatives_core import review_digest, reviews
from hermes_initiatives_core.settings import load_settings, settings_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"))
daily_review = pytest.importorskip("daily_review")

OPTS = [
    {"id": "A", "label": "Approve and unblock t_1", "action": "kanban_unblock", "args": {"board": "b", "card": "t_1"}},
    {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
]


@pytest.fixture
def store(tmp_path):
    settings_path().write_text(json.dumps({"timezone": "America/New_York", "push_start_hour": 9,
                                           "push_end_hour": 18, "digest_at": "10:15"}))
    return reviews.ReviewStore(tmp_path / "reviews.db")


def _decision(store, key="kanban:b:t_1:gate", basis=None, initiative="in-a"):
    item, _ = store.upsert_item(
        key, section="gated", source="kanban", title=f"{key} title", basis=basis or {"e": 1}, initiative=initiative
    )
    store.add_version(item["short_id"], question="Approve the matrix?", options=OPTS, recommend="A", context="why")
    return item["short_id"]


class Sent:
    """Stands in for `hermes send`: records each message and returns a receipt."""

    def __init__(self, fail_at: int | None = None):
        self.messages: list[str] = []
        self.fail_at = fail_at

    def __call__(self, target: str, message: str) -> str | None:
        if self.fail_at is not None and len(self.messages) == self.fail_at:
            return "chat not found"
        self.messages.append(message)
        return None


def test_push_message_names_the_initiative_question_options_and_reply(store):
    sid = _decision(store)
    text = review_digest.push_message(store.get(sid), {"in-a": "Test suite improvement"})
    assert text.startswith(f"Decision {sid} — Test suite improvement")
    assert "Approve the matrix?" in text and "A (recommended): Approve and unblock t_1" in text
    assert f"{sid} A" in text, "the reply syntax uses this item's id"


def test_notify_pushes_each_decision_once_per_basis(store):
    sid = _decision(store)
    send = Sent()
    at = dt.datetime(2026, 10, 12, 10, 0, tzinfo=load_settings()[0].zone())
    assert daily_review.notify(store, "telegram:1:2", send, now=at, titles={}) == [sid]
    assert daily_review.notify(store, "telegram:1:2", send, now=at, titles={}) == []
    assert len(send.messages) == 1
    # New card state, new options: pushed again.
    store.upsert_item("kanban:b:t_1:gate", section="gated", source="kanban", title="t", basis={"e": 2})
    store.add_version(sid, question="Approve v2?", options=OPTS, recommend="A", context="")
    assert daily_review.notify(store, "telegram:1:2", send, now=at, titles={}) == [sid]


def test_notify_skips_items_without_options_watching_and_deferred(store):
    _decision(store, key="kanban:b:t_2:gate")
    store.watch(_decision(store, key="kanban:b:t_3:gate"), "Machine block")
    store.defer(_decision(store, key="kanban:b:t_4:gate"), kind="state_change", until=None, reason="")
    store.upsert_item("kanban:b:t_5:gate", section="gated", source="kanban", title="no options yet", basis={})
    send = Sent()
    at = dt.datetime(2026, 10, 12, 10, 0, tzinfo=load_settings()[0].zone())
    pushed = daily_review.notify(store, "telegram:1:2", send, now=at, titles={})
    assert len(pushed) == 1 and len(send.messages) == 1


def test_notify_is_quiet_at_night_and_catches_up_in_the_morning(store):
    sid = _decision(store)
    send = Sent()
    assert daily_review.notify(store, "t", send, now=dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.UTC), titles={}) == []
    assert daily_review.notify(store, "t", send, now=dt.datetime(2026, 10, 12, 12, 59, tzinfo=dt.UTC), titles={}) == []
    at = dt.datetime(2026, 10, 12, 13, 0, tzinfo=dt.UTC)  # 09:00 in the configured zone
    assert daily_review.notify(store, "t", send, now=at, titles={}) == [sid]


def test_baseline_marks_current_decisions_pushed_without_sending(store):
    old = _decision(store)
    send = Sent()
    at = dt.datetime(2026, 10, 12, 10, 0, tzinfo=load_settings()[0].zone())
    assert daily_review.notify(store, "t", send, now=at, titles={}, baseline=True) == [old]
    assert send.messages == [], "cutover must not re-send decisions the user already has"
    new = _decision(store, key="kanban:b:t_8:gate")
    assert daily_review.notify(store, "t", send, now=at, titles={}) == [new]


def test_notify_failure_is_not_recorded_so_the_next_tick_retries(store):
    sid = _decision(store)
    at = dt.datetime(2026, 10, 12, 10, 0, tzinfo=load_settings()[0].zone())
    with pytest.raises(daily_review.SendFailed, match="chat not found"):
        daily_review.notify(store, "t", Sent(fail_at=0), now=at, titles={})
    assert daily_review.notify(store, "t", Sent(), now=at, titles={}) == [sid]


def test_daily_is_due_once_per_day_after_configured_time_and_until_it_was_sent(store):
    # create_review stamps the real clock, so the test day must be today in the configured zone.
    today = dt.datetime.now(load_settings()[0].zone()).date()
    before = dt.datetime.combine(today, dt.time(10, 14), tzinfo=load_settings()[0].zone())
    after = dt.datetime.combine(today, dt.time(10, 16), tzinfo=load_settings()[0].zone())
    assert daily_review.daily_due(store, now=before) is False
    assert daily_review.daily_due(store, now=after) is True
    review = store.create_review(trigger="cron", model="m", window_start=None)
    assert daily_review.daily_due(store, now=after) is True, "created but not sent: retry"
    store.update_review(review["id"], telegram_json=json.dumps([{"part": 1}]))
    with store._connect() as conn:
        conn.execute("UPDATE reviews SET generated_at = ? WHERE id = ?", (after.isoformat(), review["id"]))
    assert daily_review.daily_due(store, now=after) is False
    assert daily_review.daily_due(store, now=after + dt.timedelta(days=1)) is True


def test_write_without_a_review_stores_tick_options(store):
    from hermes_initiatives_core import review_write

    item, _ = store.upsert_item("kanban:b:t_9:gate", section="gated", source="kanban", title="t", basis={"e": 9})
    entry = {"question": "Q?", "options": OPTS, "recommend": "A", "why": "w", "summary": SUMMARY}
    result = review_write.apply(store, {"items": {item["short_id"]: entry}})
    assert result["errors"] == []
    assert store.get(item["short_id"])["version"]["review_id"] is None
    assert store.latest_review() is None
    refused = review_write.apply(store, {"items": {}, "summaries": {"in-a": "x"}})
    assert any("summaries" in e for e in refused["errors"]), "summaries belong to a review"


def test_versions_written_between_digests_carry_no_review(store):
    sid = _decision(store)
    assert store.get(sid)["version"]["review_id"] is None
    assert store.latest_review() is None, "a tick creates no review row"
