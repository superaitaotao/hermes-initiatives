"""Validator and lifecycle rules: each test names the regression it would catch."""

from __future__ import annotations

import datetime as dt
import json

from conftest import DECIDED, MEASUREMENT, cycle_md, write

from hermes_initiatives_core import lifecycle, store
from hermes_initiatives_core.validate import validate

TODAY = dt.date(2026, 11, 1)


def _cycle(root, cycle_id="in-demo-c01", **fields):
    return write(root / "in-demo" / "cycles" / f"{cycle_id}.md", cycle_md(cycle_id, **fields))


def _obs(root, cycle_id, *rows):
    path = root / "in-demo" / "observations" / f"{cycle_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _errors(root):
    return validate(store.load(root), check_sources=False).errors


def test_valid_pilot_shape_passes(root):
    _cycle(root, stage="decided", decisions=DECIDED, kanban_cards=["t_0123abcd"], change_register=["C01"])
    report = validate(store.load(root), check_sources=False)
    assert report.ok, dict(report.errors)


def test_null_verdict_is_rejected_not_read_as_no_effect(root):
    # Regression: YAML null silently meaning "no effect" hides an unevaluated cycle.
    _cycle(root, result={"verdict": None, "evidence": []})
    assert any("verdict is null" in m for m in _errors(root)["in-demo-c01"])


def test_concluded_needs_verdict_disposition_and_evidence(root):
    _cycle(root, stage="concluded", decisions=DECIDED)
    messages = " ".join(_errors(root)["in-demo-c01"])
    assert "needs a verdict" in messages and "disposition action" in messages and "evidence link" in messages


def test_agent_decision_is_rejected_and_backfill_needs_evidence(root):
    _cycle(
        root,
        stage="decided",
        decisions=[
            {"date": "2026-09-01", "by": "coder", "decision": "x"},
            {"date": "2026-09-02", "by": "backfill", "decision": "y"},
            {"date": "2026-09-03", "by": "agent", "rule": "idea_reject", "review": "D1", "decision": "z"},
        ],
    )
    messages = " ".join(_errors(root)["in-demo-c01"])
    assert "must be 'user', 'backfill', or 'agent' with a delegated rule" in messages
    assert "backfilled decision needs evidence" in messages
    assert "decisions[2]: an agent decision needs a delegated rule" in messages


def test_delegated_agent_decision_is_valid_but_never_the_user_decision_a_stage_needs(root):
    agent = {"date": "2026-10-13", "by": "agent", "rule": "repair_promotion", "review": "D44", "decision": "Promoted"}
    _cycle(root, stage="decided", decisions=[agent])
    messages = " ".join(_errors(root).get("in-demo-c01", []))
    assert "agent decision" not in messages
    assert messages, "a decided cycle with only an agent row still lacks the user decision"
    _cycle(root, stage="decided", decisions=[*DECIDED, agent])
    assert not _errors(root).get("in-demo-c01")


def test_measuring_without_verified_release_is_an_error(root):
    _cycle(root, stage="measuring", decisions=DECIDED, measurement=MEASUREMENT)
    assert any("release_verified" in m for m in _errors(root)["in-demo-c01"])


def test_cross_file_references_and_shared_change_rows(root):
    _cycle(
        root,
        "in-demo-c01",
        change_register=["C05"],
        disposition={"action": "iterate", "follow_ups": ["in-demo-c09", "idea-20261001-nope"]},
    )
    _cycle(root, "in-demo-c02", change_register=["C05"])
    errors = _errors(root)
    assert any("in-demo-c09" in m for m in errors["in-demo-c01"])
    assert any("idea-20261001-nope" in m for m in errors["in-demo-c01"])
    assert any("C05 already belongs" in m for m in errors["in-demo-c02"])


def test_cycle_in_folder_without_initiative_is_reported(root):
    write(root / "in-orphan" / "cycles" / "in-orphan-c01.md", cycle_md("in-orphan-c01"))
    (root / "in-orphan" / "initiative.md").write_text("not frontmatter", encoding="utf-8")
    snapshot = store.load(root)
    assert any(path.name == "initiative.md" for path in snapshot.parse_errors)


def test_release_observation_moves_stage_to_measuring_and_sets_evaluation_date(root):
    # Regression: merged-but-unreleased cycles start their measurement clock early.
    _cycle(root, stage="shipping", decisions=DECIDED, measurement=MEASUREMENT)
    _obs(
        root,
        "in-demo-c01",
        {"at": "2026-10-01T00:00:00Z", "by": "a", "kind": "release_failed", "detail": "404"},
        {"at": "2026-10-03T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md", "evidence_date": "2026-10-02"},
    )
    snapshot = store.load(root)
    rows = store.active_observations(snapshot.observations["in-demo-c01"])
    release = lifecycle.release_state(rows)
    fm = snapshot.cycles["in-demo-c01"].frontmatter
    assert release["state"] == "verified" and release["date"] == "2026-10-02"
    assert lifecycle.effective_stage(fm, release) == "measuring"
    assert lifecycle.evaluation_date(fm, release) == dt.date(2026, 10, 30)
    flags = lifecycle.cycle_flags(fm, "measuring", release, dt.date(2026, 10, 30), rows, [], 7, False, TODAY)
    assert "evaluation_due" in flags


def test_superseded_observation_is_ignored(root):
    _cycle(root, stage="shipping", decisions=DECIDED, measurement=MEASUREMENT)
    _obs(
        root,
        "in-demo-c01",
        {"at": "2026-10-03T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"},
        {"at": "2026-10-04T00:00:00Z", "by": "a", "kind": "note", "supersedes": "2026-10-03T00:00:00Z"},
    )
    rows = store.active_observations(store.load(root).observations["in-demo-c01"])
    assert lifecycle.release_state(rows)["state"] == "none"


def test_release_unverified_after_cards_done_beyond_grace(root):
    cards = [{"id": "t_1", "state": "fresh", "status": "done", "completed_at": "2026-10-01T00:00:00+00:00"}]
    release = {"state": "none", "date": None}
    flags = lifecycle.cycle_flags({"stage": "shipping"}, "shipping", release, None, [], cards, 7, False, TODAY)
    assert "release_unverified" in flags
    stale_cards = [{**cards[0], "state": "unavailable"}]
    assert "release_unverified" not in lifecycle.cycle_flags(
        {"stage": "shipping"}, "shipping", release, None, [], stale_cards, 7, False, TODAY
    )


def test_observation_stale_uses_observe_every_days(root):
    rows = [{"at": "2026-10-20T00:00:00Z", "kind": "metric"}]
    release = {"state": "verified", "date": "2026-10-01"}
    fm = {"measurement": MEASUREMENT}
    assert "observation_stale" not in lifecycle.cycle_flags(fm, "measuring", release, None, rows, [], 7, False, TODAY)
    assert "observation_stale" in lifecycle.cycle_flags(fm, "measuring", release, None, rows, [], 3, False, TODAY)


def test_unchanged_files_are_not_reparsed(root):
    _cycle(root)
    store.load(root)
    before = store.cache_parse_count()
    store.load(root)
    assert store.cache_parse_count() == before
    _cycle(root, title="Changed title that also changes size")
    store.load(root)
    assert store.cache_parse_count() == before + 1


def test_too_many_cycles_before_shipping_is_a_warning_not_an_error(root):
    """Regression: an initiative silently accumulates exploring/decided cycles past what three lanes can work."""
    for n in range(1, 4):
        _cycle(root, f"in-demo-c0{n}", stage="decided" if n == 1 else "exploring", decisions=DECIDED if n == 1 else [])
    report = validate(store.load(root), check_sources=False)
    assert report.ok
    assert any("3 cycles are exploring or decided" in w for w in report.warnings["in-demo"])


def test_cycles_at_the_wip_limit_raise_no_warning(root):
    _cycle(root, "in-demo-c01")
    _cycle(root, "in-demo-c02")
    _cycle(root, "in-demo-c03", stage="shipping", decisions=DECIDED, measurement=MEASUREMENT)
    report = validate(store.load(root), check_sources=False)
    assert not any("exploring or decided" in w for w in report.warnings.get("in-demo", []))


VERIFIED_ROW = {"at": "2026-10-11T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}


def test_spot_check_cycle_needs_no_observation_window(root):
    # c02 sale precompute was judged once against its criteria; a fake one-day window was the only way past this.
    spot = {k: v for k, v in MEASUREMENT.items() if k != "observation_window"}
    _cycle(root, stage="measuring", decisions=DECIDED, measurement=spot)
    _obs(root, "in-demo-c01", VERIFIED_ROW)
    assert not _errors(root).get("in-demo-c01")
    _cycle(root, stage="measuring", decisions=DECIDED, measurement={**spot, "success_criteria": None})
    assert any("observation_window" in m for m in _errors(root)["in-demo-c01"]), "without criteria a window is needed"


def test_free_text_observation_window_is_an_error(root):
    # Free text such as "7 days after C and D land" never yields an evaluation date.
    _cycle(root, stage="measuring", decisions=DECIDED, measurement={**MEASUREMENT, "observation_window": "7 days"})
    _obs(root, "in-demo-c01", VERIFIED_ROW)
    assert any("integer length_days" in m for m in _errors(root)["in-demo-c01"])
