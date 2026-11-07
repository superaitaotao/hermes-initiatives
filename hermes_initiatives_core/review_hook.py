"""Gateway hook: answers typed in the daily review topic are applied by the executor, not read by an agent.

Registered on ``post_gateway_admission`` (after auth, under the routed profile). A message is taken only when it is
in ``settings.review_target`` and every line is a review command (``D3 A``, ``D3 B: note``, ``confirm D3``,
``defer D3 until 2026-10-20``, ``auto D3: note``, ``manual D3``). Anything else passes through to the agent unchanged.

The hook has a time limit (``plugins.hook_callback_timeout``, 30 s), and a timed-out hook lets the message through
to the agent. So the hook answers within ``BUDGET_S``: answers are recorded first (durable), and when applying
takes longer the reply says so and the results are sent to the topic when the work ends.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from typing import Any

from .review_exec import Executor, format_results, hermes_bin, parse_message
from .reviews import ReviewStore
from .settings import load_settings

logger = logging.getLogger(__name__)
BUDGET_S = 20.0


def _target() -> tuple[str, str, str] | None:
    config, error = load_settings()
    if error or not config.review_target:
        return None
    parts = config.review_target.split(":")
    if len(parts) != 3 or parts[0] != "telegram":
        return None
    return parts[0], parts[1], parts[2]


def in_review_topic(platform: str, source: dict[str, Any]) -> bool:
    target = _target()
    if target is None or platform != target[0]:
        return False
    if str(source.get("chat_id")) != target[1] or str(source.get("thread_id") or "") != target[2]:
        return False
    # The gateway admitted the sender; a group topic can still hold admitted users who are not the decision owner.
    approvers = load_settings()[0].answer_approvers()
    user = str(source.get("user_id") or "").strip()
    if approvers is None or user not in approvers:
        logger.warning("initiatives review: answer-shaped text from user %s ignored; not a review approver", user or "unknown")
        return False
    return True


def _send_later(text: str) -> None:
    config, _ = load_settings()
    if not config.review_target:
        return
    subprocess.run(  # noqa: S603 - fixed binary, argument list
        [hermes_bin(), "send", "--to", config.review_target], input=text, capture_output=True, text=True, timeout=60, check=False
    )


def handle(platform: str, source: dict[str, Any], message_id: str | None, text: str) -> dict[str, Any] | None:
    """Synchronous core of the hook (tests call this). None = not a review answer."""
    if not in_review_topic(platform, source):
        return None
    commands = parse_message(text)
    if commands is None:
        return None
    executor = Executor(ReviewStore())
    done = threading.Event()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["results"] = executor.run_commands(commands, surface="telegram", telegram_msg_id=message_id)
        except Exception as exc:  # noqa: BLE001 - reported to the user; the hook must not fail open into the agent
            logger.exception("daily review: applying answers failed")
            box["error"] = str(exc)
        finally:
            done.set()
            if box.get("late"):
                _send_later(_reply(box))

    threading.Thread(target=work, name="daily-review-answers", daemon=True).start()
    if not done.wait(BUDGET_S):
        box["late"] = True
        if not done.is_set():
            return {"action": "handled", "reply": "Answers recorded. Still applying them; the results follow here."}
    return {"action": "handled", "reply": _reply(box)}


def _reply(box: dict[str, Any]) -> str:
    if "error" in box:
        return f"❌ Applying the answers stopped with an error: {box['error']}. Check the Review tab."
    return format_results(box.get("results") or [])


async def post_gateway_admission(
    session_key: str = "", platform: str = "", source: dict | None = None, message_id: str | None = None, text: str = "", **_: Any
) -> dict[str, Any] | None:
    return await asyncio.to_thread(handle, platform, source or {}, message_id, text)
