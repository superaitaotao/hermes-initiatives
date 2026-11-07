"""Telegram answer buttons: the keyboard a push carries, and what one tap does.

These tests guard the two silent failures: a button whose callback no longer names a real option (the tap
would record nothing), and a buttoned push that fails without falling back to plain text (the decision would
never reach the user). A tap runs the same executor a typed reply runs, so the recorded surface and message
id are asserted too.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"))

from hermes_initiatives_core import review_buttons, review_digest, review_exec, reviews  # noqa: E402
from hermes_initiatives_core.settings import settings_path  # noqa: E402

daily_review = pytest.importorskip("daily_review")

CHAT = "123456789"
TARGET = f"telegram:{CHAT}:4242"
CALLBACK_BOT_TOKEN = "111:the-bot-that-got-the-tap"
D14 = dt.datetime(2026, 10, 17, 14, 0, tzinfo=dt.UTC)  # Inside the configured UTC push window
OPTS = [
    {
        "id": "A",
        "label": "Archive the gate card and keep the 12 reds",
        "action": "kanban_archive",
        "args": {"board": "b", "card": "t_1"},
    },
    {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
]


@pytest.fixture
def store(isolated_hermes) -> reviews.ReviewStore:
    """One decision in the plugin data dir the tap handler itself opens, with a review target configured."""
    settings_path().write_text(json.dumps({"projects": [], "review_target": TARGET, "timezone": "UTC"}), encoding="utf-8")
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("kanban:b:t_1:gate", section="gated", source="kanban", title="gate title", basis={"e": 1})
    store.add_version(item["short_id"], question="Archive the gate?", options=OPTS, recommend="A", context="why")
    return store


class FakeQuery:
    """The parts of a PTB CallbackQuery the handler touches: its data, its message, and its toast."""

    def __init__(
        self, data: str, *, chat_id: str = CHAT, message_id: int = 77, text: str = "Decision D1 — gate", user: str = CHAT
    ) -> None:
        self.data = data
        self.answers: list[str] = []
        self.from_user = SimpleNamespace(id=int(user)) if user else None
        self.message = SimpleNamespace(chat_id=chat_id, message_id=message_id, text=text, message_thread_id=4242)

    async def answer(self, text: str | None = None, **_: object) -> None:
        self.answers.append(text or "")


def tap(query: FakeQuery, *, bot_token: str | None = CALLBACK_BOT_TOKEN) -> None:
    """Run one tap as PTB does: the update, plus a context whose bot is the one that got the callback."""
    bot = SimpleNamespace(token=bot_token) if bot_token else None
    asyncio.run(review_buttons.on_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)))


def test_push_keyboard_carries_the_option_ids_a_typed_reply_accepts(store):
    item = store.get(store.queue(now=D14)[0]["short_id"])
    keyboard = review_digest.push_keyboard(item)

    assert [row[0]["callback_data"] for row in keyboard["inline_keyboard"][:2]] == [
        review_buttons.callback_data(item["short_id"], "A"),
        review_buttons.callback_data(item["short_id"], "B"),
    ]
    assert keyboard["inline_keyboard"][0][0]["text"].startswith("A · ") and "★" in keyboard["inline_keyboard"][0][0]["text"]
    assert "⚠" in keyboard["inline_keyboard"][0][0]["text"], "an irreversible option must show its warning"
    assert [b["callback_data"] for b in keyboard["inline_keyboard"][2]] == [
        review_buttons.callback_data(item["short_id"], "~defer"),
        review_buttons.callback_data(item["short_id"], "~auto"),
    ]
    for row in keyboard["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64, "Telegram caps callback_data at 64 bytes"
            assert review_buttons.parse_callback(button["callback_data"]) is not None


def test_typed_or_foreign_button_data_is_not_ours():
    for data in ("cl:3:0", "ea:once:1", "rv:D1:", "rv:X:A", "rv:D1:E", "D3 A", ""):
        assert review_buttons.parse_callback(data) is None


def test_a_defer_or_auto_tap_maps_to_the_typed_command():
    assert review_buttons.command_for("D3", "B") == review_exec.Command("answer", "D3", option="B")
    assert review_buttons.command_for("D3", review_buttons.CONFIRM) == review_exec.Command("confirm", "D3")
    assert review_buttons.command_for("D3", review_buttons.AUTO) == review_exec.Command("auto", "D3")
    deferred = review_buttons.command_for("D3", review_buttons.DEFER)
    assert (deferred.kind, deferred.defer_kind) == ("defer", "state_change")


def test_a_tap_records_the_answer_and_asks_for_the_confirm_step(store, monkeypatch):
    edits: list[tuple] = []
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: edits.append(args))
    short_id = store.queue(now=D14)[0]["short_id"]

    tap(FakeQuery(review_buttons.callback_data(short_id, "A")))

    item = store.get(short_id)
    assert item["status"] == "awaiting_confirm"
    answer = store.answers(short_id)[0]
    assert (answer["option"], answer["surface"], answer["telegram_msg_id"]) == ("A", "telegram", "77")
    assert edits[0][0] == 77 and "cannot be undone" in edits[0][1]
    assert edits[0][2]["inline_keyboard"][0][0]["callback_data"] == review_buttons.callback_data(short_id, review_buttons.CONFIRM)


def test_a_tap_from_another_chat_is_refused(store, monkeypatch):
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: pytest.fail("a refused tap must not edit"))
    short_id = store.queue(now=D14)[0]["short_id"]
    query = FakeQuery(review_buttons.callback_data(short_id, "A"), chat_id="999")

    tap(query)

    assert store.get(short_id)["status"] == "open" and store.answers(short_id) == []
    assert query.answers == ["This is not the review chat."]


def test_a_tap_in_another_thread_of_the_review_chat_is_refused(store, monkeypatch):
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: pytest.fail("a refused tap must not edit"))
    short_id = store.queue(now=D14)[0]["short_id"]
    query = FakeQuery(review_buttons.callback_data(short_id, "A"))
    query.message.message_thread_id = 999

    tap(query)

    assert store.answers(short_id) == [] and query.answers == ["This is not the review chat."]


def test_a_tap_carrying_no_thread_field_is_accepted_in_a_private_chat(store, monkeypatch):
    """A DM may report only `direct_messages_topic_id`, or neither: a private chat has one conversation."""
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: None)
    short_id = store.queue(now=D14)[0]["short_id"]
    query = FakeQuery(review_buttons.callback_data(short_id, "A"))
    query.message.message_thread_id = None

    tap(query)

    assert store.answers(short_id)[0]["option"] == "A"


def test_the_self_test_button_answers_without_recording_anything(store, monkeypatch):
    edits: list[tuple] = []
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: edits.append(args))
    short_id = store.queue(now=D14)[0]["short_id"]
    query = FakeQuery(review_buttons.callback_data(review_buttons.PING_ID, "ping"))

    tap(query)

    assert query.answers == ["Buttons work."] and store.answers(short_id) == []
    assert "Buttons work" in edits[0][1] and edits[0][2] is None, "the ping clears the keyboard"


def test_the_rewrite_uses_the_bot_that_received_the_tap(store, monkeypatch):
    """The gateway process holds the root bot's token; the rewrite must still go out as the callback's own bot."""
    monkeypatch.setattr(review_buttons, "bot_token", lambda: "999:some-other-bot")
    edits: list[tuple] = []
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: edits.append(args))
    short_id = store.queue(now=D14)[0]["short_id"]

    tap(FakeQuery(review_buttons.callback_data(short_id, "A")))

    assert edits[0][3] == CALLBACK_BOT_TOKEN
    assert store.answers(short_id)[0]["option"] == "A"


def test_a_tap_carrying_no_bot_falls_back_to_the_configured_token(store, monkeypatch):
    monkeypatch.setattr(review_buttons, "bot_token", lambda: "999:some-other-bot")
    edits: list[tuple] = []
    monkeypatch.setattr(review_buttons, "edit_message", lambda *args, **_: edits.append(args))
    short_id = store.queue(now=D14)[0]["short_id"]

    tap(FakeQuery(review_buttons.callback_data(short_id, "A")), bot_token=None)

    assert edits[0][3] == "999:some-other-bot"


def test_a_buttoned_push_that_cannot_go_out_falls_back_to_plain_text(monkeypatch):
    monkeypatch.setattr(review_buttons, "bot_token", lambda: "token")
    monkeypatch.setattr(review_buttons, "_bot", lambda *a, **k: {"ok": False, "error": "message thread not found"})
    plain: list[str] = []
    monkeypatch.setattr(review_buttons, "plain_send", lambda target, message: (plain.append(message) or None, None))

    assert review_buttons.send(TARGET, "body", {"inline_keyboard": []}) == (None, None)
    assert plain == ["body"]


def test_a_buttoned_push_keeps_its_message_id_and_skips_the_fallback(monkeypatch):
    monkeypatch.setattr(review_buttons, "bot_token", lambda: "token")
    monkeypatch.setattr(review_buttons, "_bot", lambda *a, **k: {"ok": True, "result": {"message_id": 77}})
    monkeypatch.setattr(review_buttons, "plain_send", lambda *a, **k: pytest.fail("no fallback expected"))

    assert review_buttons.send(TARGET, "body", {"inline_keyboard": []}) == (None, "77")


def test_the_factory_registers_one_rv_scoped_callback_handler():
    """An unscoped handler would swallow Hermes' own button flows (approvals, the model picker)."""
    from telegram.ext import CallbackQueryHandler

    added = []
    review_buttons.telegram_factory(SimpleNamespace(add_handler=added.append))

    assert len(added) == 1 and isinstance(added[0], CallbackQueryHandler)
    assert added[0].pattern.pattern == "^rv:"
    assert review_buttons.telegram_factory(None) is None, "an adapter without a native client must not raise"


def test_notify_pushes_a_decision_with_its_buttons(store):
    short_id = store.queue(now=D14)[0]["short_id"]
    sent: list[dict] = []

    def send_with_buttons(target: str, message: str, keyboard: dict) -> tuple[str | None, str | None]:
        sent.append({"target": target, "message": message, "keyboard": keyboard})
        return None, "77"

    pushed = daily_review.notify(
        store,
        TARGET,
        lambda *a: pytest.fail("the plain sender must not be used"),
        now=D14,
        titles={},
        send_with_buttons=send_with_buttons,
    )

    assert pushed == [short_id]
    keyboard = sent[0]["keyboard"]
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == review_buttons.callback_data(short_id, "A")
    assert "Decision" in sent[0]["message"]


def test_a_tap_by_another_member_of_the_review_chat_is_refused(store, monkeypatch):
    """Taps arrive before the gateway's sender check: in a group topic every member sees the buttons."""
    monkeypatch.setattr(review_buttons, "edit_message", lambda *a, **k: None)
    short_id = store.queue(now=D14)[0]["short_id"]
    query = FakeQuery(review_buttons.callback_data(short_id, "A"), user="555")
    tap(query)
    assert store.answers(short_id) == [] and query.answers == ["This is not the review chat."]


def test_a_group_target_needs_listed_approvers(store, monkeypatch):
    monkeypatch.setattr(review_buttons, "edit_message", lambda *a, **k: None)
    group = "-1001234567890"
    short_id = store.queue(now=D14)[0]["short_id"]
    for approvers, allowed in (([], False), (["42"], True)):
        settings_path().write_text(json.dumps({"projects": [], "review_target": f"telegram:{group}:4242",
                                               "review_approvers": approvers, "timezone": "UTC"}), encoding="utf-8")
        query = FakeQuery(review_buttons.callback_data(short_id, "B"), chat_id=group, user="42")
        tap(query)
        assert bool(store.answers(short_id)) is allowed, approvers


def test_a_group_tap_without_a_topic_is_refused(store, monkeypatch):
    """In a group, a tap with no topic came from the general chat (or hid it): the review topic must match exactly."""
    monkeypatch.setattr(review_buttons, "edit_message", lambda *a, **k: pytest.fail("a refused tap must not edit"))
    group = "-1001234567890"
    settings_path().write_text(json.dumps({"projects": [], "review_target": f"telegram:{group}:4242",
                                           "review_approvers": ["42"], "timezone": "UTC"}), encoding="utf-8")
    short_id = store.queue(now=D14)[0]["short_id"]
    query = FakeQuery(review_buttons.callback_data(short_id, "B"), chat_id=group, user="42")
    query.message.message_thread_id = None

    tap(query)

    assert store.answers(short_id) == [] and query.answers == ["This is not the review chat."]


def test_bot_api_failures_never_carry_the_token(monkeypatch, caplog):
    token = "123456789:" + "AA" + "EXAMPLEexampleEXAMPLEexample_token"  # split so the public-tree token scan stays strict

    def boom(url, **_):
        raise review_buttons.httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(review_buttons.httpx, "post", boom)
    caplog.set_level("DEBUG")
    error = review_buttons._bot(token, "sendMessage", {})["error"]
    assert token not in error and "AAEXAMPLE" not in error and "<token>" in error

    class Refusal:
        status_code = 401

        @staticmethod
        def json():
            return {"ok": False, "description": f"Unauthorized bot{token}"}

    monkeypatch.setattr(review_buttons.httpx, "post", lambda url, **_: Refusal())
    assert token not in review_buttons._bot(token, "sendMessage", {})["error"]
    assert logging_level("httpx") >= 30, "httpx logs request URLs (with the token) at INFO"
    assert token not in caplog.text


def logging_level(name: str) -> int:
    import logging

    return logging.getLogger(name).getEffectiveLevel()


def test_the_bot_token_comes_from_hermes_not_from_env_files(monkeypatch, isolated_hermes):
    (isolated_hermes / ".env").write_text("TELEGRAM_BOT_TOKEN=from-the-profile-env\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    import inspect

    source = inspect.getsource(review_buttons)
    assert "read_text" not in source and ".env\"" not in source, "the plugin must not parse .env files itself"
    assert review_buttons.bot_token() == "from-the-profile-env"
