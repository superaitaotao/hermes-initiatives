"""Inline buttons for the Telegram review push: tap an option instead of typing ``D3 A``.

A pushed decision carries one button per option plus ``Defer`` and ``Agents handle it``. A tap is applied
through the same :class:`~hermes_initiatives_core.review_exec.Executor` a typed reply uses, with
``surface="telegram"``, so the answer row, its effects, and first-answer-wins are identical either way.

Two transports keep a push from being lost:

* buttons ride a direct Bot API ``sendMessage`` (``hermes send`` carries no keyboard);
* any failure there falls back to the plain-text ``hermes send``, so a missing or foreign bot token never
  loses a decision.

Flow::

    daily_review notify -> send(target, text, keyboard) -> Bot API -> message_id
    tap -> telegram adapter -> CallbackQueryHandler("^rv:") -> Executor.run_commands -> answer + edited message

The tap handler is registered by the plugin's ``register()`` through ``ctx.register_platform_handler``,
scoped to the ``rv:`` prefix so core button flows keep working. It arrives in the gateway process that polls
the review bot; the push is sent by the cron process that runs ``daily_review.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from typing import Any

import httpx

from .review_exec import Command, Executor, format_results, hermes_bin
from .reviews import ReviewStore
from .settings import load_settings

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and the Bot API puts the token in the URL path. Keep it out of any log.
logging.getLogger("httpx").setLevel(max(logging.getLogger("httpx").level, logging.WARNING))
_TOKEN_IN_TEXT = re.compile(r"(?<!\d)(bot)?\d{6,}:[-A-Za-z0-9_]{20,}")


def _redact(text: str, token: str) -> str:
    """``text`` with the bot token (and anything token-shaped) masked: transport errors echo the request URL."""
    if token:
        text = text.replace(token, "<token>")
    return _TOKEN_IN_TEXT.sub("<token>", text)

CALLBACK_PREFIX = "rv:"
PING_ID = "_PING"
DEFER = "~defer"
AUTO = "~auto"
CONFIRM = "~confirm"
DEFER_KIND = "state_change"  # the button means "defer until it changes", like a typed `defer D3 until change`
OPTION_CHARS = 34  # Telegram shows a long button label truncated; keep the option id and the start of the label
BOT_API = "https://api.telegram.org/bot{token}/{method}"
SEND_TIMEOUT_S = 20.0
_CALLBACK = re.compile(
    rf"^{re.escape(CALLBACK_PREFIX)}(D\d+|{PING_ID}):([A-D]|{re.escape(DEFER)}|{re.escape(AUTO)}|{re.escape(CONFIRM)}|ping)$"
)


# --- callback data -----------------------------------------------------------------------------------------


def callback_data(short_id: str, action: str) -> str:
    """``rv:<short id>:<option|~defer|~auto|~confirm>`` — Telegram caps callback_data at 64 bytes."""
    return f"{CALLBACK_PREFIX}{str(short_id).upper()}:{action}"


def parse_callback(data: str) -> tuple[str, str] | None:
    """``(short_id, action)`` for one of our buttons, or None for anything else (a core button, junk)."""
    match = _CALLBACK.fullmatch(str(data or "").strip())
    if match is None:
        return None
    return match.group(1).upper(), match.group(2)


def command_for(short_id: str, action: str) -> Command:
    """The typed reply a tap stands for: ``D3 A``, ``confirm D3``, ``defer D3 until change``, ``auto D3``."""
    if action == CONFIRM:
        return Command("confirm", short_id)
    if action == DEFER:
        return Command("defer", short_id, defer_kind=DEFER_KIND)
    if action == AUTO:
        return Command("auto", short_id)
    return Command("answer", short_id, option=action.upper())


# --- configuration ----------------------------------------------------------------------------------------


def review_target() -> tuple[str, str, str] | None:
    """``(chat_id, thread_id, target)`` of the review channel, or None when it is unset or not Telegram."""
    config, _ = load_settings()
    target = (config.review_target or "").strip()
    parts = target.split(":")
    if len(parts) != 3 or parts[0] != "telegram":
        return None
    return parts[1], parts[2], target


def _hermes_secret(name: str) -> str:
    """A credential through Hermes's documented routes (catalog rule 11: never parse ``.env`` files ourselves).

    In the gateway the token sits in the profile's secret scope (``get_secret``). A script process (the review
    cron's ``daily_review.py notify``) runs with bot tokens stripped from its environment, so it asks Hermes for
    the running profile's env (``hermes_cli.config.load_env``), the same source ``hermes send`` reads.
    """
    try:
        from agent.secret_scope import get_secret

        value = (get_secret(name) or "").strip()
        if value:
            return value
        from hermes_cli.config import load_env

        return str(load_env().get(name) or "").strip()
    except Exception:  # noqa: BLE001 - an unscoped read or an old Hermes means "no token": plain-text fallback
        logger.warning("initiatives review: Hermes could not resolve %s; pushes go out as plain text", name)
        return ""


def bot_token() -> str:
    """The review bot's token, resolved by Hermes for the running profile. Empty: plain ``hermes send`` only."""
    return _hermes_secret("TELEGRAM_BOT_TOKEN")


def _bot(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One Bot API call. Returns ``{ok: ...}``; transport failures come back as ``{ok: false, error: ...}``."""
    try:
        response = httpx.post(BOT_API.format(token=token, method=method), json=payload, timeout=SEND_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": _redact(f"{type(exc).__name__}: {exc}", token)}
    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "error": f"HTTP {response.status_code}: not JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": f"HTTP {response.status_code}: unexpected body"}
    if not body.get("ok"):
        reason = _redact(str(body.get("description") or "no description"), token)
        return {"ok": False, "error": f"HTTP {response.status_code}: {reason}"}
    return body


# --- sending ----------------------------------------------------------------------------------------------


def plain_send(target: str, message: str) -> tuple[str | None, str | None]:
    """Deliver through ``hermes send`` (the transport the review used before buttons). ``(error, None)``."""
    proc = subprocess.run(  # noqa: S603 - fixed binary, argument list
        [hermes_bin(), "send", "--to", target, "--json"], input=message, capture_output=True, text=True, timeout=120, check=False
    )
    return _plain_error(proc), None


def send(target: str, message: str, keyboard: dict | None = None) -> tuple[str | None, str | None]:
    """``(error, message_id)`` for one push: buttons when possible, plain text otherwise.

    A Telegram target with a readable bot token and a keyboard goes out with inline buttons. Everything else
    (no keyboard, no token, any Bot API refusal) is delivered by ``hermes send`` as before, so the caller only
    sees an error when both routes failed.
    """
    parts = str(target or "").split(":")
    token = bot_token()
    if keyboard and len(parts) == 3 and parts[0] == "telegram" and token:
        payload = {"chat_id": parts[1], "message_thread_id": parts[2], "text": message, "reply_markup": keyboard}
        body = _bot(token, "sendMessage", payload)
        if body.get("ok"):
            return None, str((body.get("result") or {}).get("message_id") or "") or None
        logger.warning("initiatives review: buttoned push failed (%s); retrying as plain text", body.get("error"))
    return plain_send(target, message)


def _plain_error(proc: subprocess.CompletedProcess) -> str | None:
    """Why ``hermes send`` did not deliver, or None. Exit 0 is not enough: a cron session that auto-delivers to
    the same target gets ``{"success": true, "skipped": true}`` and nothing is sent."""
    if proc.returncode != 0:
        return proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
    try:
        result = json.loads(proc.stdout) if proc.stdout else None
    except json.JSONDecodeError:
        return None
    if isinstance(result, dict) and (result.get("skipped") or result.get("success") is False):
        return str(result.get("reason") or result.get("error") or "skipped")
    return None


def edit_message(message_id: str | int, text: str, keyboard: dict | None = None, token: str | None = None) -> str | None:
    """Rewrite a pushed decision in place (the outcome, and a cleared or replaced keyboard). Error text or None.

    ``token`` is the bot that delivered the pushed message. Pass the tap's own bot there: a gateway process can
    hold a different bot's token in its environment, and the rewrite must go out as the bot that sent the push.
    """
    target = review_target()
    use_token = (token or "").strip() or bot_token()
    if target is None or not use_token:
        return "no Telegram review target or bot token"
    payload: dict[str, Any] = {"chat_id": target[0], "message_id": message_id, "text": text[:4096]}
    payload["reply_markup"] = keyboard if keyboard is not None else {"inline_keyboard": []}
    body = _bot(use_token, "editMessageText", payload)
    if body.get("ok"):
        return None
    return str(body.get("error"))


# --- the tap ----------------------------------------------------------------------------------------------


def _thread_ids(query: Any) -> tuple[Any, Any]:
    """Both thread fields a Telegram message can carry: forum topics use one, DM topics the other."""
    message = getattr(query, "message", None)
    return getattr(message, "message_thread_id", None), getattr(message, "direct_messages_topic_id", None)


def _allowed(query: Any) -> bool:
    """Only the review chat and topic, and only an approver (``Settings.answer_approvers``), may answer.

    Button taps reach this handler straight from the Telegram client, before the gateway's sender check, so the
    tapping user is checked here: in a group topic every member can see the buttons.
    """
    target = review_target()
    message = getattr(query, "message", None)
    if target is None or message is None:
        return False
    if str(getattr(message, "chat_id", "")) != target[0]:
        logger.warning(
            "initiatives review: refused a tap from chat %s; the review chat is %s", getattr(message, "chat_id", None), target[0]
        )
        return False
    thread = next((t for t in _thread_ids(query) if t is not None), None)
    # A group target must match its topic exactly: a tap with no topic came from the group's general chat (or a
    # client that omits it), not from the review topic. A private chat has one conversation, so no topic is fine.
    private = target[0].isdigit()
    if (thread is None and not private) or (thread is not None and str(thread) != target[1]):
        logger.warning("initiatives review: refused a tap in thread %s; the review thread is %s", thread, target[1])
        return False
    user = str(getattr(getattr(query, "from_user", None), "id", "") or "").strip()
    approvers = load_settings()[0].answer_approvers()
    if not user or approvers is None or user not in approvers:
        logger.warning("initiatives review: refused a tap by user %s; not a configured review approver", user or "unknown")
        return False
    return True


def _apply(short_id: str, action: str, message_id: str | None) -> list[dict[str, str]]:
    """Run the tap's command through the shared executor. Never raises: a failure is a result line."""
    try:
        executor = Executor(ReviewStore())
        return executor.run_commands([command_for(short_id, action)], surface="telegram", telegram_msg_id=message_id)
    except Exception as exc:  # noqa: BLE001 - reported to the user in the edited message, as the text hook does
        logger.exception("initiatives review: applying a button answer failed")
        return [{"short_id": short_id, "state": "failed", "message": f"could not apply: {exc}"}]


def _appended(original: str, outcome: str) -> str:
    """The pushed text with the outcome below it: the question and options stay readable after the tap."""
    return f"{original.rstrip()}\n\n{outcome}"[:4096]


def _confirm_keyboard(short_id: str) -> dict:
    return {"inline_keyboard": [[{"text": "Confirm — cannot be undone", "callback_data": callback_data(short_id, CONFIRM)}]]}


def _edit(message_id: Any, text: str, keyboard: dict | None = None, token: str | None = None) -> None:
    """Rewrite the message, and log a refusal: a silent failure here looks like the tap never arrived."""
    try:
        target_id = int(message_id)
    except (TypeError, ValueError):
        logger.warning("initiatives review: the tap carried no usable message id (%r); nothing to rewrite", message_id)
        return
    error = edit_message(target_id, text, keyboard, token)
    if error:
        logger.warning("initiatives review: could not rewrite message %s after a tap (%s)", target_id, error)
    else:
        logger.warning("initiatives review: rewrote message %s after a tap as bot %s", target_id, _fingerprint(token))


async def _toast(query: Any, text: str) -> None:
    """Acknowledge the tap. A failure here must never stop the answer from being applied."""
    try:
        await query.answer(text=text)
    except Exception:  # noqa: BLE001 - the toast is cosmetic; the answer and the rewrite still matter
        logger.exception("initiatives review: could not acknowledge a tap")


def _callback_token(context: Any, query: Any) -> str:
    """The token of the bot that received the tap: the push and its rewrite must use the same bot.

    A gateway process can carry another bot's token in its environment or another profile's ``.env``, so the
    callback's own bot wins. Reading it from the update also survives a token rotation in the settings files.
    """
    bot = getattr(context, "bot", None)
    if bot is None:
        getter = getattr(query, "get_bot", None)
        bot = getter() if callable(getter) else None
    token = (getattr(bot, "token", "") or "").strip()
    if token:
        return token
    logger.warning("initiatives review: the tap carried no bot; falling back to the configured token")
    return bot_token()


def _fingerprint(token: str | None = None) -> str:
    """A short hash of a token, never the token, so a log can say which bot a call used."""
    value = (token or "").strip() or bot_token()
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "none"


async def on_callback(update: Any, context: Any = None) -> None:
    """A button tap: apply it, then rewrite the pushed message with the outcome."""
    query = getattr(update, "callback_query", None)
    data = getattr(query, "data", None) if query is not None else None
    parsed = parse_callback(data or "")
    if query is None or parsed is None:
        return
    try:
        await _handle_tap(query, data or "", parsed, _callback_token(context, query))
    except Exception:  # noqa: BLE001 - PTB would swallow this; the user must see why a tap did nothing
        logger.exception("initiatives review: handling tap %s failed", data)


async def _handle_tap(query: Any, data: str, parsed: tuple[str, str], token: str) -> None:
    if not _allowed(query):
        await _toast(query, "This is not the review chat.")
        return
    message = getattr(query, "message", None)
    message_id = getattr(message, "message_id", None)
    original = getattr(message, "text", None) or ""
    short_id, action = parsed
    target = review_target()
    logger.warning(
        "initiatives review: tap %s on message %r (text %d chars); target %s, bot %s",
        data,
        message_id,
        len(original),
        target[2] if target else None,
        _fingerprint(token),
    )
    if short_id == PING_ID:
        await _toast(query, "Buttons work.")
        done = "✅ Buttons work — a tap applies an answer like a typed reply."
        await asyncio.to_thread(_edit, message_id, _appended(original, done), None, token)
        logger.warning("initiatives review: tap %s finished", data)
        return
    await _toast(query, f"{short_id} {action} — applying")
    results = await asyncio.to_thread(_apply, short_id, action, str(message_id) if message_id else None)
    outcome = format_results(results)
    keyboard = _confirm_keyboard(short_id) if any(r["state"] == "awaiting_confirm" for r in results) else None
    await asyncio.to_thread(_edit, message_id, _appended(original, outcome), keyboard, token)
    logger.warning("initiatives review: tap %s finished: %s", data, outcome.replace("\n", " | "))


def telegram_factory(native: Any, adapter: Any = None) -> None:  # noqa: ARG001 - factory contract takes both
    """``ctx.register_platform_handler`` factory: add the scoped ``rv:`` callback handler to the PTB app."""
    if native is None or not hasattr(native, "add_handler"):
        logger.warning("initiatives review: no Telegram native client; answer buttons stay inert on this adapter")
        return
    try:
        from telegram.ext import CallbackQueryHandler
    except ImportError:  # pragma: no cover - PTB is present wherever a Telegram adapter is
        logger.warning("initiatives review: python-telegram-bot is unavailable; buttons stay inert")
        return
    native.add_handler(CallbackQueryHandler(on_callback, pattern=rf"^{re.escape(CALLBACK_PREFIX)}"))
    # WARNING, not INFO: the gateway logs at WARNING, so this is the only visible proof that taps reach us.
    logger.warning("initiatives review: wired the %s answer-button handler onto this Telegram adapter", CALLBACK_PREFIX)
