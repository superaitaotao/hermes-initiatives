"""A discussion must preserve measurement ownership, not mistake finished delivery for finished proof."""

import datetime as dt
import json

import pytest
from conftest import DECIDED, MEASUREMENT, cycle_md, write

from hermes_initiatives_core import views
from hermes_initiatives_core.settings import ProjectSettings


@pytest.fixture
def measurement_project(root, monkeypatch):
    assessment = {
        "id": "t_00000002",
        "title": "Assessment",
        "state": "fresh",
        "status": "blocked",
        "assignee": "measurement-owner",
        "pr": {"state": "none", "url": None},
    }
    delivery = {
        "id": "t_00000001",
        "title": "Delivery",
        "state": "fresh",
        "status": "done",
        "assignee": "coder",
        "completed_at": "2026-10-01",
        "pr": {"state": "merged", "url": "https://example.com/pr/1"},
    }
    contract = {
        "enabled": True,
        "owner": "measurement-owner",
        "release_surface": "production",
        "collector": "Read the released cohort",
        "review_by": "2026-11-30",
        "target_date": None,
    }
    write(
        root / "in-demo/cycles/in-demo-c01.md",
        cycle_md(
            "in-demo-c01",
            stage="shipping",
            decisions=DECIDED,
            measurement=MEASUREMENT,
            kanban_cards=[delivery["id"]],
            assessment_cards=[assessment["id"]],
            outcome_review=contract,
        ),
    )
    monkeypatch.setattr(
        views,
        "_kanban",
        lambda *a: (
            {"state": "fresh", "error": None, "cards": {c["id"]: c for c in [delivery, assessment]}},
            {},
        ),
    )
    return ProjectSettings(id="demo", label="Demo", root=root), assessment, contract


@pytest.mark.parametrize("kind", ["discussion", "cycle"])
def test_session_context_keeps_active_assessment_and_contract(measurement_project, kind):
    project, assessment, contract = measurement_project
    day = dt.date(2026, 12, 1)
    if kind == "discussion":
        payload = json.loads(views.discussion_context(project, "in-demo", day)["context"])
        cycle = payload["cycles"][0]
    else:
        payload = json.loads(views.subject_context(project, "in-demo", "cycle", "in-demo-c01", day)["context"])
        cycle = payload["cycle"]
    assert cycle["outcome_review"] == contract
    assert cycle["assessment_cards"][0]["id"] == assessment["id"]
    assert cycle["assessment_cards"][0]["status"] == "blocked"
    assert cycle["assessment_cards"][0]["assignee"] == "measurement-owner"
    assert cycle["outcome_loop"]["state"] == "assessment_running"
    assert cycle["outcome_loop"]["assessment_card"] == assessment["id"]
    assert "blocked" in cycle["next_action"]
    assert "measurement-owner" in cycle["next_action"]
    assert cycle["cards"][0]["status"] == "done"


@pytest.mark.parametrize("kind", ["discussion", "cycle"])
def test_long_collector_is_trimmed_without_losing_ownership(measurement_project, kind):
    from hermes_initiatives_core import writers

    project, assessment, _ = measurement_project
    writers.edit_frontmatter(
        project.root / "in-demo/cycles/in-demo-c01.md",
        project.root,
        lambda fm: fm["outcome_review"].update(collector="collection detail " * 4000),
    )
    day = dt.date(2026, 12, 1)
    context = (
        views.discussion_context(project, "in-demo", day)
        if kind == "discussion"
        else views.subject_context(project, "in-demo", "cycle", "in-demo-c01", day)
    )
    payload = json.loads(context["context"])
    cycle = payload["cycles"][0] if kind == "discussion" else payload["cycle"]
    assert cycle["outcome_review"]["owner"] == "measurement-owner"
    assert "collector" not in cycle["outcome_review"]
    assert cycle["assessment_cards"][0]["id"] == assessment["id"]
    assert cycle["outcome_loop"]["state"] == "assessment_running"
    assert len(context["context"]) <= views.CONTEXT_LIMIT


@pytest.mark.parametrize("kind", ["cycle", "card"])
def test_invalid_contract_budget_fallback_still_returns_context(measurement_project, monkeypatch, kind):
    project, assessment, _ = measurement_project
    path = project.root / "in-demo/cycles/in-demo-c01.md"
    write(
        path,
        cycle_md(
            "in-demo-c01",
            stage="shipping",
            decisions=DECIDED,
            outcome_review="TBD",
            measurement={"source": "large " * 4000},
            kanban_cards=["t_00000001"],
            assessment_cards=[assessment["id"]],
        ),
    )
    monkeypatch.setattr(
        views,
        "read_card_detail",
        lambda *a: {
            **assessment,
            "comments": [],
            "runs": [],
            "body": "large " * 4000,
        },
    )
    subject = "in-demo-c01" if kind == "cycle" else assessment["id"]
    context = views.subject_context(project, "in-demo", kind, subject)
    assert context is not None
    payload = json.loads(context["context"])
    assert payload.get("truncated") is True or payload["initiative"]["id"] == "in-demo"
    assert len(context["context"]) <= views.CONTEXT_LIMIT


def test_summary_list_does_not_include_collector(measurement_project):
    project, _, _ = measurement_project
    cycle = views.build_list(project)["initiatives"][0]["open_cycles"][0]
    assert cycle["id"] == "in-demo-c01"
    assert "outcome_review" not in cycle


def test_unassigned_assessment_does_not_claim_configured_owner_is_assignee(measurement_project):
    project, assessment, _ = measurement_project
    assessment["assignee"] = None
    context = views.discussion_context(project, "in-demo", dt.date(2026, 12, 1))
    cycle = json.loads(context["context"])["cycles"][0]
    assert "unassigned" in cycle["next_action"]
    assert "configured owner measurement-owner" in cycle["next_action"]


@pytest.mark.parametrize("card_id", ["t_00000001", "t_00000002"])
def test_assessment_card_session_resolves_its_cycle_not_as_delivery(measurement_project, monkeypatch, card_id):
    project, assessment, _ = measurement_project
    monkeypatch.setattr(views, "read_card_detail", lambda *a: {**assessment, "id": card_id, "comments": [], "runs": []})
    context = views.subject_context(project, "in-demo", "card", card_id, dt.date(2026, 12, 1))
    assert context is not None
    payload = json.loads(context["context"])
    assert payload["card"]["id"] == card_id
    assert payload["cycles"][0]["assessment_cards"][0]["status"] == "blocked"
    assert payload["cycles"][0]["id"] == "in-demo-c01"
    assert payload["cycles"][0]["outcome_loop"]["assessment_card"] == assessment["id"]
    assert views.subject_context(project, "in-demo", "card", "t_ffffffff") is None
