"""Agent settlement: review items an agent may close itself, under rules the user delegated.

The user's words: repair ideas may be promoted into their existing cycle ("records by: agent, and cites the rule;
new scope, new cycles and rejections stay with me"), and an exploring cycle may get a plan card ("asks me only when
the plan is ready").

Each rule is checked here, against the files and the board, before anything is written. The model only picks the
rule and gives its reason; it cannot widen a rule. Every settlement writes an audit row (``by: agent``, ``rule``,
``review``, profile and run) where the user will see it, logs an action, and moves the item to ``settled``.

Rules:

- ``plan_after_cards_done``: a decided cards-done cycle gets the recommended plan-lane card, without changing stage.

- ``plan_outcome_followup``: a ready outcome review whose recommended option carries the exact signature
  ``cycle:outcome_review_ready:kanban_create:<plan lane>``, and whose every option stays off the review bar, gets
  one generated read-only investigation and repair-plan card. One protected alternative anywhere in the item (a
  verdict, disposition, stage, measurement or decision option, recommended or not) refuses the whole item.
- ``repair_promotion``: an open idea becomes ``promoted`` into a cycle that the user already decided (stage
  decided, shipping, or measuring) because it repairs one of that cycle's user decisions. The idea must not be
  initiative-sized. The agent cites the decision it repairs by index.
- ``plan_exploring_cycle``: an open ``exploring`` cycle item with no cards gets one plan card for the project's
  plan lane. The card asks for a plan only. The cycle stays ``exploring``; when the card is done, the collector
  raises the cycle again as ``plan_ready`` for the user. At most ``PLAN_STARTS_PER_DAY`` a day.
- ``retire_suggestion``: a suggestion the review itself made is closed because a newer item or decision replaced
  it. The agent names the replacing item.
- ``withdraw_answered``: a request an agent filed (``request:*``) or a suggestion the review made is closed because
  one of the user's recorded decisions already answers it. The agent cites the decision by
  file and index; the decision must be ``by: user`` and older than the request. Urgent consent items are refused.
- ``defer_behind_focus_cycle``: a decided cycle with no cards is parked (``deferred``, not settled) while its
  initiative's focus cycle is still before measuring. ``focus_wait`` is the shared predicate: the settle check and
  ``return_focus_waits`` both read it. The item comes back to the user by itself in ``sync`` when the focus cycle
  reaches measuring, or when ``focus_cycle`` changes. One overrule suspends the rule and returns the item.

A rule the user overruled is suspended (``ReviewStore.rule_states``) and refuses everything until the user restores it.

``reopen_deferral`` is not a decision: it returns a deferred item to the user when the condition the deferral named
is met but its source basis did not change (for example "until PR #341 merges").
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import review_collect, review_learn, reviews
from .kanban import CYCLE_MARKER
from .lifecycle import effective_stage, release_state
from .model import AGENT_RULES, CYCLE_ID, INITIATIVE_ID, LEARNED_RULE, OPEN_IDEA_STATUSES
from .review_exec import ActionFailed, Kanban, Runner, default_runner
from .reviews import ReviewError, ReviewStore, basis_hash
from .settings import ProjectSettings, load_settings
from .store import active_observations, load
from .views import build_detail
from .writers import caller_identity, edit_frontmatter, link_cards

DECIDED_STAGES = ("decided", "shipping", "measuring")
PLAN_STARTS_PER_DAY = 3
PLAN_AFTER_DONE_PER_DAY = 3

# ``defer_behind_focus_cycle``: park a decided cycle with no cards while the initiative's focus cycle is still before
# measuring. The anchor is ``focus_cycle``, a field only the user sets, so the rule only ever says "not now".
RULE = "defer_behind_focus_cycle"
FOCUS_WAIT_STAGES = ("exploring", "decided", "shipping")
FOCUS_SIGNATURE = "cycle:no_cards:defer:state_change"
# The option actions this rule tolerates: everything else carries a human gate (unblock, attend, merge, archive,
# reject, handoff, rule state) and refuses.
FOCUS_OPTION_ACTIONS = ("defer", "acknowledge", "record_decision", "set_cycle_fields", "kanban_create")


class SettleRefused(ValueError):
    """The rule does not cover this item; nothing was written."""


class FocusWaitRefused(ValueError):
    """The initiative's focus cycle does not hold a ``defer_behind_focus_cycle`` wait; nothing was written."""


def _audit_row(rule: str, short_id: str, decision: str, why: str, **extra: Any) -> dict[str, Any]:
    profile, run = caller_identity()
    row: dict[str, Any] = {
        "date": dt.date.today().isoformat(),
        "by": "agent",
        "rule": rule,
        "review": short_id,
        "decision": decision,
        "reason": why,
        "agent": f"{profile} {run}",
    }
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    return row


def _append_decision(project: ProjectSettings, cycle_id: str, row: dict[str, Any]) -> None:
    _append_row(project, load(project.root).cycles[cycle_id].path, row)


def _append_row(project: ProjectSettings, path: Any, row: dict[str, Any]) -> None:
    def change(fm: dict[str, Any]) -> None:
        fm["decisions"] = [*(fm.get("decisions") or []), row]

    after = edit_frontmatter(path, project.root, change)
    if (after.get("decisions") or [])[-1:] != [row]:
        raise ActionFailed(f"audit row not found in {path.name} on read-back")


def _user_decisions(fm: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in fm.get("decisions") or [] if isinstance(d, dict) and d.get("by") == "user"]


class Settler:
    def __init__(self, store: ReviewStore, project: ProjectSettings, run: Runner | None = None) -> None:
        self.store = store
        self.project = project
        self.owner_name = load_settings()[0].owner_name
        self.kanban = Kanban(run or default_runner)

    def settle(self, short_id: str, rule: str, args: dict[str, Any], why: str) -> dict[str, Any]:
        """Check *rule* against the item and its sources, apply it, and settle the item. Raises SettleRefused."""
        if rule == "auto_delegated":
            raise SettleRefused("auto_delegated items are handled with `daily_review.py auto`, not settle")
        if rule == "stale_sweep":
            raise SettleRefused("stale items are handled with `daily_review.py sweep`, not settle")
        if LEARNED_RULE.fullmatch(rule or ""):
            # A rule appended from an auto mark (review_learned): it picks one of the item's own options.
            from .review_auto import AutoRefused, settle_learned

            try:
                return settle_learned(self.store, short_id, rule, args or {}, why, run=self.kanban.run, project=self.project)
            except AutoRefused as exc:
                raise SettleRefused(str(exc)) from exc
        if rule == "defer_exploring_cycle":
            from .review_deferral import park

            return park(self.store, self.project, short_id, args, why)
        if not isinstance(why, str):
            # JSON callers can pass a number, list, or null; refuse it as input instead of raising AttributeError.
            raise SettleRefused("why must say in a sentence which approved fact makes this rule apply")
        why = why.strip()
        if rule not in AGENT_RULES:
            raise SettleRefused(f"unknown rule '{rule}'; delegated rules: {', '.join(AGENT_RULES)}")
        suspended = self.store.rule_states().get(rule)
        if suspended and suspended["state"] == "suspended":
            raise SettleRefused(f"rule {rule} is suspended since {suspended['at'][:10]}: {suspended['reason']}")
        if len(why) < 20:
            raise SettleRefused("why must say in a sentence which approved fact makes this rule apply")
        item = self.store.get(short_id)
        if item is None:
            raise SettleRefused(f"unknown item {short_id}")
        if item["status"] != "open":
            # Deferred, watching, and answered items carry a user choice or are not due; agents do not touch them.
            raise SettleRefused(f"{item['short_id']} is {item['status']}; only open items can be settled")
        if item.get("project") not in (None, self.project.id):
            raise SettleRefused(f"{item['short_id']} belongs to project {item.get('project')}")
        if rule == "plan_after_cards_done":
            return self._plan_after_cards_done(item, args or {}, why)
        if rule == "plan_outcome_followup":
            from .review_outcome_settle import followup

            return followup(self, item, args or {}, why)
        if rule == "normalize_outcome_contract":
            from .review_contract_normalize import normalize

            return normalize(self, item, args or {}, why)
        if rule == RULE:
            return self._defer_behind_focus_cycle(item, args or {}, why)
        receipt = getattr(self, f"_{rule}")(item, args or {}, why)
        if not self.store.settle(item["short_id"], rule, receipt):
            raise SettleRefused(f"{item['short_id']} changed while settling; it is no longer open")
        return {"short_id": item["short_id"], "rule": rule, "receipt": receipt}

    # --- rules ---------------------------------------------------------------------------------------------

    def _repair_promotion(self, item: dict[str, Any], args: dict[str, Any], why: str) -> str:
        if item["source"] != "idea":
            raise SettleRefused("repair_promotion applies to idea items only")
        snapshot = load(self.project.root)
        idea_id = str((item.get("basis") or {}).get("idea") or "")
        doc = snapshot.ideas.get(idea_id)
        if doc is None or str(doc.frontmatter.get("status")) not in OPEN_IDEA_STATUSES:
            raise SettleRefused(f"idea {idea_id or '?'} is not open")
        if str(doc.frontmatter.get("size")) == "initiative":
            raise SettleRefused("an initiative-sized idea is new scope; the user decides it")
        into = str(args.get("into") or "")
        if not CYCLE_ID.fullmatch(into) or into not in snapshot.cycles:
            raise SettleRefused(f"into must be an existing cycle, got '{into}'")
        cycle = snapshot.cycles[into].frontmatter
        if str(cycle.get("stage")) not in DECIDED_STAGES:
            stage = cycle.get("stage")
            raise SettleRefused(f"{into} is {stage}; a repair goes only into a decided, shipping, or measuring cycle")
        related = doc.frontmatter.get("related_initiative")
        if related and snapshot.cycle_initiative.get(into) != related:
            raise SettleRefused(f"{into} is not a cycle of the idea's initiative {related}")
        decisions = _user_decisions(cycle)
        index = args.get("repairs_decision")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(decisions):
            raise SettleRefused(f"repairs_decision must index one of {into}'s {len(decisions)} user decisions (0-based)")
        repaired = str(decisions[index].get("decision") or "")
        reason = f"Agent rule repair_promotion (review {item['short_id']}): repairs '{repaired[:200]}'. {why}"

        def change(fm: dict[str, Any]) -> None:
            fm.update({"status": "promoted", "promoted_to": into, "reason": reason})

        after = edit_frontmatter(doc.path, self.project.root, change)
        if after.get("status") != "promoted" or after.get("promoted_to") != into:
            raise ActionFailed(f"{idea_id} read back as {after.get('status')} -> {after.get('promoted_to')}")
        _append_decision(
            self.project,
            into,
            _audit_row(
                "repair_promotion",
                item["short_id"],
                f"Promoted idea {idea_id} into this cycle as a repair of an approved decision",
                why,
                idea=idea_id,
                repairs=repaired[:300],
            ),
        )
        return f"{idea_id} -> promoted into {into} (repairs user decision {index})"

    def _plan_exploring_cycle(self, item: dict[str, Any], args: dict[str, Any], why: str) -> str:
        if item["source"] != "cycle":
            raise SettleRefused("plan_exploring_cycle applies to cycle items only")
        reasons = (item.get("basis") or {}).get("reasons") or []
        if "exploring" not in reasons or "plan_ready" in reasons:
            raise SettleRefused("the cycle must be exploring with no finished plan; a ready plan goes to the user")
        cycle_id = str(item.get("cycle") or "")
        snapshot = load(self.project.root)
        doc = snapshot.cycles.get(cycle_id)
        if doc is None or str(doc.frontmatter.get("stage")) != "exploring":
            raise SettleRefused(f"{cycle_id} is not an exploring cycle")
        if doc.frontmatter.get("kanban_cards"):
            raise SettleRefused(f"{cycle_id} already has cards; reconcile those instead")
        board, lane = self.project.kanban_board, self.project.plan_assignee
        if not board or not lane:
            raise SettleRefused("the project settings need kanban_board and plan_assignee")
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).replace(microsecond=0).isoformat()
        started = [a for a in self.store.actions_since(since) if a["action"] == "settle:plan_exploring_cycle"]
        if len(started) >= PLAN_STARTS_PER_DAY:
            raise SettleRefused(f"{PLAN_STARTS_PER_DAY} plans were started in the last 24 hours; this one waits")
        fm = doc.frontmatter
        body = "\n".join(
            [
                f"- Initiative cycle: {cycle_id}",
                "",
                f"Write the plan for exploring cycle `{cycle_id}` ({fm.get('title') or ''}). Cycle file: {doc.path}",
                f"Question: {fm.get('question') or '-'}",
                f"Hypothesis: {fm.get('hypothesis') or '-'}",
                "",
                "Scope: a plan for the user to approve or reject. Read-only analysis that the plan needs is allowed.",
                "- Do not implement, change config, data, or credentials, or create delivery or reconcile cards.",
                "- Do not route the plan for approval: starting the cycle is the user's decision. Write the plan in the",
                "  vault, attach it, and complete this card with its path and SHA-256. The daily review then asks the",
                "  user to approve it.",
                "- Propose a primary metric, baseline, success rule, and guardrail for the user to accept.",
                "- Include a target date, measurement window/minimum sample, executable collection procedure and owner,",
                "  latest review date, review triggers, budget and stop conditions. Mark these as proposed, not decided.",
                "- Carry the outcome_review contract into the approval proposal; delivery alone is not outcome proof.",
                "",
                f"Started by the daily review under the user's rule plan_exploring_cycle (review {item['short_id']}).",
                f"Why now: {why}",
            ]
        )
        card = self.kanban.create(
            board,
            title=f"Plan {cycle_id}: {fm.get('title') or ''}"[:200],
            assignee=lane,
            body=body,
            key=f"review-settle:{item['short_id']}:{item['basis_hash']}",
            skills=self.project.plan_skills,
        )
        try:
            link_cards(self.project.root, board, cycle_id, [card])
        except Exception as exc:  # noqa: BLE001 - the card exists; report the link failure, do not hide it
            raise ActionFailed(f"created {card} but could not link it to {cycle_id}: {exc}") from exc
        _append_decision(
            self.project,
            cycle_id,
            _audit_row(
                "plan_exploring_cycle",
                item["short_id"],
                f"Started plan card {card} for {lane}; the cycle stays exploring until the user approves the plan",
                why,
                card=card,
            ),
        )
        return f"plan card {card} for {lane}, linked to {cycle_id}"

    def _plan_after_cards_done(self, item: dict[str, Any], args: dict[str, Any], why: str) -> dict[str, Any]:
        rule = "plan_after_cards_done"
        if args:
            raise SettleRefused(f"{rule} takes no arguments")
        board, lane = self.project.kanban_board, self.project.plan_assignee
        if not board or not lane:
            raise SettleRefused("project settings need kanban_board and plan_assignee")
        if item.get("project") != self.project.id:
            raise SettleRefused("the item must have this exact project attribution")
        if item.get("section") != "gated" or item.get("source") != "cycle":
            raise SettleRefused("only a gated cycle item can create the next plan card")
        cycle_id = str(item.get("cycle") or "")
        if not CYCLE_ID.fullmatch(cycle_id) or item.get("key") != f"cycle:{self.project.id}:{cycle_id}:gate":
            raise SettleRefused("the item key must name its own project and cycle")
        if any(item.get(k) is not None for k in ("urgent_rule", "owner_profile", "owner_job")) or item.get("blocks") != []:
            raise SettleRefused("the item carries urgency, an owner, or blocked work")
        basis = item.get("basis") or {}
        if (
            basis.get("reasons") != ["cards_done"]
            or basis.get("stage") != "decided"
            or basis.get("verdict") != "unset"
            or basis.get("disposition") != "unset"
            or not isinstance(basis.get("cards_done"), list)
            or not basis["cards_done"]
        ):
            raise SettleRefused("the basis must be a decided cards_done cycle with unset verdict and disposition")
        short_id = item["short_id"]
        if not reviews.current(item) or short_id in {i["short_id"] for i in self.store.needs_options()}:
            raise SettleRefused("the options are not current; the next tick rewrites them")
        if self.store.answers(short_id):
            raise SettleRefused("the user already answered this item")
        version = item["version"]
        options = version["options"]
        chosen = [o for o in options if o.get("id") == version.get("recommend")]
        if len(chosen) != 1 or chosen[0].get("action") != "kanban_create":
            raise SettleRefused("the recommended option must be one kanban_create")
        option = chosen[0]
        for candidate in options:
            if candidate.get("confirm"):
                raise SettleRefused("an option needs confirm; the user answers it")
        card_args = option.get("args") or {}
        if set(card_args) - {"board", "assignee", "title", "body"}:
            raise SettleRefused("the recommended option has unsupported arguments")
        if card_args.get("assignee") != lane:
            raise SettleRefused("the recommended assignee must be the plan lane")
        if "board" in card_args and card_args["board"] != board:
            raise SettleRefused("the recommended board must be the project board")
        title = card_args.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise SettleRefused("the recommended title must be non-empty and at most 200 characters")
        expected = f"cycle:cards_done:kanban_create:{lane}"
        if review_learn.signature(item, option) != expected:
            raise SettleRefused(f"the recommended signature must be {expected}")
        option_body = str(card_args.get("body") or "")
        if any(marker.lower() != cycle_id for text in (title, option_body) for marker in CYCLE_MARKER.findall(text)):
            raise SettleRefused("every Initiative cycle marker must name this cycle")
        for candidate in options:
            if candidate.get("action") not in {"defer", "acknowledge", "record_decision", "set_cycle_fields", "kanban_create"}:
                raise SettleRefused(f"option {candidate.get('id')} carries a human gate")
        snapshot = load(self.project.root)
        if snapshot.parse_errors or snapshot.duplicate_ids:
            raise SettleRefused("the sources are malformed or ambiguous")
        fresh = next(
            (c for c in review_collect._cycle_gates(self.project, snapshot, dt.date.today()) if c["key"] == item["key"]), None
        )
        if fresh is None or basis_hash(fresh["basis"]) != item["basis_hash"]:
            raise SettleRefused("stale collector candidate or basis; the next tick rewrites this item")
        doc = snapshot.cycles.get(cycle_id)
        owner = snapshot.cycle_initiative.get(cycle_id)
        if doc is None or doc.frontmatter.get("stage") != "decided":
            raise SettleRefused("the recorded cycle must still be decided")
        initiative = snapshot.initiatives.get(owner)
        if owner != item.get("initiative") or initiative is None:
            raise SettleRefused("the cycle must belong to the item's initiative")
        if initiative.frontmatter.get("status") != "active":
            raise SettleRefused("the initiative must be active")
        detail = build_detail(self.project, owner)
        view = next((c for c in detail["cycles"] if c["id"] == cycle_id), None) if detail else None
        if view is None or detail["freshness"]["kanban"] != "fresh":
            raise SettleRefused("the cycle must have fresh Kanban sources")
        opened = str(item["opened_at"])[:10]
        for source in (doc.frontmatter, initiative.frontmatter):
            if any(str(row.get("date") or "") >= opened for row in _user_decisions(source)):
                raise SettleRefused("a user decision is dated on or after this item opened")
        decisions = doc.frontmatter.get("decisions") or []
        previous = [i for i, row in enumerate(decisions) if isinstance(row, dict) and row.get("rule") == rule]
        if previous and not _user_decisions({"decisions": decisions[previous[-1] + 1 :]}):
            raise SettleRefused("chain guard: the previous rule card needs a later user decision")
        failed = []
        for action in self.store.actions_since(None):
            if action["action"] != f"settle:{rule}" or action["state"] != "failed":
                continue
            prior_item = self.store.get(action["short_id"])
            if prior_item and prior_item["key"] == item["key"]:
                failed.append(action["at"][:10])
        if failed and not any(str(row.get("date") or "") > max(failed) for row in _user_decisions(doc.frontmatter)):
            raise SettleRefused("chain guard: the failed rule action needs a strictly later user decision")
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).replace(microsecond=0).isoformat()
        if len([a for a in self.store.actions_since(since) if a["action"] == f"settle:{rule}"]) >= PLAN_AFTER_DONE_PER_DAY:
            raise SettleRefused(f"{PLAN_AFTER_DONE_PER_DAY} rule actions exist in the last 24 hours")
        body = option_body
        if not CYCLE_MARKER.search(body):
            body = f"- Initiative cycle: {cycle_id}\n\n{body}"
        body += (
            f"\n\nStarted by the daily review under {self.owner_name}'s rule {rule} "
            f"(review {short_id}, recommended option {option['id']}: {option['label']}).\n"
            f"Why now: {why}\nEvery card of {cycle_id} is done. Do this card's step only. "
            f"Plan approval, high-risk gates, merges, and stage changes stay with {self.owner_name}.\n"
            f"Context from the review:\n{version.get('context') or ''}"
        )
        if not self.store.claim_under_rule(short_id, basis_hash=item["basis_hash"], version=version["n"]):
            raise SettleRefused("the item changed while settling; the claim was refused")
        card = None
        try:
            card = self.kanban.create(
                board,
                title=title,
                assignee=lane,
                body=body,
                key=f"review-settle:{short_id}:{item['basis_hash']}",
                skills=self.project.plan_skills,
            )
            link_cards(self.project.root, board, cycle_id, [card])
            _append_decision(
                self.project,
                cycle_id,
                _audit_row(
                    rule,
                    short_id,
                    f"Created {card} for {lane} under the recommended option {option['id']}; the cycle stays decided",
                    why,
                    card=card,
                ),
            )
            receipt = f"{card} for {lane}, linked to {cycle_id}"
            if not self.store.settle(short_id, rule, receipt, expect=("applying",)):
                raise ActionFailed("the claimed item could not be settled")
        except Exception as exc:  # noqa: BLE001 - release every failed claim, retaining any created card in the error
            error = f"{card}: {exc}" if card else str(exc)
            self.store.release_rule_claim(short_id, rule=rule, error=error)
            raise ActionFailed(f"{short_id}: {rule} failed: {error}") from exc
        return {"short_id": short_id, "rule": rule, "receipt": receipt}

    def _retire_suggestion(self, item: dict[str, Any], args: dict[str, Any], why: str) -> str:
        if item["section"] != "suggested" or item["source"] != "suggestion":
            raise SettleRefused("retire_suggestion applies to the review's own suggestions only")
        replaced_by = str(args.get("replaced_by") or "").upper()
        other = self.store.get(replaced_by) if replaced_by else None
        if other is None or other["short_id"] == item["short_id"]:
            raise SettleRefused("replaced_by must name another review item (Dnn) that carries the decision now")
        if item.get("evidence_hash"):
            self.store.dismiss(item["key"], evidence_hash=item["evidence_hash"])
        return f"replaced by {other['short_id']} ({other['status']})"

    def _withdraw_answered(self, item: dict[str, Any], args: dict[str, Any], why: str) -> str:
        source = str(item.get("source") or "")
        if not (source.startswith("request:") or (item["section"] == "suggested" and source == "suggestion")):
            raise SettleRefused("withdraw_answered applies to agent-filed requests and the review's own suggestions only")
        if item.get("urgent_rule"):
            raise SettleRefused(f"{item['short_id']} carries urgent rule {item['urgent_rule']}; the user answers it")
        ref = str(args.get("in") or "")
        snapshot = load(self.project.root)
        if CYCLE_ID.fullmatch(ref) and ref in snapshot.cycles:
            doc = snapshot.cycles[ref]
        elif INITIATIVE_ID.fullmatch(ref) and ref in snapshot.initiatives:
            doc = snapshot.initiatives[ref]
        else:
            raise SettleRefused(f"in must be an existing cycle or initiative id, got '{ref}'")
        decisions = _user_decisions(doc.frontmatter)
        index = args.get("decision")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(decisions):
            raise SettleRefused(f"decision must index one of {ref}'s {len(decisions)} user decisions (0-based)")
        row = decisions[index]
        if row.get("review") == item["short_id"]:
            raise SettleRefused("that decision answered this item; it is not an earlier record")
        if str(row.get("date") or "") > str(item.get("opened_at") or "")[:10]:
            raise SettleRefused(f"{ref} decision {index} ({row.get('date')}) is newer than the request; the user saw both")
        answer = str(row.get("decision") or "")
        _append_row(
            self.project,
            doc.path,
            _audit_row(
                "withdraw_answered",
                item["short_id"],
                f"Withdrew request {item['short_id']} ({item['title'][:120]}): the user decision of {row.get('date')} "
                "already answers it",
                why,
                answered_by=f"{ref} decisions[by: user][{index}]",
            ),
        )
        if item.get("evidence_hash"):
            self.store.dismiss(item["key"], evidence_hash=item["evidence_hash"])
        return f"answered by {ref} user decision {index} ({row.get('date')}): '{answer[:160]}'"

    def _defer_behind_focus_cycle(self, item: dict[str, Any], args: dict[str, Any], why: str) -> dict[str, Any]:
        """Park a decided cycle with no cards while its initiative's focus cycle is still before measuring."""
        if args:
            raise SettleRefused(f"{RULE} takes no arguments; got {', '.join(sorted(map(str, args)))}")
        cycle_id = str(item.get("cycle") or "")
        if item.get("project") != self.project.id:
            # Appendix E.3: exact project equality. The shared guard at the top of ``settle`` tolerates a missing
            # attribution for older rules; this rule may only park an item whose project is exactly this one.
            raise SettleRefused(
                f"{item['short_id']} has no project attribution ({item.get('project')}); {RULE} parks an item of "
                f"project {self.project.id} only"
            )
        if item.get("section") != "gated" or item.get("source") != "cycle":
            raise SettleRefused("only a gated cycle item can park behind its focus cycle")
        if not CYCLE_ID.fullmatch(cycle_id) or item.get("key") != f"cycle:{self.project.id}:{cycle_id}:gate":
            raise SettleRefused(f"the item key must be cycle:{self.project.id}:<cycle id>:gate, got '{item.get('key')}'")
        if item.get("urgent_rule") or item.get("owner_profile") or item.get("owner_job") or item.get("blocks"):
            raise SettleRefused(f"{item['short_id']} carries urgency, an owner, or blocked work; the user answers it himself")
        basis = item.get("basis") or {}
        if (
            basis.get("reasons") != ["no_cards"]
            or basis.get("stage") != "decided"
            or basis.get("verdict") != "unset"
            or basis.get("disposition") != "unset"
        ):
            raise SettleRefused(
                "the basis must be a decided cycle whose only reason is ['no_cards'], with verdict and disposition unset"
            )
        if not reviews.current(item) or item["short_id"] in {i["short_id"] for i in self.store.needs_options()}:
            raise SettleRefused(f"{item['short_id']}'s options are not current; the next tick rewrites them")
        self._focus_recommendation(item)
        snapshot = load(self.project.root)
        if snapshot.parse_errors or snapshot.duplicate_ids:
            raise SettleRefused(f"the sources under {self.project.root} are malformed or ambiguous")
        doc = snapshot.cycles.get(cycle_id)
        owner = snapshot.cycle_initiative.get(cycle_id)
        if doc is None or owner is None or item.get("initiative") != owner:
            raise SettleRefused(f"{cycle_id} is not a cycle of initiative {item.get('initiative')} in the project sources")
        candidate = next(
            (c for c in review_collect._cycle_gates(self.project, snapshot, dt.date.today()) if c["key"] == item["key"]),
            None,
        )
        if candidate is None or basis_hash(candidate["basis"]) != item["basis_hash"]:
            raise SettleRefused("stale collector candidate or basis; the next tick rewrites this item")
        self._focus_cycle_source(item, doc, owner, snapshot)
        try:
            focus = focus_wait(self.project, snapshot, cycle_id)
        except FocusWaitRefused as exc:
            raise SettleRefused(str(exc)) from exc
        return self._park_behind_focus(item, doc, focus, why)

    def _focus_recommendation(self, item: dict[str, Any]) -> None:
        """The item's own options must be this one step, and no earlier answer of the user's may say otherwise."""
        version = item.get("version") or {}
        options = version.get("options") or []
        chosen = [o for o in options if o.get("id") == version.get("recommend")]
        if len(chosen) != 1 or chosen[0].get("action") != "defer" or chosen[0].get("args") != {"kind": "state_change"}:
            raise SettleRefused("the recommended option must be the one defer with args {'kind': 'state_change'}")
        signature = review_learn.signature(item, chosen[0])
        if signature != FOCUS_SIGNATURE:
            raise SettleRefused(f"the recommended option's signature is '{signature}', not '{FOCUS_SIGNATURE}'")
        for option in options:
            if option.get("action") not in FOCUS_OPTION_ACTIONS:
                raise SettleRefused(
                    f"option {option.get('id')} uses '{option.get('action')}', which carries a human gate; "
                    "this rule refuses that item"
                )
        for answer in self.store.answers(item["short_id"]):
            shown = self.store.versions(item["short_id"])[answer["version"] - 1]
            taken = next((o for o in shown["options"] if o["id"] == answer["option"]), None)
            if taken is None or taken.get("action") != "defer" or str(answer.get("note") or "").strip():
                raise SettleRefused(
                    f"answer {answer['id']} on {item['short_id']} was not an empty defer; the user already decided this item"
                )

    def _focus_cycle_source(self, item: dict[str, Any], doc: Any, owner: str, snapshot: Any) -> None:
        """The target cycle, its initiative, and the user's newer decisions must all still allow this park."""
        cycle_id = str(doc.frontmatter.get("id") or item.get("cycle"))
        fm = doc.frontmatter
        if str(fm.get("stage")) != "decided":
            raise SettleRefused(f"{cycle_id} is {fm.get('stage')}; this rule parks a decided cycle only")
        if fm.get("kanban_cards"):
            raise SettleRefused(f"{cycle_id} already has cards linked; reconcile those instead")
        if str(fm.get("next_action") or "").strip():
            raise SettleRefused(f"{cycle_id} already names a next action: {str(fm['next_action'])[:120]}")
        detail = build_detail(self.project, owner)
        view = next((c for c in detail["cycles"] if c["id"] == cycle_id), None) if detail else None
        if view is None or view["cards"] or (self.project.kanban_board and detail["freshness"]["kanban"] != "fresh"):
            raise SettleRefused(f"{cycle_id} has a derived card, or the card source cannot be read")
        initiative = snapshot.initiatives.get(owner)
        status = str((initiative.frontmatter.get("status") if initiative else None) or "unknown")
        if status != "active":
            raise SettleRefused(f"initiative {owner} is {status}; only an active initiative parks behind its focus cycle")
        opened = str(item.get("opened_at") or "")[:10]
        for ref, source in ((cycle_id, fm), (owner, initiative.frontmatter)):
            for row in _user_decisions(source):
                if opened and str(row.get("date") or "") >= opened:
                    raise SettleRefused(
                        f"{ref} records a user decision on {row.get('date')}, on or after this item opened "
                        f"({opened}); the user saw both"
                    )

    def _park_behind_focus(self, item: dict[str, Any], doc: Any, focus: dict[str, Any], why: str) -> dict[str, Any]:
        """Claim the item, write the agent audit row, then park it: one deferral and one verified receipt."""
        short_id = item["short_id"]
        version = (item.get("version") or {}).get("n")
        if not self.store.claim_under_rule(short_id, basis_hash=item["basis_hash"], version=version):
            raise SettleRefused(f"{short_id} changed while settling; it is no longer open with these options")
        row = _audit_row(
            RULE,
            short_id,
            f"Parked this cycle's review: {focus['cycle']} is the focus cycle at {focus['stage']}",
            why,
            focus_cycle=focus["cycle"],
            focus_stage=focus["stage"],
        )
        try:
            _append_row(self.project, doc.path, row)
            receipt = self.store.park_under_rule(
                short_id,
                rule=RULE,
                basis_hash=item["basis_hash"],
                version=version,
                reason=why,
                receipt={
                    "focus_cycle": focus["cycle"],
                    "focus_stage": focus["stage"],
                    "basis_hash": item["basis_hash"],
                    "version": version,
                    "agent": row["agent"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - any failure must return the claim, never leave a silent park
            self.store.release_rule_claim(short_id, rule=RULE, error=str(exc))
            raise ActionFailed(f"{short_id}: {RULE} did not park: {exc}") from exc
        return {"short_id": short_id, "rule": RULE, "status": "deferred", "receipt": receipt}


def reopen_deferral(store: ReviewStore, short_id: str, reason: str, project: ProjectSettings | None = None) -> dict[str, Any]:
    """Return a deferred item to the user because the deferral's named condition is met. Not a decision."""
    reason = (reason or "").strip()
    if len(reason) < 20:
        raise ReviewError("reason must name the deferral's condition and the evidence that it is met")
    item = store.get(short_id)
    if item is None:
        raise ReviewError(f"unknown item {short_id}")
    if item["status"] != "deferred":
        raise ReviewError(f"{item['short_id']} is {item['status']}, not deferred")
    from .review_deferral import current_operation, reopen

    with store._connect() as conn:
        owned = current_operation(conn, item["short_id"])
    if owned:
        if project is None or project.id != item["project"]:
            raise ReviewError("configured project required for rule-owned reopening")
        try:
            return reopen(store, project, item["short_id"], reason)
        except ValueError as exc:
            raise ReviewError(str(exc)) from exc
    if not store.reopen(item["short_id"]):
        raise ReviewError(f"{item['short_id']} changed; not reopened")
    shown = (item.get("version") or {}).get("n", 0)
    store.log_action(None, item["short_id"], "reopen_deferral", {"version": shown}, "reopened", receipt=reason[:2000])
    # The next ``tick`` prints it (needs_options counts the reopen), so it gets options that see the met condition.
    return {"short_id": item["short_id"], "status": "open", "reason": reason}


def focus_wait(project: ProjectSettings, snapshot: Any, cycle_id: str) -> dict[str, Any]:
    """The initiative's focus cycle and its effective stage while the wait holds, else ``FocusWaitRefused``.

    ``focus_cycle`` is a field only the user sets (``USER_OWNED_INITIATIVE_FIELDS``), so this reads the user's own priority
    instead of inferring one from prose. The wait holds while the focus cycle is a different cycle of the same
    initiative and has not started measuring yet; a verified release already makes it measuring
    (``lifecycle.effective_stage``)."""
    owner = snapshot.cycle_initiative.get(cycle_id) if CYCLE_ID.fullmatch(cycle_id or "") else None
    doc = snapshot.initiatives.get(owner) if owner else None
    if doc is None:
        raise FocusWaitRefused(f"{cycle_id} is not a cycle of any initiative in these sources")
    if not doc.path.resolve().is_relative_to(project.root.resolve()):
        raise FocusWaitRefused(f"the initiative {owner} of {cycle_id} is outside {project.id}")
    focus = str(doc.frontmatter.get("focus_cycle") or "")
    if not focus:
        raise FocusWaitRefused(f"{owner} has no focus cycle set, so there is nothing to wait behind")
    if focus == cycle_id:
        raise FocusWaitRefused(f"{owner}'s focus cycle is this item's own cycle ({cycle_id})")
    focus_doc = snapshot.cycles.get(focus)
    if focus_doc is None or snapshot.cycle_initiative.get(focus) != owner:
        raise FocusWaitRefused(f"focus cycle '{focus}' is not a cycle of {owner}")
    stage = effective_stage(focus_doc.frontmatter, release_state(active_observations(snapshot.observations.get(focus))))
    if stage not in FOCUS_WAIT_STAGES:
        raise FocusWaitRefused(f"the focus cycle {focus} is {stage}; the wait ends when it reaches measuring")
    return {"cycle": focus, "stage": stage}


def _park_receipt(store: ReviewStore, short_id: str) -> dict[str, Any] | None:
    """The receipt of the newest ``settle:defer_behind_focus_cycle`` park on *short_id*, or None."""
    for action in reversed(store.actions(short_id)):
        if action["action"] == f"settle:{RULE}" and action["state"] == "deferred":
            try:
                receipt = json.loads(action.get("receipt") or "{}")
            except ValueError:
                return None
            return receipt if isinstance(receipt, dict) else None
    return None


def _focus_wait_stopped(item: dict[str, Any], receipt: dict[str, Any], projects: list[ProjectSettings]) -> str:
    """Why this rule's wait no longer holds, or '' while it still does. Unreadable sources return the item."""
    project = next((p for p in projects if p.id == item.get("project")), None)
    if project is None:
        return f"no configured project '{item.get('project')}' reads the focus cycle"
    try:
        snapshot = load(project.root)
        if snapshot.parse_errors or snapshot.duplicate_ids:
            raise FocusWaitRefused("the initiative sources are malformed or ambiguous")
        current = focus_wait(project, snapshot, str(item.get("cycle") or ""))
    except (FocusWaitRefused, OSError, ValueError, KeyError) as exc:
        return str(exc)
    if current["cycle"] != receipt.get("focus_cycle"):
        return (
            f"the focus cycle is now {current['cycle']} at {current['stage']}; "
            f"{receipt.get('focus_cycle')} was the focus cycle when this item was parked"
        )
    return ""


def return_focus_waits(store: ReviewStore, projects: list[ProjectSettings]) -> list[str]:
    """Return the parks this rule owns once its focus wait stops holding. Other deferrals are never touched.

    The user's own park and the parks of other rules keep their newest deferral id elsewhere, so only an item whose
    newest deferral is this rule's park is read; the mutation binds to that same deferral id inside its transaction,
    so a deferral written while the sources were read supersedes the park instead of being returned. A return is not
    a decision: it logs a ``reopen_deferral`` with the version it saw, so the next tick asks for fresh options
    (``ReviewStore.needs_options``)."""
    reopened: list[str] = []
    for item in store.items(statuses=("deferred",)):
        receipt = _park_receipt(store, item["short_id"])
        if receipt is None or store.latest_deferral_id(item["short_id"]) != receipt.get("deferral_id"):
            continue
        reason = _focus_wait_stopped(item, receipt, projects)
        version = (item.get("version") or {}).get("n")
        # The park's own deferral id is passed into the mutation, which re-checks ownership in its transaction: a
        # deferral written while the sources above were read supersedes this park and is never returned.
        if reason and store.reopen_rule_deferral(
            item["short_id"], rule=RULE, version=version, reason=f"{RULE}: {reason}", deferral_id=receipt["deferral_id"]
        ):
            reopened.append(item["short_id"])
    return reopened
