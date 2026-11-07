"""Settings: the open-source fields, their defaults, and a bad optional field never costing every project."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes_initiatives_core import settings as S

PROJECT = {"id": "demo", "label": "Demo", "root": "/tmp/demo-root", "kanban_board": "demo"}


def _write(doc: dict) -> None:
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps(doc))


def test_legacy_file_without_new_fields_loads_with_defaults():
    _write({"projects": [PROJECT], "review_target": "telegram:1:2"})
    config, error = S.load_settings()
    assert error is None and [p.id for p in config.projects] == ["demo"]
    assert config.owner_name == "the user" and config.digest_at == "08:30"
    assert list(config.push_hours()) == list(range(8, 24)) and config.alert_jobs == []


@pytest.mark.parametrize(
    "bad",
    [
        {"timezone": "Mars/Olympus"},
        {"alert_jobs": [{"job": "has spaces", "label": "x"}]},
        {"digest_at": "8:30pm"},
        {"push_start_hour": 20, "push_end_hour": 9},
        {"owner_name": ""},
    ],
)
def test_bad_optional_global_keeps_projects_and_reports_it(bad):
    _write({"projects": [PROJECT], "review_target": "telegram:1:2", **bad})
    config, error = S.load_settings()
    assert [p.id for p in config.projects] == ["demo"], "a bad optional field must not drop every project"
    assert config.review_target == "telegram:1:2"
    assert error and "ignored" in error
    for name in bad:
        assert name in error
        assert getattr(config, name) == S.Settings.model_fields[name].get_default(call_default_factory=True)


def test_bad_optional_project_field_drops_only_that_field():
    _write({"projects": [{**PROJECT, "github_repo": "not a repo", "policy_skill": "my-policy"}]})
    config, error = S.load_settings()
    project = config.project("demo")
    assert project is not None and project.github_repo is None and project.policy_skill == "my-policy"
    assert "projects[0].github_repo" in error


def test_bad_required_field_still_refuses_the_file():
    _write({"projects": [{**PROJECT, "id": "Bad Id"}], "timezone": "Mars/Olympus"})
    config, error = S.load_settings()
    assert config.projects == [] and error


def test_host_zone_follows_dst(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    zone = S.Settings().zone()
    summer = dt.datetime(2026, 7, 1, 7, 30, tzinfo=dt.UTC).astimezone(zone)
    winter = dt.datetime(2026, 1, 15, 7, 30, tzinfo=dt.UTC).astimezone(zone)
    assert (summer.hour, winter.hour) == (8, 7), "the host zone must carry DST rules, not today's fixed offset"
    assert S.Settings().zone_label() == "Europe/London"


def test_configured_zone_wins_over_host(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    assert S.Settings(timezone="Asia/Tokyo").zone() == ZoneInfo("Asia/Tokyo")


@pytest.mark.parametrize("approvers", [[42], ["42"], [" 42 "]])
def test_numeric_and_string_approver_ids_are_both_accepted(approvers):
    _write({"projects": [PROJECT], "review_target": "telegram:123:7", "review_approvers": approvers})
    config, error = S.load_settings()
    assert error is None and config.answer_approvers() == {"42"}


@pytest.mark.parametrize("approvers", [["@alice"], [True], [4.2], "42"])
def test_an_invalid_approver_list_refuses_everyone_instead_of_falling_back_to_the_chat_owner(approvers):
    _write({"projects": [PROJECT], "review_target": "telegram:123:7", "review_approvers": approvers})
    config, error = S.load_settings()
    assert error and "review_approvers" in error
    assert [p.id for p in config.projects] == ["demo"]
    assert config.answer_approvers() is None, "a broken list must not hand answers to the private chat's owner"


def test_approvers_invalid_is_not_a_settings_file_field():
    config = S.Settings(review_target="telegram:123:7")
    assert "approvers_invalid" not in json.loads(config.model_dump_json())
