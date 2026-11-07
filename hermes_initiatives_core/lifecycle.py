"""Derived lifecycle state: release, displayed stage, evaluation date, next action, attention flags.

Nothing here is stored. The cycle file holds user decisions, the observation log holds agent
observations, and these functions combine them with live Kanban card data at read time.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from .model import OPEN_STAGES, RELEASE_KINDS, STAGE_RANK

DONE_CARD_STATUSES = {"done", "archived"}
RELEASE_GRACE_DAYS = 7
STALE_GRACE_DAYS = 7
DEFAULT_OBSERVE_EVERY_DAYS = 7


def as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def observation_date(row: dict[str, Any]) -> dt.date | None:
    return as_date(row.get("evidence_date")) or as_date(row.get("at"))


def release_state(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The newest release observation decides: verified (with its date) or failed or none."""
    latest = None
    for row in rows:
        if row.get("kind") in RELEASE_KINDS:
            latest = row
    if latest is None:
        return {"state": "none", "date": None, "observation": None}
    verified = latest.get("kind") == "release_verified"
    released = observation_date(latest) if verified else None
    return {
        "state": "verified" if verified else "failed",
        "date": released.isoformat() if released else None,
        "observation": latest,
    }


def effective_stage(frontmatter: dict[str, Any], release: dict[str, Any]) -> str:
    stage = str(frontmatter.get("stage") or "exploring")
    if stage in OPEN_STAGES and release["state"] == "verified" and STAGE_RANK[stage] < STAGE_RANK["measuring"]:
        return "measuring"
    return stage


def evaluation_date(frontmatter: dict[str, Any], release: dict[str, Any]) -> dt.date | None:
    measurement = frontmatter.get("measurement") or {}
    window = measurement.get("observation_window") if isinstance(measurement, dict) else None
    if not isinstance(window, dict):
        return None
    raw_length = window.get("length_days")
    if isinstance(raw_length, bool) or not isinstance(raw_length, (int, str)):
        return None
    try:
        length = int(raw_length)
    except ValueError:
        return None
    starts = window.get("starts")
    start = as_date(release.get("date")) if starts in (None, "verified_release") else as_date(starts)
    return start + dt.timedelta(days=length) if start else None


def cards_done(cards: list[dict[str, Any]]) -> tuple[bool, dt.date | None]:
    if not cards or any(card.get("state") != "fresh" for card in cards):
        return False, None
    if not all(str(card.get("status")) in DONE_CARD_STATUSES for card in cards):
        return False, None
    finished = [as_date(card.get("completed_at")) for card in cards]
    finished = [d for d in finished if d]
    return True, max(finished) if finished else None


def reevaluation_due(frontmatter: dict[str, Any], stage: str, eval_date: dt.date | None, today: dt.date) -> bool:
    """A verdict with ``await_evidence`` whose observation window has ended: the user asked to look again.

    ``evaluation_due`` needs an unset verdict, so without this the cycle never comes back after that answer. The
    answer that keeps waiting moves the window, which moves the date and the review item's basis."""
    result = frontmatter.get("result") or {}
    disposition = frontmatter.get("disposition") or {}
    if not isinstance(result, dict) or not isinstance(disposition, dict):
        return False
    return (
        stage == "measuring"
        and disposition.get("action") == "await_evidence"
        and str(result.get("verdict") or "unset") != "unset"
        and eval_date is not None
        and today >= eval_date
    )


def spot_check_verdict_due(frontmatter: dict[str, Any], stage: str, release: dict[str, Any]) -> bool:
    """A verified cycle judged by a one-off check, not a window: no date will ever make it evaluation_due.

    Its criteria are set and it has no observation window, so the verdict is due once the release is verified.
    """
    measurement = frontmatter.get("measurement") or {}
    result = frontmatter.get("result") or {}
    if not isinstance(measurement, dict) or not isinstance(result, dict):
        return False
    return (
        stage == "measuring"
        and release["state"] == "verified"
        and bool(measurement.get("success_criteria"))
        and measurement.get("observation_window") is None
        and str(result.get("verdict") or "unset") == "unset"
    )


def disposition_due(frontmatter: dict[str, Any], stage: str, eval_date: dt.date | None, today: dt.date) -> bool:
    """A measuring cycle with a verdict but no disposition, once its observation window has ended (or it has none).

    ``evaluation_due`` needs an unset verdict and ``reevaluation_due`` needs ``await_evidence``, so a verdict recorded
    with "confirm in production first" otherwise never comes back to ask how the cycle ends."""
    result = frontmatter.get("result") or {}
    disposition = frontmatter.get("disposition") or {}
    if not isinstance(result, dict) or not isinstance(disposition, dict):
        return False
    return (
        stage == "measuring"
        and str(result.get("verdict") or "unset") != "unset"
        and str(disposition.get("action") or "unset") == "unset"
        and (eval_date is None or today >= eval_date)
    )


def cycle_flags(
    frontmatter: dict[str, Any],
    stage: str,
    release: dict[str, Any],
    eval_date: dt.date | None,
    observations: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    observe_every_days: int,
    invalid: bool,
    today: dt.date,
) -> list[str]:
    flags: list[str] = []
    measurement = frontmatter.get("measurement") or {}
    result = frontmatter.get("result") or {}
    disposition = frontmatter.get("disposition") or {}
    verdict = str(result.get("verdict") or "unset") if isinstance(result, dict) else "unset"
    if stage in ("decided", "shipping") and release["state"] != "verified":
        done, finished = cards_done(cards)
        if done and finished and (today - finished).days > RELEASE_GRACE_DAYS:
            flags.append("release_unverified")
    if stage in OPEN_STAGES and eval_date and today >= eval_date and verdict == "unset":
        flags.append("evaluation_due")
    if reevaluation_due(frontmatter, stage, eval_date, today):
        flags.append("reevaluation_due")
    if spot_check_verdict_due(frontmatter, stage, release):
        flags.append("verdict_due")
    if disposition_due(frontmatter, stage, eval_date, today):
        flags.append("disposition_due")
    if stage == "measuring":
        stamps = [
            str(row.get("evidence_date") or row.get("at") or "")[:10]
            for row in observations
            if row.get("kind") in ("metric", "guardrail")
        ]
        newest = as_date(max(stamps)) if stamps else None
        if newest is None or (today - newest).days > observe_every_days + STALE_GRACE_DAYS:
            flags.append("observation_stale")
    if stage in ("shipping", "measuring"):
        criteria = measurement.get("success_criteria") if isinstance(measurement, dict) else None
        if not criteria:
            flags.append("criteria_missing")
    if stage == "concluded" and isinstance(disposition, dict):
        if disposition.get("action") in ("continue", "iterate") and not as_list(disposition.get("follow_ups")):
            flags.append("no_next_action")
    if any(card.get("link") == "derived" for card in cards):
        flags.append("cards_unlinked")
    if invalid:
        flags.append("invalid")
    return flags


def next_action(
    frontmatter: dict[str, Any],
    stage: str,
    release: dict[str, Any],
    eval_date: dt.date | None,
    cards: list[dict[str, Any]],
    today: dt.date,
) -> str:
    explicit = frontmatter.get("next_action")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    result = frontmatter.get("result") or {}
    verdict = str(result.get("verdict") or "unset") if isinstance(result, dict) else "unset"
    if stage == "exploring":
        return "Decide whether to run this cycle"
    if stage in ("decided", "shipping"):
        open_cards = [c for c in cards if str(c.get("status")) not in DONE_CARD_STATUSES]
        if not cards:
            return "Link the Kanban cards that deliver this cycle"
        if open_cards:
            return f"Finish delivery: {len(open_cards)} of {len(cards)} cards open"
        if release["state"] == "failed":
            return "Fix the failed production release"
        return "Verify the production release"
    if reevaluation_due(frontmatter, stage, eval_date, today):
        return "Re-evaluate: record a verdict, or a new observation window to keep waiting"
    if disposition_due(frontmatter, stage, eval_date, today):
        return "Decide the disposition: conclude, iterate, or wait for more evidence"
    if stage == "measuring":
        if spot_check_verdict_due(frontmatter, stage, release):
            return "Evaluate the spot-check: record a verdict and a disposition"
        if eval_date is None:
            return "Set the observation window"
        if today >= eval_date and verdict == "unset":
            return "Evaluate: record a verdict and a disposition"
        return f"Measure until {eval_date.isoformat()}"
    if stage == "concluded":
        disposition = frontmatter.get("disposition") or {}
        action = disposition.get("action") if isinstance(disposition, dict) else None
        follow_ups = as_list(disposition.get("follow_ups")) if isinstance(disposition, dict) else []
        if action in ("continue", "iterate") and not follow_ups:
            return "Name the follow-up idea or successor cycle"
        if action == "await_evidence":
            return "Wait for more evidence, then re-evaluate"
        return "None"
    return "None"
