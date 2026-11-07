"""Autonomy installation stays profile-local and never duplicates named jobs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def test_resolves_project_config(tmp_path):
    from autonomy.config import resolve_config
    from hermes_initiatives_core.settings import Settings

    settings = Settings.model_validate({"projects": [{"id": "demo", "label": "Demo", "root": str(tmp_path),
        "kanban_board": "demo-board", "github_repo": "example/demo", "worker_profiles": ["coder", "reviewer"]}]})
    cfg = resolve_config(project="demo", settings=settings)
    assert cfg.board == "demo-board"
    assert cfg.repo == "example/demo"
    assert cfg.workers == {"coder", "reviewer"}
    override = resolve_config(project="demo", board="other", repo="other/repo", workers=["planner"], settings=settings)
    assert (override.board, override.repo, override.workers) == ("other", "other/repo", {"planner"})


def test_setup_wrapper_and_idempotence(tmp_path):
    from hermes_initiatives_core.autonomy_setup import build_plan
    from hermes_initiatives_core.settings import Settings

    settings = Settings.model_validate({"review_target": "telegram:123:456", "projects": [{"id": "demo", "label": "Demo",
        "root": str(tmp_path), "kanban_board": "default", "worker_profiles": ["coder"],
        "policy_skill": "demo-policy", "merge_reviewer": "reviewer", "cwd": str(tmp_path)}]})
    home = tmp_path / "profile"
    plan = build_plan(settings, "demo", "test", home=home)
    assert len(plan) == 3
    recovery = next(p for p in plan if p.kind == "recovery")
    wrapper = recovery.wrapper("abc123", None)
    assert str(Path(sys.executable).absolute()) in wrapper
    assert "--project demo" in wrapper and "--board default" in wrapper
    assert "--job abc123" in wrapper and "--recovery-signals" in wrapper
    assert "env -u PYTHONPATH" in wrapper and str(home / "scripts") in str(recovery.wrapper_path)
    existing = [{"name": p.name, "id": f"job{i}"} for i, p in enumerate(plan)]
    again = build_plan(settings, "demo", "test", home=home, existing=existing)
    assert all(p.existing_id for p in again)
    assert not any(p.create_command for p in again)


def test_dry_run_writes_nothing(tmp_path, capsys):
    from hermes_initiatives_core.autonomy_setup import build_plan, execute_plan
    from hermes_initiatives_core.settings import Settings

    settings = Settings.model_validate({"projects": [
        {"id": "demo", "label": "Demo", "root": str(tmp_path), "kanban_board": "default"}]})
    plan = build_plan(settings, "demo", "test", home=tmp_path / "untouched")
    before = set(tmp_path.rglob("*"))
    execute_plan(plan, dry_run=True)
    assert set(tmp_path.rglob("*")) == before
    text = capsys.readouterr().out
    assert "cron create" in text and "--paused" in text and "cron edit" in text and "cron resume" in text


def test_doctor_invalid_settings(tmp_path, monkeypatch):
    from hermes_initiatives_core.autonomy_setup import doctor

    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"timezone": "not/a/zone"}))
    monkeypatch.setenv("HERMES_INITIATIVES_DATA_DIR", str(data))
    errors = doctor("demo")
    assert any("settings" in e and "timezone" in e for e in errors)


@pytest.mark.parametrize("target", ["telegram:123", "telegram:abc:1", "slack:1:2"])
def test_setup_refuses_a_target_answers_cannot_reach(tmp_path, target):
    """The hook and the buttons read answers only in a chat topic, so setup never pushes anywhere else."""
    from hermes_initiatives_core.autonomy_setup import build_plan
    from hermes_initiatives_core.settings import Settings

    settings = Settings.model_validate({"review_target": target, "projects": [
        {"id": "demo", "label": "Demo", "root": str(tmp_path), "kanban_board": "default"}]})
    with pytest.raises(ValueError, match="telegram:<chat>:<topic>"):
        build_plan(settings, "demo", "test", home=tmp_path / "p")


def test_setup_has_no_target_override():
    import argparse

    from hermes_initiatives_core.cli import register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--profile", "p", "--project", "demo", "--review-target", "telegram:1:2"])
