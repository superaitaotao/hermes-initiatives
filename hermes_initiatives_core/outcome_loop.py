"""Closed-loop outcome assessment: the input epoch, readiness, and the durable dispatch of assessment work.

The cycle file owns the workflow contract (``outcome_review``) and the append-only list of assessment cards
(``assessment_cards``). Nothing here is stored. :func:`derive` is pure — it reads dicts and returns a decision —
and :func:`dispatch` only creates Kanban cards through the CLI and appends their ids through the locked writer.

Delivery cards keep the ``Initiative cycle:`` marker. Assessment cards use ``Assessment cycle:`` instead, so the
ordinary linking pass can never treat an assessment as delivery proof, and this module's cards are invisible to
the delivery lifecycle predicates.

Input epoch
-----------
The epoch fingerprints what would change the answer: the workflow contract (owner, surface, collector, deadlines),
the measurement definition (metric, source, baseline, window, criteria, guardrails), the user's own decisions —
stage, decision rows, result and disposition — and the delivery cards' identities, statuses and completion dates.

It deliberately excludes everything an assessment worker writes or records while doing its job: release
observations, metric and guardrail rows, notes, receipts, assessment cards, and the current date. Release
verification is the first thing a worker does, and the derived evaluation date follows it; including either would
move the epoch out from under the worker's own receipt and force a second card for unchanged inputs. The maturity
trigger is already carried by the window definition in ``measurement``.

A recorded verdict with a terminal disposition closes the loop for that cycle: :func:`derive` returns
``disabled``, so a finished decision can never spawn another assessment. A verdict with ``await_evidence`` stays
open — the user asked for more data, and a new window moves the epoch when it matures. ``done`` and ``archived``
delivery cards share one completion token, so archiving a finished card does not invalidate a receipt either.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .lifecycle import (
    DONE_CARD_STATUSES,
    as_date,
    as_list,
    cards_done,
    evaluation_date,
    observation_date,
    release_state,
)
from .model import ASSESSMENT_STATUSES, CLOSED_STAGES, CYCLE_ID, OPEN_STAGES

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"
RELEASE_SURFACES = ("production", "main_ci", "runtime", "manual")
MAX_ACTIVE = 3
EPOCH_LENGTH = 12
# Dispositions that end the cycle's work. Once one is recorded the loop stops asking for outcome evidence.
TERMINAL_DISPOSITIONS = ("continue", "iterate", "stop")

# The card title carries the epoch so a card list read back from the board can be attributed without its body;
# the body line is the primary source, because the title is only a convenience.
CARD_EPOCH = re.compile(r"\[epoch ([0-9a-f]{6,64})\]")
BODY_EPOCH = re.compile(r"^Epoch:[ \t]*([0-9a-f]{6,64})[ \t]*$", re.MULTILINE)
# The one body line that ties a card to a cycle as an *assessment*: "Assessment cycle: <cycle-id>". Never
# "Initiative cycle:", which kanban.cycle_markers reads as delivery.
ASSESSMENT_MARKER = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?Assessment cycle:[ \t]*`?(in-[a-z0-9]+(?:-[a-z0-9]+)*-c\d{2,3})`?(?![\w-])",
    re.IGNORECASE | re.MULTILINE,
)

SURFACE_HELP = {
    "production": (
        "confirm the delivery commit is live in production (deployment version, image or URL, and the time it "
        "went live), then read the metric from production data; a green CI run alone is not production proof"
    ),
    "main_ci": (
        "confirm the delivery commit's CI run on main passed and read the metric from that run or its artifacts; "
        "never describe a CI result as production"
    ),
    "runtime": (
        "check the running service's reported version, health and logs for the delivery commit, then read the "
        "metric from runtime telemetry"
    ),
    "manual": (
        "ask the user to perform or confirm the check, record their answer verbatim with its date, and do not "
        "substitute your own estimate"
    ),
}


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def _show(value: Any) -> str:
    if value in (None, ""):
        return "not set"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def contract_gaps(frontmatter: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """What the contract or the measurement lacks for a judgement. Reported, never repaired here."""
    gaps: list[str] = []
    if not str(contract.get("owner") or "").strip():
        gaps.append("outcome_review.owner missing")
    surface = str(contract.get("release_surface") or "").strip()
    if surface not in RELEASE_SURFACES:
        gaps.append(f"outcome_review.release_surface invalid: {surface or 'missing'}")
    if not str(contract.get("collector") or "").strip():
        gaps.append("outcome_review.collector missing")
    for key in ("review_by", "target_date"):
        raw = contract.get(key)
        if raw not in (None, "") and as_date(raw) is None:
            gaps.append(f"outcome_review.{key} is not an ISO date: {raw}")
    if not _mapping(frontmatter.get("measurement")).get("success_criteria"):
        gaps.append("measurement.success_criteria missing")
    if not as_date(contract.get("review_by")) and not as_date(contract.get("target_date")):
        gaps.append("outcome_review.review_by and target_date both missing")
    return gaps


def input_epoch(frontmatter: dict[str, Any], delivery_cards: list[dict[str, Any]]) -> str:
    """The stable fingerprint of the inputs an assessment answers. See the module docstring for exclusions."""
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    contract = _mapping(fm.get("outcome_review"))
    delivery = []
    for card in delivery_cards or []:
        if not isinstance(card, dict):
            continue
        status = str(card.get("status") or "unknown")
        delivery.append(
            [
                str(card.get("id") or ""),
                "done" if status in DONE_CARD_STATUSES else status,
                _iso(as_date(card.get("completed_at"))) or "",
            ]
        )
    material = {
        "contract": {
            "enabled": bool(contract.get("enabled")),
            "owner": str(contract.get("owner") or "").strip(),
            "release_surface": str(contract.get("release_surface") or "").strip(),
            "collector": str(contract.get("collector") or "").strip(),
            "review_by": _iso(as_date(contract.get("review_by"))) or _show(contract.get("review_by")),
            "target_date": _iso(as_date(contract.get("target_date"))) or _show(contract.get("target_date")),
        },
        "measurement": _mapping(fm.get("measurement")),
        "stage": str(fm.get("stage") or ""),
        "decisions": as_list(fm.get("decisions")),
        "result": _mapping(fm.get("result")),
        "disposition": _mapping(fm.get("disposition")),
        "delivery": sorted(delivery),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:EPOCH_LENGTH]


def card_epoch(card: dict[str, Any]) -> str | None:
    """The epoch a card was created for: its ``epoch`` key, then the body's ``Epoch:`` line, then the title.

    The body is authoritative. A card list read without bodies (the dashboard's card rows) still attributes
    through the title dispatch writes.
    """
    explicit = str(card.get("epoch") or "").strip()
    if explicit:
        return explicit
    body = BODY_EPOCH.search(str(card.get("body") or ""))
    if body:
        return body.group(1)
    match = CARD_EPOCH.search(str(card.get("title") or ""))
    return match.group(1) if match else None


def assessment_markers(board: str | None, path: Path | None = None) -> dict[str, Any]:
    """Read-only ``{"state", "error", "markers": {cycle_id: [{"id", "body"}]}}`` from assessment card bodies.

    The delivery marker reader (``kanban.cycle_markers``) deliberately does not match these cards, and this reader
    never matches delivery: an unlinked assessment card is recoverable, and it is never counted as delivery.
    """
    from .kanban import board_path

    if not board and path is None:
        return {"state": "unavailable", "error": "no Kanban board configured", "markers": {}}
    try:
        db = path or board_path(str(board))
        if not db.is_file():
            raise FileNotFoundError(f"Kanban board database not found: {db}")
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except (ImportError, OSError, sqlite3.Error) as exc:
        return {"state": "unavailable", "error": str(exc), "markers": {}}
    try:
        rows = conn.execute(
            "SELECT id, body FROM tasks WHERE status != 'archived' AND body LIKE '%ssessment cycle:%' ORDER BY created_at, rowid"
        ).fetchall()
    except sqlite3.Error as exc:
        return {"state": "unavailable", "error": f"Kanban schema not readable: {exc}", "markers": {}}
    finally:
        conn.close()
    markers: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        body = row["body"] or ""
        for cycle_id in dict.fromkeys(match.group(1).lower() for match in ASSESSMENT_MARKER.finditer(body)):
            markers.setdefault(cycle_id, []).append({"id": row["id"], "body": body})
    return {"state": "fresh", "error": None, "markers": markers}


def latest_receipt(observations: list[dict[str, Any]], epoch: str, finished: dt.date | None) -> dict[str, Any] | None:
    """The newest receipt for *epoch* that can be judged: evidence, proposal, and dated at or after delivery.

    A note, a metric row, or a receipt with a missing artifact or a stale epoch is not a receipt. A receipt dated
    before the last delivery card completed describes a different delivery and is ignored.
    """
    candidates: list[tuple[dt.date, str, dict[str, Any]]] = []
    for row in observations or []:
        if not isinstance(row, dict) or row.get("kind") != "assessment":
            continue
        if str(row.get("epoch") or "") != epoch:
            continue
        if str(row.get("status") or "") not in ASSESSMENT_STATUSES:
            continue
        if not str(row.get("evidence") or "").strip() or not str(row.get("proposal") or "").strip():
            continue
        when = observation_date(row)
        if when is None:
            continue
        if finished is not None and when < finished:
            continue
        candidates.append((when, str(row.get("at") or ""), row))
    if not candidates:
        return None
    when, _at, row = max(candidates, key=lambda item: (item[0], item[1]))
    return {
        "epoch": epoch,
        "status": row["status"],
        "evidence": row["evidence"],
        "proposal": row["proposal"],
        "detail": str(row.get("detail") or ""),
        "date": when.isoformat(),
        "at": row.get("at"),
        "artifact_sha256": row.get("artifact_sha256"),
    }


def diagnostic_gaps(measurement: dict[str, Any], review_by: dt.date | None, target: dt.date | None) -> list[str]:
    """Contract pieces whose absence would otherwise leave the cycle with no assessment, ever."""
    gaps = []
    if not measurement.get("success_criteria"):
        gaps.append("success criteria")
    if not measurement.get("observation_window"):
        gaps.append("observation window")
    if not review_by and not target:
        gaps.append("review deadline or target date")
    return gaps


def derive(
    frontmatter: dict[str, Any],
    observations: list[dict[str, Any]],
    delivery_cards: list[dict[str, Any]],
    assessment_cards: list[dict[str, Any]],
    today: dt.date | str | None,
) -> dict[str, Any]:
    """The closed-loop state of one cycle. Pure: it writes nothing and reads no clock when *today* is given.

    States, in the order they are decided:

    ``disabled``          no enabled contract, a closed stage, or a verdict with a terminal disposition.
    ``source_unavailable`` a listed card cannot be read (or was not read). Nothing is judged from a gap.
    ``delivery_open``     delivery is unfinished (or the cycle is exploring) and the contract can still judge it.
    ``waiting``           delivery is done but the observation window or approved target has not matured.
    ``assessment_needed`` the outcome, or the contract itself, is due; dispatch creates one card per epoch.
    ``assessment_running`` an unfinished card retains ownership, even across input-epoch changes.
    ``assessment_failed`` a current-epoch card finished without a receipt; surfaced, never replaced.
    ``review_ready``      a current-epoch receipt is waiting for the user, or a verdict awaits its disposition.
    """
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    rows = [row for row in observations or [] if isinstance(row, dict)]
    delivery = [card for card in delivery_cards or [] if isinstance(card, dict)]
    assessments = [card for card in assessment_cards or [] if isinstance(card, dict)]
    day = as_date(today) or dt.date.today()

    contract = _mapping(fm.get("outcome_review"))
    enabled = bool(contract.get("enabled"))
    result: dict[str, Any] = {
        "enabled": enabled,
        "epoch": None,
        "state": "disabled",
        "due_date": None,
        "reasons": [],
        "assessment": None,
        "assessment_card": None,
        "contract_gaps": [],
        "next_action": "No outcome review configured for this cycle",
    }
    if not enabled:
        return result

    gaps = contract_gaps(fm, contract)
    reasons = [f"contract_gap: {gap}" for gap in gaps]
    epoch = input_epoch(fm, delivery)
    result.update(epoch=epoch, contract_gaps=gaps, reasons=reasons)

    stage = str(fm.get("stage") or "exploring")
    recorded = _mapping(fm.get("result"))
    disposition = _mapping(fm.get("disposition"))
    verdict = str(recorded.get("verdict") or "unset")
    action = str(disposition.get("action") or "unset")

    if stage in CLOSED_STAGES:
        result.update(
            state="disabled",
            reasons=[*reasons, f"cycle_closed: stage {stage}"],
            next_action=f"Outcome review closed: the cycle is {stage}",
        )
        return result
    if verdict != "unset" and action in TERMINAL_DISPOSITIONS:
        # The user concluded. Nothing is owed to the loop, and an earlier receipt must not reopen it.
        result.update(
            state="disabled",
            reasons=[*reasons, f"cycle_concluded: verdict {verdict}, disposition {action}"],
            next_action=f"Outcome review closed: verdict {verdict}, disposition {action}",
        )
        return result

    listed_delivery = [str(card) for card in as_list(fm.get("kanban_cards"))]
    listed_assessments = [str(card) for card in as_list(fm.get("assessment_cards"))]
    unreadable = [str(card.get("id")) for card in (*delivery, *assessments) if card.get("state") != "fresh"]
    if listed_delivery and not delivery:
        unreadable.append("delivery cards listed but not read")
    if listed_assessments and not assessments:
        unreadable.append("assessment cards listed but not read")
    if unreadable:
        result.update(
            state="source_unavailable",
            reasons=[*reasons, f"source_unavailable: {', '.join(unreadable)}"],
            next_action="Restore Kanban reads for this cycle's cards: no outcome can be judged from missing evidence",
        )
        return result

    release = release_state(rows)
    window_end = evaluation_date(fm, release)
    review_by = as_date(contract.get("review_by"))
    target = as_date(contract.get("target_date"))
    gates = [date for date in (window_end, target, review_by) if date]
    trigger = min(gates) if gates else None
    review_due = bool(review_by and day >= review_by)
    target_due = bool(target and day >= target)
    window_due = bool(window_end and day >= window_end)
    measurement = _mapping(fm.get("measurement"))
    diagnostic = diagnostic_gaps(measurement, review_by, target)
    open_cards = [card for card in delivery if str(card.get("status")) not in DONE_CARD_STATUSES]
    all_done, finished = cards_done(delivery)
    due: dt.date | None = trigger
    state = "delivery_open"

    # An interim deadline assessment cannot discharge the later, mature measurement. Keep the base epoch
    # stable until that boundary, then use a new phase exactly once (never a new epoch on every day).
    earlier = [date for date in (review_by, target) if date and window_end and date < window_end]
    if window_due and earlier:
        epoch = hashlib.sha256(f"{epoch}:mature:{window_end.isoformat()}".encode()).hexdigest()[:EPOCH_LENGTH]
        result["epoch"] = epoch

    if verdict != "unset":
        reasons.append(f"verdict_recorded: {verdict}")
        if action != "await_evidence":
            waiting = bool(window_end and day < window_end and not (review_due or target_due))
            result.update(
                state="waiting" if waiting else "review_ready",
                due_date=_iso(trigger),
                reasons=reasons,
                next_action=(
                    f"Wait for the agreed measurement window: {window_end.isoformat()}"
                    if waiting
                    else "Record the disposition: conclude, iterate, or wait for more evidence"
                ),
            )
            return result
        reasons.append("await_evidence")

    if stage == "exploring":
        reasons.append("exploring")
        if review_due:
            reasons.append(f"review_by_passed: {review_by.isoformat()}")
        if not delivery:
            next_action = "Plan the delivery cards for this cycle"
        elif all_done:
            next_action = "Decide whether to run this cycle"
        else:
            next_action = f"Finish planning: {len(open_cards)} of {len(delivery)} cards open"
    elif review_due or target_due or window_due:
        state = "assessment_needed"
        if review_due:
            reasons.append(f"review_by_passed: {review_by.isoformat()}")
        if target_due:
            reasons.append(f"target_date: {target.isoformat()}")
        if window_due:
            reasons.append(f"window_ended: {window_end.isoformat()}")
        if open_cards:
            reasons.append(f"delivery_open: {len(open_cards)} of {len(delivery)} cards open")
        if not delivery:
            reasons.append("no_delivery_record")
        next_action = "Assess the outcome and delivery so far: collect evidence and prepare a bounded proposal"
    elif window_end and day < window_end:
        # Runtime and product measurement can legitimately have no delivery cards. Missing review dates
        # do not turn a valid future window into a diagnostic ask before that window ends.
        state = "waiting"
        reasons.append(f"waiting_for: {trigger.isoformat()}")
        next_action = f"Wait for the outcome window or agreed deadline: assess on {trigger.isoformat()}"
    elif open_cards:
        reasons.append(f"delivery_open: {len(open_cards)} of {len(delivery)} cards open")
        if diagnostic:
            state = "assessment_needed"
            reasons.append(f"diagnostic_contract_assessment: missing {', '.join(diagnostic)}")
            next_action = "Assess the missing contract and delivery status; propose criteria and dates without adopting them"
        else:
            next_action = f"Finish delivery: {len(open_cards)} of {len(delivery)} cards open"
    elif delivery or stage == "measuring" or diagnostic:
        state = "assessment_needed"
        if all_done:
            reasons.append("delivery_done")
        if not delivery and diagnostic:
            reasons.append(f"diagnostic_contract_assessment: missing {', '.join(diagnostic)}")
        reasons.append("spot_check" if measurement else "no_measurement")
        next_action = (
            "Assess the contract and propose its missing fields"
            if not delivery and diagnostic
            else "Assess the outcome: verify the release surface, collect evidence and prepare a bounded proposal"
        )
    else:
        reasons.append("no_delivery_cards")
        next_action = "Locate the existing delivery or runtime evidence before assessing this cycle"

    # Only current receipts speak; unfinished cards retain ownership across an epoch change.
    current = [card for card in assessments if card_epoch(card) == epoch]
    for card in assessments:
        if card_epoch(card) is None:
            reasons.append(f"assessment_card_without_epoch: {card.get('id')}")
    # Reuse unfinished work across input changes. The owner must re-inspect and recollect, not spawn
    # a second unit while the old unit still consumes capacity or has a live worker.
    active = [card for card in assessments if str(card.get("status")) not in DONE_CARD_STATUSES]
    closed = [card for card in current if str(card.get("status")) in DONE_CARD_STATUSES]
    card_id = str(active[0].get("id")) if active else (str(closed[-1].get("id")) if closed else None)
    receipt = latest_receipt(rows, epoch, finished)

    if receipt and state == "assessment_needed":
        state = "review_ready"
        reasons.append(f"assessment_receipt: {receipt['status']}")
        next_action = "Review the evidence and the proposal, then record the verdict and disposition"
    elif receipt and state == "waiting":
        # A receipt recorded before the window matures is visible, not a reason to review early.
        reasons.append(f"assessment_receipt_early: {receipt['status']}")
    elif state == "assessment_needed":
        if active:
            state = "assessment_running"
            reasons.append(f"assessment_running: {card_id}")
            status = str(active[0].get("status") or "unknown")
            assignee = str(active[0].get("assignee") or "").strip()
            owner = f"owner {assignee}" if assignee else f"unassigned; configured owner {contract.get('owner') or 'not recorded'}"
            next_action = f"Assessment {card_id} is {status}; {owner}: follow this card through to its receipt"
        elif closed:
            state = "assessment_failed"
            reasons.append(f"assessment_done_without_receipt: {card_id}")
            next_action = f"{card_id} finished without a receipt: record one or investigate; no replacement card is created"

    result.update(
        state=state,
        due_date=_iso(due),
        reasons=reasons,
        assessment=receipt if state == "review_ready" else None,
        assessment_card=card_id,
        next_action=next_action,
    )
    return result


def card_title(cycle_id: str, epoch: str, mode: str = "outcome") -> str:
    lead = "Assess the contract for" if mode == "contract" else "Assess outcomes of"
    return f"{lead} {cycle_id} [epoch {epoch}]"[:200]


def card_body(
    frontmatter: dict[str, Any],
    contract: dict[str, Any],
    epoch: str,
    cycle_id: str,
    root: Path,
    mode: str = "outcome",
) -> str:
    """The assessment worker's brief. No ``Initiative cycle:`` line: this card is not delivery."""
    measurement = _mapping(frontmatter.get("measurement"))
    surface = str(contract.get("release_surface") or "manual")
    collector = str(contract.get("collector") or "").strip()
    collector_gap = ""
    if not collector:
        collector_gap = " (not recorded: reconstruct it from measurement.source and the cycle's sources)"
    if mode == "contract":
        opening = (
            f"Diagnose the outcome contract for cycle {cycle_id}. The contract cannot judge this cycle yet, so "
            "report its current state and propose the missing parts. This is a diagnostic assessment, not a "
            "judgement and not a build."
        )
    else:
        opening = (
            f"Assess whether cycle {cycle_id} delivered the outcome it promised. This is an assessment, not a "
            "build: do not implement the follow-up you propose."
        )
    report_dir = root / cycle_id.rsplit("-c", 1)[0] / "reports" / cycle_id
    delivery = [str(card) for card in as_list(frontmatter.get("kanban_cards"))]
    delivery_note = (
        "No delivery card is linked to this cycle."
        if not delivery
        else f"Linked delivery cards: {len(delivery)}. Read their live status; linkage alone is not outcome proof."
    )
    lines = [
        opening,
        "",
        f"Assessment cycle: {cycle_id}",
        f"Epoch: {epoch}",
        f"Mode: {mode}",
        delivery_note,
        f"Measurement owner: {_show(contract.get('owner'))}",
        f"Release surface to verify: {surface}",
        f"Target date: {_show(contract.get('target_date'))}",
        f"Review by: {_show(contract.get('review_by'))}",
        "",
        f"Primary metric: {_show(measurement.get('primary_metric'))}",
        f"Source: {_show(measurement.get('source'))}",
        f"Baseline: {_show(measurement.get('baseline'))}",
        f"Observation window: {_show(measurement.get('observation_window'))}",
        f"Success criteria: {_show(measurement.get('success_criteria'))}",
        f"Guardrails: {_show(measurement.get('guardrails'))}",
        f"Insufficient data means: {_show(measurement.get('insufficient_data'))}",
        "",
        f"Collector instructions{collector_gap}:",
        "",
    ]
    lines.extend(f"    {line}" if line.strip() else "" for line in (collector or "(none recorded)").splitlines())
    lines += [
        "",
        "Do this, in order:",
        f"1. Before collection, run python3 {SCRIPTS / 'record_assessment.py'} --cycle {cycle_id} --inspect; "
        "retain that epoch and read the current contract. Never relabel evidence collected for older inputs.",
        f"2. Verify the release surface '{surface}': {SURFACE_HELP.get(surface, SURFACE_HELP['manual'])}.",
        "3. Run the collector instructions against real data. Record what you ran, the date range, the sample size, "
        "and the raw numbers.",
        f"4. Create {report_dir}/ if missing. Write the report to a new file at an absolute path there, for example ",
        f"{report_dir}/{epoch}-assessment.md. It must carry the numbers, the sample, the query or ",
        "command, and the date range.",
        "5. Write a bounded follow-up proposal to a second absolute new file: the smallest change that would close "
        "the gap, with benefit, cost, risk, timeline, and the criteria it leaves unmet. One page.",
        "6. Re-inspect before recording; if the epoch changed",
        "   during collection, discard the stale recommendation and recollect. Never relabel interim evidence:",
        "",
        f"    python3 {SCRIPTS / 'record_assessment.py'} --cycle {cycle_id} --inspect",
        f"    python3 {SCRIPTS / 'record_assessment.py'} --cycle {cycle_id} --epoch <epoch printed by --inspect> \\",
        "        --status met|missed|insufficient_data|measurement_failed \\",
        "        --evidence /absolute/path/report.md --proposal /absolute/path/proposal.md \\",
        '        --detail "<one line: what you measured, over what dates>"',
        "",
        "7. At completion, hand the report/proposal receipt to the existing cycle Review gate. If the same "
        "contract decision is already on a planning card, give its owner both artifacts and keep one decision "
        "package; do not create a duplicate approval or execution graph.",
        "",
        "Rules:",
        "- Run --inspect after you have written every observation. Verifying the release or recording metrics does "
        "not move the epoch, but changed inputs do, and a stale epoch is refused.",
        "- Begin both artifacts with 'Assessment epoch: <epoch inspected BEFORE collection>'. The receipt",
        "  command verifies that binding, so stale artifacts cannot be relabelled for a mature window.",
        "- The receipt needs both files as existing, non-empty absolute paths; the writer hashes them.",
        "- Write only new files. Never edit an existing report, the cycle file, another initiative's notes, or "
        "anything outside the report paths above.",
        "- A finding is not a user verdict. Do not edit the cycle frontmatter, its stage, measurement, result, "
        "disposition, decisions or delivery cards. Do not create a successor cycle, a follow-up card, or approve "
        "your own proposal.",
        "- If the evidence cannot be collected, record measurement_failed with what you tried and what is missing. "
        "Follow the approved insufficient-data rule, not an invented minimum. A complete, comparable mature "
        "window below its success target is missed (shortfall), even for low usage, unless an explicitly "
        "approved minimum-sample rule makes it unevaluable. An incomplete window or missing observations is "
        "insufficient_data; collector failure is measurement_failed. Never invent numbers.",
        "- For an approved one-off spot check, evaluate that check against real evidence without inventing a "
        "metric, baseline, guardrail, deadline or time window. Missing optional template fields alone do not "
        "require insufficient_data. This exception applies only to optional template-field completeness in "
        "older generated collectors, never to approved criteria or evidence requirements. Quote the exact "
        "approved criterion and its source before judging it. Missing actual criteria or outcome proof still "
        "requires a diagnostic.",
    ]
    if mode == "contract":
        lines += [
            "- The outcome cannot be judged yet: record insufficient_data, and make the proposal a concrete "
            "contract proposal (success criteria, observation window, target date, review deadline) with benefit, "
            "cost, risk and timeline. Do not set those fields yourself.",
        ]
    lines.append(
        "- If collection or a prerequisite fails, publish the limitation and bounded remedy on this same card "
        "with its receipt; block it with the exact missing prerequisite and next owner. Do not silently "
        "restart the window or create a replacement chain."
    )
    return "\n".join(lines) + "\n"


def link_assessment_card(root: Path, cycle_id: str, card_id: str) -> list[str]:
    """Append *card_id* to the cycle's ``assessment_cards`` under the root write lock, then verify the read-back.

    Append-only: an existing id is left in place and nothing else in the frontmatter may move.
    """
    import copy

    from . import writers

    match = CYCLE_ID.fullmatch(cycle_id)
    if not match:
        raise writers.WriteRefused(f"'{cycle_id}' is not a cycle id")
    path = root / match.group(1) / "cycles" / f"{cycle_id}.md"
    if not path.is_file():
        raise writers.WriteRefused(f"cycle file not found: {path}")
    before: dict[str, Any] = {}
    listed_before: list[str] = []

    def change(fm: dict[str, Any]) -> None:
        before.update(copy.deepcopy(fm))
        listed_before.extend(str(card) for card in as_list(fm.get("assessment_cards")))
        current = [str(card) for card in as_list(fm.get("assessment_cards"))]
        if card_id not in current:
            fm["assessment_cards"] = [*current, card_id]

    after = writers.edit_frontmatter(path, root, change)
    moved = {
        key: [before.get(key), after.get(key)]
        for key in {*before, *after}
        if key != "assessment_cards" and before.get(key) != after.get(key)
    }
    if moved:
        raise writers.WriteRefused(f"{cycle_id}: linking {card_id} changed other fields: {moved}")
    listed = [str(card) for card in as_list(after.get("assessment_cards"))]
    expected = listed_before if card_id in listed_before else [*listed_before, card_id]
    if listed != expected:
        raise writers.WriteRefused(f"{cycle_id}: read-back shows assessment_cards {listed}, expected {expected}")
    return listed


def dispatch(
    config: Any,
    run: Any = None,
    today: dt.date | str | None = None,
    max_active: int = MAX_ACTIVE,
) -> dict[str, Any]:
    """Serialize cap accounting and create/link recovery across overlapping review ticks."""
    from contextlib import ExitStack

    from .writers import file_lock

    roots = sorted({Path(project.root).resolve() for project in getattr(config, "projects", []) or []})
    with ExitStack() as stack:
        for root in roots:
            stack.enter_context(file_lock(root / ".outcome-dispatch.lock"))
        return _dispatch(config, run=run, today=today, max_active=max_active)


def _dispatch(
    config: Any,
    run: Any = None,
    today: dt.date | str | None = None,
    max_active: int = MAX_ACTIVE,
) -> dict[str, Any]:
    """Create the missing assessment card for each cycle whose assessment is due, at most *max_active* at a time.

    Reads each board once: markers and card rows for both delivery and assessment ids. ``pending`` names the
    cycles that owe an assessment but got no card, and why (``cap``, ``owner_missing``,
    ``release_surface_invalid``, ``source_unavailable``, ``create_failed``). ``errors`` carries the human line.
    """
    from . import kanban, writers
    from .review_exec import ActionFailed, Kanban, default_runner
    from .store import active_observations, load

    day = as_date(today) or dt.date.today()
    api = Kanban(run or default_runner)
    created: list[dict[str, Any]] = []
    errors: list[str] = []
    pending: list[dict[str, Any]] = []

    for project in getattr(config, "projects", []) or []:
        project_id = str(getattr(project, "id", "") or "")
        board = getattr(project, "kanban_board", None)
        root = Path(project.root)
        snapshot = load(root)
        cycles: list[tuple[str, dict[str, Any]]] = []
        for cycle_id, doc in sorted(snapshot.cycles.items()):
            fm = doc.frontmatter
            contract = fm.get("outcome_review")
            if not (isinstance(contract, dict) and contract.get("enabled")):
                continue
            if str(fm.get("stage") or "exploring") not in OPEN_STAGES:
                continue
            initiative = snapshot.initiatives.get(snapshot.cycle_initiative.get(cycle_id, ""))
            if initiative is None or str(initiative.frontmatter.get("status") or "active") != "active":
                continue
            cycles.append((cycle_id, fm))
        if not cycles:
            continue

        def blocked(project: str, cycle_rows: list[tuple[str, dict[str, Any]]], reason: str, detail: str) -> None:
            errors.append(f"{project}: {detail}")
            pending.extend(
                {"project": project, "cycle": cycle_id, "epoch": None, "state": "source_unavailable", "reason": reason}
                for cycle_id, _fm in cycle_rows
            )

        if not board:
            blocked(project_id, cycles, "board_unavailable", "no Kanban board configured; assessment dispatch skipped")
            continue
        markers = kanban.cycle_markers(board)
        if markers["state"] != "fresh":
            blocked(project_id, cycles, "source_unavailable", f"Kanban unavailable: {markers['error']}")
            continue
        marked = assessment_markers(board)
        if marked["state"] != "fresh":
            blocked(project_id, cycles, "source_unavailable", f"Kanban unavailable: {marked['error']}")
            continue

        delivery_ids: dict[str, list[str]] = {}
        assessment_ids: dict[str, list[str]] = {}
        linked_ids: dict[str, set[str]] = {}
        bodies: dict[str, str] = {}
        for cycle_id, fm in cycles:
            listed = [str(card) for card in as_list(fm.get("kanban_cards"))]
            skip = set(listed) | {str(card) for card in as_list(fm.get("excluded_cards"))}
            derived = [card for card in markers["markers"].get(cycle_id, []) if card not in skip]
            delivery_ids[cycle_id] = list(dict.fromkeys([*listed, *derived]))
            linked = [str(card) for card in as_list(fm.get("assessment_cards"))]
            marked_cards = marked["markers"].get(cycle_id, [])
            for item in marked_cards:
                bodies[item["id"]] = item["body"]
            linked_ids[cycle_id] = set(linked)
            assessment_ids[cycle_id] = list(dict.fromkeys([*linked, *(item["id"] for item in marked_cards)]))
        wanted = list(dict.fromkeys([card for ids in (*delivery_ids.values(), *assessment_ids.values()) for card in ids]))
        found = kanban.read_cards(board, wanted)
        if found["state"] != "fresh":
            blocked(project_id, cycles, "source_unavailable", f"Kanban unavailable: {found['error']}")
            continue
        rows = {card: {**found["cards"][card], **({"body": bodies[card]} if card in bodies else {})} for card in wanted}

        states: dict[str, dict[str, Any]] = {}
        fronts: dict[str, dict[str, Any]] = {}
        for cycle_id, fm in cycles:
            states[cycle_id] = derive(
                fm,
                active_observations(snapshot.observations.get(cycle_id)),
                [rows[card] for card in delivery_ids[cycle_id]],
                [rows[card] for card in assessment_ids[cycle_id]],
                day,
            )
            fronts[cycle_id] = fm
        active_cards = sorted(
            card
            for card in {card for ids in assessment_ids.values() for card in ids}
            if rows[card].get("state") == "fresh" and str(rows[card].get("status")) not in DONE_CARD_STATUSES
        )
        active_units = len(active_cards)
        for cycle_id, state in states.items():
            if state["state"] == "source_unavailable":
                pending.append(
                    {
                        "project": project_id,
                        "cycle": cycle_id,
                        "epoch": state["epoch"],
                        "state": state["state"],
                        "reason": "source_unavailable",
                    }
                )

        # Recovery: a card created before a link crashed carries the marker, so it is adopted by its body epoch
        # instead of being created again (the idempotency key would return the same card anyway).
        for cycle_id, _fm in cycles:
            state = states[cycle_id]
            if state["state"] == "disabled" or not state["epoch"]:
                continue
            for card in assessment_ids[cycle_id]:
                if card in linked_ids[cycle_id] or card_epoch(rows[card]) != state["epoch"]:
                    continue
                if rows[card].get("state") != "fresh":
                    continue
                linked = True
                try:
                    link_assessment_card(root, cycle_id, card)
                except (writers.WriteRefused, OSError) as exc:
                    linked = False
                    errors.append(f"{project_id}/{cycle_id}: assessment card {card} not linked: {exc}")
                created.append(
                    {
                        "project": project_id,
                        "cycle": cycle_id,
                        "epoch": state["epoch"],
                        "state": state["state"],
                        "card": card,
                        "linked": linked,
                        "reused": True,
                    }
                )

        candidates = sorted(
            ((cycle_id, state) for cycle_id, state in states.items() if state["state"] == "assessment_needed"),
            key=lambda item: (item[1].get("due_date") or "", item[0]),
        )
        for cycle_id, state in candidates:
            contract = fronts[cycle_id]["outcome_review"]
            diagnostic = any(reason.startswith("diagnostic_contract_assessment") for reason in state["reasons"])
            mode = "contract" if diagnostic else "outcome"
            entry = {
                "project": project_id,
                "cycle": cycle_id,
                "epoch": state["epoch"],
                "state": state["state"],
                "reused": False,
            }
            owner = str(contract.get("owner") or "").strip()
            surface = str(contract.get("release_surface") or "").strip()
            if not state["epoch"]:
                errors.append(f"{project_id}/{cycle_id}: no input epoch; no assessment card created")
                pending.append({**entry, "reason": "epoch_missing"})
                continue
            if not owner:
                # No plan_assignee fallback: guessing the owner would create work for the wrong profile.
                errors.append(f"{project_id}/{cycle_id}: outcome_review.owner missing; no assessment card created")
                pending.append({**entry, "reason": "owner_missing"})
                continue
            if surface not in RELEASE_SURFACES:
                errors.append(
                    f"{project_id}/{cycle_id}: outcome_review.release_surface '{surface or '-'}' is not one of "
                    f"{', '.join(RELEASE_SURFACES)}; no assessment card created"
                )
                pending.append({**entry, "reason": "release_surface_invalid"})
                continue
            if active_units >= max_active:
                pending.append({**entry, "reason": "cap", "active_cards": list(active_cards)})
                continue
            try:
                card = api.create(
                    board,
                    title=card_title(cycle_id, state["epoch"], mode),
                    assignee=owner,
                    body=card_body(fronts[cycle_id], contract, state["epoch"], cycle_id, root, mode),
                    key=f"outcome-assessment:{project_id}:{cycle_id}:{state['epoch']}",
                    skills=tuple(getattr(project, "plan_skills", []) or ()),
                )
            except ActionFailed as exc:
                errors.append(f"{project_id}/{cycle_id}: {exc}")
                pending.append({**entry, "reason": "create_failed"})
                continue
            active_units += 1
            active_cards.append(card)
            linked = True
            try:
                link_assessment_card(root, cycle_id, card)
            except (writers.WriteRefused, OSError) as exc:
                # The key makes the next tick reuse this card, so the link is retried, never duplicated.
                linked = False
                errors.append(f"{project_id}/{cycle_id}: assessment card {card} created but not linked: {exc}")
            created.append({**entry, "card": card, "linked": linked})

    return {"created": created, "errors": errors, "pending": pending}
