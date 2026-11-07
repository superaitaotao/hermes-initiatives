"""Migration of existing initiatives to the outcome_review contract: dry run, rerun, preservation, scope."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, cycle_md, write

from hermes_initiatives_core import store, writers
from hermes_initiatives_core.docs import dump_markdown, parse_markdown
from hermes_initiatives_core.outcome_migration import (
    ADOPTION_DECISION,
    MigrationRefused,
    classify_surface,
    contract_gaps,
    migrate,
)
from hermes_initiatives_core.validate import validate

TODAY = "2026-10-23"
SESSION = "20261023_173441_8bdcd3"
OWNER = "planner"

MEASUREMENT = {
    "primary_metric": "P1 guide clicks",
    "source": "GSC",
    "baseline": "27 clicks / 3,821 impressions",
    "observation_window": {"starts": "2026-10-01", "length_days": 28},
    "success_criteria": "clicks +10%",
    "guardrails": "CTR not down",
    "insufficient_data": "fewer than 100 impressions",
}


def _initiative(init_id: str = "in-demo", status: str = "active", decisions: list[dict] | None = None, **extra) -> str:
    data = {
        "id": init_id,
        "title": f"Initiative {init_id}",
        "status": status,
        "rank": 3,
        "goal": "Grow demo",
        "observe_every_days": 7,
        "decisions": decisions if decisions is not None else [{"date": "2026-09-01", "by": "user", "decision": "Start"}],
    }
    data.update(extra)
    return "---\n" + json.dumps(data, indent=1) + "\n---\nBrief body.\n"


def _cycle_path(root: Path, cycle_id: str) -> Path:
    return root / cycle_id.rsplit("-c", 1)[0] / "cycles" / f"{cycle_id}.md"


def _cycle(root: Path, cycle_id: str, stage: str = "shipping", measurement: dict | None = None, **overrides) -> Path:
    data = {
        "stage": stage,
        "decisions": [{"date": "2026-09-02", "by": "user", "decision": "Run it"}],
        "kanban_cards": ["t_0000beef"],
        "delivery_plans": ["2026-10-01-demo-plan"],
        "change_register": ["C01"],
        "measurement": MEASUREMENT if measurement is None else measurement,
        "result": {"verdict": "unset", "evidence": ["evidence/report.md"]},
        "disposition": {"action": "unset", "follow_ups": [], "reason": None},
        "sources": ["docs/tasks/demo.md"],
        "question": "Does the change move the metric?",
        "hypothesis": "It does.",
    }
    data.update(overrides)
    return write(_cycle_path(root, cycle_id), cycle_md(cycle_id, **data))


def _docs(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".review-write.lock"
    }


def _fm(root: Path, cycle_id: str) -> dict:
    path = _cycle_path(root, cycle_id)
    return parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter


def _observation(root: Path, cycle_id: str, **row) -> None:
    base = {"at": "2026-10-05T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}
    base.update(row)
    log = root / cycle_id.rsplit("-c", 1)[0] / "observations" / f"{cycle_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(base) + "\n", encoding="utf-8")


def test_dry_run_reports_before_and_after_without_writing(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    before = _docs(root)

    report = migrate(root, OWNER, SESSION, TODAY)

    assert _docs(root) == before
    assert not (root / ".review-write.lock").exists()
    assert json.loads(json.dumps(report))["ok"] is True
    assert report["apply"] is False
    cycle = report["cycles"][0]
    assert cycle["status"] == "would_adopt" and cycle["changed"] is False
    assert cycle["before"]["outcome_review"] is None
    assert cycle["before"]["measurement"] == MEASUREMENT
    assert cycle["after"]["outcome_review"] == {
        "enabled": True,
        "owner": OWNER,
        "release_surface": "manual",
        "collector": cycle["after"]["outcome_review"]["collector"],
        "review_by": None,
        "target_date": None,
    }
    assert cycle["after"]["assessment_cards"] == []
    assert cycle["after"]["measurement"] == cycle["before"]["measurement"]
    assert report["initiatives"][0]["status"] == "would_adopt"
    assert report["initiatives"][0]["decision_added"] is False


def test_apply_twice_is_a_noop_with_one_adoption_decision(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")

    first = migrate(root, OWNER, SESSION, TODAY, apply=True)

    assert first["cycles"][0]["status"] == "adopted"
    assert _fm(root, "in-demo-c01")["outcome_review"]["enabled"] is True
    assert _fm(root, "in-demo-c01")["assessment_cards"] == []
    initiative = parse_markdown((root / "in-demo" / "initiative.md").read_text(encoding="utf-8"), root).frontmatter
    assert [row["decision"] for row in initiative["decisions"]].count(ADOPTION_DECISION) == 1
    assert initiative["decisions"][-1]["by"] == "user"
    assert initiative["decisions"][-1]["date"] == TODAY
    assert initiative["decisions"][-1]["session"] == SESSION
    assert validate(store.load(root), check_sources=False).ok
    after_first = _docs(root)

    second = migrate(root, OWNER, "20261023_999999_other", TODAY, apply=True)

    assert _docs(root) == after_first
    assert second["cycles"][0]["status"] == "existing"
    assert second["counts"]["cycles_adopted"] == 0
    assert second["counts"]["cycles_unchanged"] == 1
    assert second["initiatives"][0]["status"] == "existing"
    rows = parse_markdown((root / "in-demo" / "initiative.md").read_text(encoding="utf-8"), root).frontmatter["decisions"]
    assert len(rows) == len(initiative["decisions"])


def test_maintenance_onboards_future_cycles_only_under_adopted_initiatives(root):
    from hermes_initiatives_core.outcome_migration import maintain

    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    migrate(root, OWNER, SESSION, TODAY, apply=True)
    brief = (root / "in-demo" / "initiative.md").read_bytes()
    _cycle(root, "in-demo-c02")
    write(root / "in-other" / "initiative.md", _initiative("in-other"))
    _cycle(root, "in-other-c01")
    report = maintain(root, today=TODAY)
    assert report["counts"]["cycles_adopted"] == 1
    assert _fm(root, "in-demo-c02")["outcome_review"]["owner"] == OWNER
    assert "outcome_review" not in _fm(root, "in-other-c01")
    assert (root / "in-demo" / "initiative.md").read_bytes() == brief
    after = _docs(root)
    assert maintain(root, today=TODAY)["counts"]["cycles_adopted"] == 0
    assert _docs(root) == after


def test_disabled_policy_survives_migration_without_adoption_decision(root):
    path = write(root / "in-demo" / "initiative.md", _initiative())
    from hermes_initiatives_core import writers

    writers.edit_frontmatter(path, root, lambda fm: fm.update(outcome_policy={"enabled": False}))
    _cycle(root, "in-demo-c01")
    before = _docs(root)
    report = migrate(root, OWNER, SESSION, TODAY, apply=True)
    assert report["counts"]["cycles_adopted"] == 0
    assert _docs(root) == before


def test_preserves_stage_measurement_result_disposition_cards_and_body(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    path = _cycle(root, "in-demo-c01", stage="measuring")
    original = parse_markdown(path.read_text(encoding="utf-8"), path)

    migrate(root, OWNER, SESSION, TODAY, apply=True)

    after = parse_markdown(path.read_text(encoding="utf-8"), path)
    for key in (
        "id",
        "title",
        "stage",
        "question",
        "hypothesis",
        "delivery_plans",
        "kanban_cards",
        "change_register",
        "decisions",
        "measurement",
        "result",
        "disposition",
        "sources",
    ):
        assert after.frontmatter[key] == original.frontmatter[key]
    assert after.body == original.body
    initiative = parse_markdown((root / "in-demo" / "initiative.md").read_text(encoding="utf-8"), root)
    assert initiative.frontmatter["rank"] == 3


def test_existing_outcome_review_and_assessment_cards_are_untouched(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    existing = {
        "enabled": True,
        "owner": "someone-else",
        "release_surface": "main_ci",
        "collector": "Use the recorded CI report.",
        "review_by": "2026-12-01",
        "target_date": "2026-12-31",
    }
    _cycle(root, "in-demo-c01", outcome_review=existing, assessment_cards=["t_0000cafe"])
    cycle_before = _cycle_path(root, "in-demo-c01").read_bytes()

    report = migrate(root, OWNER, SESSION, TODAY, apply=True)

    cycle = report["cycles"][0]
    assert cycle["status"] == "existing" and cycle["changed"] is False
    assert report["counts"]["cycles_adopted"] == 0
    assert _cycle_path(root, "in-demo-c01").read_bytes() == cycle_before
    assert report["initiatives"][0]["status"] == "adopted"
    assert _fm(root, "in-demo-c01")["outcome_review"] == existing
    assert _fm(root, "in-demo-c01")["assessment_cards"] == ["t_0000cafe"]


def test_paused_closed_and_terminal_records_are_skipped(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    _cycle(root, "in-demo-c02", stage="concluded")
    _cycle(root, "in-demo-c03", stage="abandoned")
    write(root / "in-paused" / "initiative.md", _initiative("in-paused", status="paused"))
    _cycle(root, "in-paused-c01")
    write(root / "in-closed" / "initiative.md", _initiative("in-closed", status="closed"))
    _cycle(root, "in-closed-c01")
    untouched = {cid: _docs(root) for cid in ("in-demo-c02", "in-demo-c03", "in-paused-c01", "in-closed-c01")}

    report = migrate(root, OWNER, SESSION, TODAY, apply=True)

    assert [cycle["id"] for cycle in report["cycles"]] == ["in-demo-c01"]
    assert report["counts"]["cycles_open"] == 1
    reasons = {item["id"]: item["reason"] for item in report["skipped"]}
    assert "terminal" in reasons["in-demo-c02"]
    assert "terminal" in reasons["in-demo-c03"]
    assert "paused" in reasons["in-paused-c01"]
    assert "closed" in reasons["in-closed-c01"]
    for cycle_id, snapshot in untouched.items():
        path = _cycle_path(root, cycle_id)
        assert str(path.relative_to(root)) in snapshot
        assert _docs(root)[str(path.relative_to(root))] == snapshot[str(path.relative_to(root))]
    assert _fm(root, "in-demo-c01")["outcome_review"]["enabled"] is True
    initiative = parse_markdown((root / "in-paused" / "initiative.md").read_text(encoding="utf-8"), root)
    assert ADOPTION_DECISION not in [row["decision"] for row in initiative.frontmatter["decisions"]]


def test_surface_comes_from_recorded_environment_and_never_from_prose_over_runtime(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01", title="Production rollout of the worker pool")
    _observation(root, "in-demo-c01", environment="runtime")
    _cycle(root, "in-demo-c02", title="School freshness")
    _observation(root, "in-demo-c02", environment="production")

    report = migrate(root, OWNER, SESSION, TODAY)
    surfaces = {cycle["id"]: cycle for cycle in report["cycles"]}

    assert surfaces["in-demo-c01"]["release_surface"] == "runtime"
    assert "runtime" in surfaces["in-demo-c01"]["surface_basis"][0]
    assert surfaces["in-demo-c02"]["release_surface"] == "production"


def test_unrecognised_or_missing_evidence_keeps_manual(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    _observation(root, "in-demo-c01", environment="lab-box")
    _cycle(root, "in-demo-c02", measurement={})

    report = migrate(root, OWNER, SESSION, TODAY)
    surfaces = {cycle["id"]: cycle for cycle in report["cycles"]}

    assert surfaces["in-demo-c01"]["release_surface"] == "manual"
    assert "not a recognised release surface" in surfaces["in-demo-c01"]["surface_basis"][0]
    assert surfaces["in-demo-c02"]["release_surface"] == "manual"
    assert "identify" in surfaces["in-demo-c02"]["surface_basis"][0] or "manual" in surfaces["in-demo-c02"]["surface_basis"][0]


def _text_surface(path: Path, cycle_id: str) -> str:
    frontmatter = parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter
    return classify_surface(frontmatter, [], cycle_id, {})[0]


def test_text_evidence_classifies_main_ci_and_production_without_environment(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    ci = _cycle(
        root,
        "in-demo-c01",
        measurement={"primary_metric": "test minutes", "source": "Playwright reports from main ci.yml browser-tests jobs"},
    )
    production = _cycle(
        root,
        "in-demo-c02",
        title="Cloud Run promotion of the answers layer",
        delivery_plans=["2026-10-03-cloud-run-promotion-plan"],
    )

    assert _text_surface(ci, "in-demo-c01") == "main_ci"
    assert _text_surface(production, "in-demo-c02") == "production"


def test_explicit_surface_mapping_wins_and_unknown_cycle_warns(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    _observation(root, "in-demo-c01", environment="production")

    report = migrate(root, OWNER, SESSION, TODAY, surfaces={"in-demo-c01": "manual", "in-demo-c09": "runtime"})

    cycle = report["cycles"][0]
    assert cycle["release_surface"] == "manual"
    assert cycle["surface_basis"] == ["explicit surface mapping for in-demo-c01"]
    assert cycle["after"]["outcome_review"]["release_surface"] == "manual"
    assert any("in-demo-c09" in warning for warning in report["warnings"])


def test_invalid_surface_value_is_refused(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    with pytest.raises(MigrationRefused, match="production, main_ci, runtime, manual"):
        migrate(root, OWNER, SESSION, TODAY, surfaces={"in-demo-c01": "staging"})


def test_collector_grounds_recorded_fields_commands_and_gaps(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(
        root,
        "in-demo-c01",
        measurement={
            "primary_metric": "planner share of worker minutes",
            "source": "scripts/flow_window.py and GSC",
            "baseline": "31%",
            "observation_window": {"starts": "2026-10-20", "length_days": 7},
            "success_criteria": None,
            "guardrails": "no drop in delivered cards",
        },
    )

    report = migrate(root, OWNER, SESSION, TODAY)
    collector = report["cycles"][0]["after"]["outcome_review"]["collector"]

    assert "primary metric: planner share of worker minutes" in collector
    assert "baseline: 31%" in collector
    assert "success criteria: NOT RECORDED (success_criteria)" in collector
    assert "guardrails: no drop in delivered cards" in collector
    assert "scripts/flow_window.py" in collector
    assert "record_observation.py" in collector and "record_assessment.py" in collector
    assert "Known contract gaps: success_criteria" in collector
    assert report["gaps"]["in-demo-c01"] == ["success_criteria"]


def test_collector_does_not_name_a_command_when_none_is_recorded(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01", measurement={})

    collector = migrate(root, OWNER, SESSION, TODAY)["cycles"][0]["after"]["outcome_review"]["collector"]

    assert "No command is recorded" in collector
    assert "scripts/" not in collector
    assert "Known contract gaps: measurement" in collector


def test_contract_gaps_name_missing_window_dates_and_fields(root):
    write(root / "in-demo" / "initiative.md", _initiative())

    assert contract_gaps({"measurement": MEASUREMENT}) == []
    assert contract_gaps({"measurement": {}}) == ["measurement"]
    partial = {
        "measurement": {
            "primary_metric": "clicks",
            "source": "GSC",
            "baseline": "1",
            "success_criteria": "up",
            "guardrails": "flat",
            "observation_window": {"length_days": 28},
        }
    }
    assert contract_gaps(partial) == ["observation_window.starts"]
    assert contract_gaps({"measurement": {"observation_window": {"starts": "verified_release"}}}) == [
        "primary_metric",
        "source",
        "baseline",
        "success_criteria",
        "guardrails",
        "observation_window.length_days",
    ]


def test_missing_measurement_still_produces_a_contract_and_a_gap_report(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01", measurement={})

    report = migrate(root, OWNER, SESSION, TODAY, apply=True)

    cycle = report["cycles"][0]
    assert cycle["status"] == "adopted"
    assert cycle["contract_gaps"] == ["measurement"]
    assert report["counts"]["cycles_with_contract_gaps"] == 1
    review = _fm(root, "in-demo-c01")["outcome_review"]
    assert review["enabled"] is True and "propose the missing contract" in review["collector"]
    assert _fm(root, "in-demo-c01")["result"] == {"verdict": "unset", "evidence": ["evidence/report.md"]}


def test_concurrent_edit_is_refused_not_clobbered(root, monkeypatch):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    original = writers.edit_frontmatter
    cycle_path = _cycle_path(root, "in-demo-c01")

    def racing(path, write_root, change):
        if path == cycle_path:
            doc = parse_markdown(path.read_text(encoding="utf-8"), path)
            frontmatter = dict(doc.frontmatter)
            frontmatter["stage"] = "measuring"
            path.write_text(dump_markdown(frontmatter, doc.body), encoding="utf-8")
        return original(path, write_root, change)

    monkeypatch.setattr(writers, "edit_frontmatter", racing)
    report = migrate(root, OWNER, SESSION, TODAY, apply=True)

    assert report["ok"] is False
    assert report["cycles"][0]["status"] == "conflict"
    assert report["initiatives"][0]["status"] == "blocked"
    after = _fm(root, "in-demo-c01")
    assert "outcome_review" not in after
    assert after["stage"] == "measuring"


def test_concurrently_appended_decision_is_merged_before_the_adoption_row(root, monkeypatch):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    original = writers.edit_frontmatter
    initiative_path = root / "in-demo" / "initiative.md"
    concurrent = {"date": TODAY, "by": "user", "decision": "A decision recorded concurrently"}

    def racing(path, write_root, change):
        if path == initiative_path:
            doc = parse_markdown(path.read_text(encoding="utf-8"), path)
            frontmatter = dict(doc.frontmatter)
            frontmatter["decisions"] = list(frontmatter.get("decisions") or []) + [concurrent]
            path.write_text(dump_markdown(frontmatter, doc.body), encoding="utf-8")
        return original(path, write_root, change)

    monkeypatch.setattr(writers, "edit_frontmatter", racing)
    report = migrate(root, OWNER, SESSION, TODAY, apply=True)

    assert report["ok"] is True
    rows = parse_markdown(initiative_path.read_text(encoding="utf-8"), initiative_path).frontmatter["decisions"]
    assert rows[-2] == concurrent
    assert rows[-1]["decision"] == ADOPTION_DECISION


def test_cli_dry_run_apply_and_session_requirement(root):
    write(root / "in-demo" / "initiative.md", _initiative())
    _cycle(root, "in-demo-c01")
    script = ROOT / "skills" / "initiatives" / "scripts" / "migrate_outcome_loops.py"
    surfaces = root / "surfaces.json"
    surfaces.write_text(json.dumps({"in-demo-c01": "main_ci"}), encoding="utf-8")
    before = _docs(root)

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), "--root", str(root), "--session", SESSION, "--today", TODAY, *extra],
            capture_output=True,
            text=True,
            env={**os.environ, "HERMES_INITIATIVES_DATA_DIR": str(root / "settings")},
            check=False,
        )

    dry = run("--owner", OWNER, "--surfaces", str(surfaces))
    assert dry.returncode == 0, dry.stderr
    report = json.loads(dry.stdout)
    assert report["apply"] is False and report["cycles"][0]["release_surface"] == "main_ci"
    assert _docs(root) == before

    missing_session = subprocess.run(
        [sys.executable, str(script), "--root", str(root), "--owner", OWNER, "--today", TODAY],
        capture_output=True,
        text=True,
        env={**os.environ, "HERMES_INITIATIVES_DATA_DIR": str(root / "settings")},
        check=False,
    )
    assert missing_session.returncode == 2
    assert "--session" in missing_session.stderr

    applied = run("--owner", OWNER, "--surfaces", str(surfaces), "--apply")
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["counts"]["cycles_adopted"] == 1
    assert _fm(root, "in-demo-c01")["outcome_review"]["release_surface"] == "main_ci"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("source: project/agents/scripts/measure.py", ["project/agents/scripts/measure.py"]),
        ("see `origin/main:backend/middleware/rate_limit.py:143`", ["backend/middleware/rate_limit.py"]),
        ("run scripts/flow_window.py and GSC", ["scripts/flow_window.py"]),
        ("tool .venv/bin/pytest -q", [".venv/bin/pytest"]),
        ("https://example.com/static/app.js and /abs/path/x.py and docs/x.md", []),
    ],
)
def test_script_command_finds_repo_relative_scripts_in_any_directory(text, expected):
    from hermes_initiatives_core.outcome_migration import SCRIPT_COMMAND

    assert [m.group(1) for m in SCRIPT_COMMAND.finditer(text)] == expected
