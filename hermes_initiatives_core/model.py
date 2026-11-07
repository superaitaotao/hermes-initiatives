"""Vocabulary of the initiative data model: stages, verdicts, dispositions, idea states, ID shapes."""

from __future__ import annotations

import re
from typing import Any

STAGES = ("exploring", "decided", "shipping", "measuring", "concluded", "abandoned")
OPEN_STAGES = ("exploring", "decided", "shipping", "measuring")
STAGE_RANK = {stage: rank for rank, stage in enumerate(STAGES)}

INITIATIVE_STATUSES = ("active", "paused", "closed")
VERDICTS = ("unset", "win", "no_effect", "regression", "mixed", "unevaluable")
DISPOSITIONS = ("unset", "continue", "iterate", "stop", "await_evidence")
IDEA_STATUSES = ("proposed", "reviewing", "promoted", "merged", "parked", "rejected")
OPEN_IDEA_STATUSES = ("proposed", "reviewing")
IDEA_SIZES = ("initiative", "cycle", "task")
# Bugs are filed explicitly (an agent through report_bug.py, or the user from chat), one file each under bugs/.
BUG_STATUSES = ("open", "triaged", "fixing", "fixed", "wontfix")
OPEN_BUG_STATUSES = ("open", "triaged", "fixing")
BUG_SEVERITIES = ("high", "medium", "low")
TASK_STATUSES = ("open", "triaged", "in_progress", "done", "cancelled")
OPEN_TASK_STATUSES = TASK_STATUSES[:3]
TASK_TYPES = ("task", "bug")
LEGACY_TASK_STATUSES = {"fixing": "in_progress", "fixed": "done", "wontfix": "cancelled"}
TASK_PRIORITIES = BUG_SEVERITIES
OBSERVATION_KINDS = ("release_verified", "release_failed", "metric", "guardrail", "note", "assessment")
ASSESSMENT_STATUSES = ("met", "missed", "insufficient_data", "measurement_failed")
RELEASE_KINDS = ("release_verified", "release_failed")

MEASUREMENT_FIELDS = (
    "primary_metric",
    "source",
    "baseline",
    "observation_window",
    "success_criteria",
    "guardrails",
    "insufficient_data",
)

# Fields only the user decides. Agents write observations and new ideas, never these. One exception: agents may
# append to ``kanban_cards`` through ``writers.link_cards`` (never remove); ``excluded_cards`` stops that per card.
USER_OWNED_CYCLE_FIELDS = (
    "stage",
    "measurement",
    "result",
    "disposition",
    "kanban_cards",
    "excluded_cards",
    "next_action",
    "outcome_review",
)
USER_OWNED_INITIATIVE_FIELDS = ("status", "rank", "focus_cycle", "goal", "watchers", "outcome_policy")

MAX_OPEN_IDEAS_PER_SOURCE = 5
# Cycles one initiative may hold in exploring or decided before the validator warns (2026-10-17 workflow review:
# 12 initiatives held 17 such cycles against 3 Kanban lane slots). A warning, never a refusal: stage is user-owned.
MAX_PRE_SHIPPING_CYCLES_PER_INITIATIVE = 2

# Decisions the user delegated to agents (user decisions 2026-10-13 and 2026-10-14; per item: auto_delegated).
# An agent decision row carries ``by: agent``, its ``rule``, and the review item it settled. It never counts as the
# user decision a stage needs.
AGENT_RULES = (
    "repair_promotion",
    "plan_exploring_cycle",
    "plan_after_cards_done",
    "plan_outcome_followup",
    "normalize_outcome_contract",
    "retire_suggestion",
    "withdraw_answered",
    "defer_exploring_cycle",
    # Park a decided cycle with no cards while the initiative's focus cycle is still before measuring: it never
    # settles, and the item returns by itself in ``sync`` (review_settle.focus_wait / return_focus_waits).
    "defer_behind_focus_cycle",
    "auto_delegated",  # one item the user marked auto in the Review tab or topic (review_auto)
    # An item idle for 3 days: the review job resolves it, or applies its recommended low/medium-risk option
    # (user delegation 2026-11-01, review_sweep).
    "stale_sweep",
)
# Rules appended from auto marks (review_learned): ``learned_<name>``. They settle, suspend, and restore like the
# coded rules above; the registry lives in reviews.db.
LEARNED_RULE = re.compile(r"^learned_[a-z][a-z0-9_]{3,45}$")


def is_agent_rule(rule: Any) -> bool:
    """A coded delegated rule, or one appended from an auto mark."""
    return rule in AGENT_RULES or bool(LEARNED_RULE.fullmatch(str(rule or "")))


CLOSED_STAGES = ("concluded", "abandoned")

INITIATIVE_ID = re.compile(r"^in-[a-z0-9]+(?:-[a-z0-9]+)*$")
CYCLE_ID = re.compile(r"^(in-[a-z0-9]+(?:-[a-z0-9]+)*)-c(\d{2,3})$")
IDEA_ID = re.compile(r"^idea-\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*$")
BUG_ID = re.compile(r"^bug-\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*$")
TASK_ID = re.compile(r"^task-\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*$")
CARD_ID = re.compile(r"^t_[0-9a-f]{8}$")
CHANGE_ROW_ID = re.compile(r"^C\d{2,3}$")
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
