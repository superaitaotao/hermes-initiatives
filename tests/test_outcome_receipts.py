"""Outcome evidence is durable preparation, never a user verdict or delivery-card alias."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import DECIDED, cycle_md, write

from hermes_initiatives_core import lifecycle, review_collect, reviews, writers
from hermes_initiatives_core.docs import dump_markdown, parse_markdown
from hermes_initiatives_core.settings import ProjectSettings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate


@pytest.fixture
def cycle(root):
    path = root / "in-demo/cycles/in-demo-c01.md"
    write(path, cycle_md("in-demo-c01", stage="shipping", decisions=DECIDED))
    return path


def test_assessment_receipt_requires_real_artifacts_and_preserves_user_fields(root, cycle, tmp_path):
    before = cycle.read_bytes()
    fields = {
        "kind": "assessment",
        "epoch": "epoch-1",
        "status": "missed",
        "detail": "Measured gap",
        "evidence": str(tmp_path / "report.md"),
        "proposal": str(tmp_path / "proposal.md"),
    }
    with pytest.raises(writers.WriteRefused, match="existing absolute"):
        writers.record_observation(root, "in-demo-c01", fields)
    write(Path(fields["evidence"]), "Measured 140.5 against target 120; six flakes.")
    write(Path(fields["proposal"]), "Proposed bounded readiness fix; awaiting user scope decision.")
    row = writers.record_observation(root, "in-demo-c01", fields)
    assert row["status"] == "missed" and row["proposal"] == fields["proposal"]
    assert len(row["artifact_sha256"]["evidence"]) == 64
    assert cycle.read_bytes() == before
    assert validate(load(root), check_sources=False).ok
    assert parse_markdown(cycle.read_text(), cycle).frontmatter["result"]["verdict"] == "unset"


def test_outcome_gate_arrives_only_after_assessment_and_contains_proposal(monkeypatch, root, cycle):
    project = ProjectSettings(id="demo", label="Demo", root=root)
    base = {
        "id": "in-demo-c01",
        "title": "Cycle",
        "stage": "shipping",
        "flags": [],
        "cards": [],
        "path": "in-demo/cycles/in-demo-c01.md",
        "result": {},
        "disposition": {},
        "outcome_loop": {
            "enabled": True,
            "epoch": "epoch-1",
            "state": "assessment_running",
            "contract_gaps": [],
            "next_action": "Agent measures",
            "assessment": None,
        },
    }
    detail = {"path": "in-demo/initiative.md", "cycles": [base]}
    monkeypatch.setattr(review_collect, "build_detail", lambda *a, **k: detail)
    snapshot = load(root)
    # The admission contract item is separate by design; this test is about the outcome gate staying quiet.
    gates = review_collect._cycle_gates(project, snapshot, dt.date(2026, 10, 23))
    assert [g for g in gates if not g["key"].endswith(":measure")] == []
    base["outcome_loop"].update(
        state="review_ready",
        assessment={
            "at": "2026-10-23T10:00:00Z",
            "status": "missed",
            "evidence": "/reports/gap.md",
            "proposal": "/reports/plan.md",
            "detail": "17% over",
        },
    )
    candidates = review_collect._cycle_gates(project, snapshot, dt.date(2026, 10, 23))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["key"] == "cycle:demo:in-demo-c01:gate"
    assert candidate["basis"]["reasons"] == ["outcome_review_ready"]
    assert {link["path"] for link in candidate["links"]} >= {"/reports/gap.md", "/reports/plan.md"}
    store = reviews.ReviewStore()
    candidate.pop("context")
    first, _ = store.upsert_item(**candidate, section="gated")
    second, _ = store.upsert_item(**candidate, section="gated")
    assert first["short_id"] == second["short_id"]


def test_outcome_contract_validation_does_not_make_up_deadline(root, cycle):
    writers.edit_frontmatter(
        cycle,
        root,
        lambda fm: fm.update(
            outcome_review={
                "enabled": True,
                "owner": "planner",
                "collector": "Read source reports",
                "release_surface": "main_ci",
                "review_by": None,
                "target_date": None,
            }
        ),
    )
    report = validate(load(root), check_sources=False)
    assert report.ok
    assert any("review deadline" in w for w in report.warnings["in-demo-c01"])
    # The writer refuses an edit that adds a cycle error, so plant the bad date by hand: a manual
    # Markdown edit is the path the validator and collector catch afterwards.
    doc = parse_markdown(cycle.read_text(encoding="utf-8"), cycle)
    doc.frontmatter["outcome_review"]["review_by"] = "not-a-date"
    write(cycle, dump_markdown(doc.frontmatter, doc.body))
    report = validate(load(root), check_sources=False)
    assert any("review_by" in e for e in report.errors["in-demo-c01"])


def test_assessment_and_release_notes_do_not_refresh_metric_freshness():
    today = dt.date(2026, 10, 23)
    rows = [
        {"kind": "metric", "at": "2026-09-01", "value": "1"},
        {"kind": "assessment", "at": "2026-10-23"},
        {"kind": "release_verified", "at": "2026-10-23"},
    ]
    fm = {"measurement": {"success_criteria": "clicks +10%"}}
    flags = lifecycle.cycle_flags(fm, "measuring", {"state": "verified"}, None, rows, [], 7, False, today)
    assert "observation_stale" in flags
    rows.append({"kind": "guardrail", "at": "2026-10-23", "value": "CTR stable"})
    flags = lifecycle.cycle_flags(fm, "measuring", {"state": "verified"}, None, rows, [], 7, False, today)
    assert "observation_stale" not in flags


def test_receipt_inspection_uses_unlinked_delivery_markers(monkeypatch, root, cycle, capsys):
    from hermes_initiatives_core import kanban, outcome_loop

    scripts = Path(__file__).resolve().parents[1] / "skills/initiatives/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("receipt_script", scripts / "record_assessment.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    writers.edit_frontmatter(
        cycle,
        root,
        lambda fm: fm.update(
            outcome_review={
                "enabled": True,
                "owner": "planner",
                "release_surface": "main_ci",
                "collector": "Read main CI",
                "review_by": None,
                "target_date": None,
            }
        ),
    )
    cards = {"t_00000001": {"id": "t_00000001", "state": "fresh", "status": "done", "completed_at": "2026-10-01"}}
    monkeypatch.setattr(
        kanban,
        "cycle_markers",
        lambda board: {
            "state": "fresh",
            "markers": {"in-demo-c01": list(cards)},
        },
    )
    monkeypatch.setattr(kanban, "read_cards", lambda board, ids: {"state": "fresh", "cards": {c: cards[c] for c in ids}})
    monkeypatch.setattr(
        sys,
        "argv",
        [str(scripts / "record_assessment.py"), "--root", str(root), "--board", "default", "--cycle", "in-demo-c01", "--inspect"],
    )
    script.main()
    observed = json.loads(capsys.readouterr().out)
    fm = parse_markdown(cycle.read_text(), cycle).frontmatter
    expected = outcome_loop.derive(fm, [], list(cards.values()), [], dt.date.today())
    assert observed["epoch"] == expected["epoch"]
    assert observed["epoch"] != outcome_loop.derive(fm, [], [], [], dt.date.today())["epoch"]
    report = write(root / "report.md", "Assessment epoch: stale\nOld interim evidence")
    proposal = write(root / "proposal.md", "Assessment epoch: stale\nOld proposal")
    argv = [
        str(scripts / "record_assessment.py"),
        "--root",
        str(root),
        "--board",
        "default",
        "--cycle",
        "in-demo-c01",
        "--epoch",
        observed["epoch"],
        "--status",
        "insufficient_data",
        "--evidence",
        str(report),
        "--proposal",
        str(proposal),
        "--detail",
        "Measured",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        script.main()
    assert "must bind the collection epoch" in capsys.readouterr().err
    report.write_text(f"Assessment epoch: {observed['epoch']}\nFresh evidence")
    proposal.write_text(f"Assessment epoch: {observed['epoch']}\nFresh proposal")
    script.main()
    saved = json.loads(capsys.readouterr().out)
    assert saved["ok"] is True
    assert saved["observation"]["epoch"] == observed["epoch"]
