"""Generalized Tasks API: canonical task files and read-only legacy bug aliases."""

from __future__ import annotations

import json

import pytest
from test_bugs import API, BUG, DAY, _client
from test_bugs import project as project_fixture

from hermes_initiatives_core import writers
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

project = project_fixture


def test_legacy_bugs_are_normalized_without_rewriting_and_filterable(project):
    bug_id = writers.report_bug(project, BUG, today=DAY)["id"]
    writers.update_bug(project, bug_id, status="fixed", reason="Already repaired")
    path = project / "bugs" / f"{bug_id}.md"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    client = _client()
    task = client.post(f"{API}/projects/demo/tasks", json={"title": "Export", "description": "CSV download"}).json()["task"]
    data = client.get(f"{API}/projects/demo/tasks").json()
    assert {r["id"] for r in data["tasks"]} == {bug_id, task["id"]}
    bug = next(r for r in data["tasks"] if r["id"] == bug_id)
    assert (bug["type"], bug["description"], bug["priority"], bug["status"], bug["card_id"]) == (
        "bug",
        BUG["symptom"],
        "high",
        "done",
        None,
    )
    assert [r["id"] for r in client.get(f"{API}/projects/demo/tasks?type=bug").json()["tasks"]] == [bug_id]
    assert [r["id"] for r in client.get(f"{API}/projects/demo/tasks?type=task").json()["tasks"]] == [task["id"]]
    assert client.get(f"{API}/projects/demo/tasks?type=idea").status_code in (400, 422)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert client.get(f"{API}/projects/demo/bugs").json()["bugs"][0]["status"] == "fixed"


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "idea"},
        {"type": ""},
        {"priority": "urgent"},
        {"priority": ""},
        {"related_initiative": "in-missing"},
        {"title": " "},
        {"description": " "},
    ],
)
def test_create_refuses_invalid_fields_without_writing(project, extra):
    response = _client().post(f"{API}/projects/demo/tasks", json={"title": "Export", "description": "CSV", **extra})
    assert response.status_code in (400, 422), response.text
    assert not list((project / "tasks").glob("*.md"))


def test_canonical_bug_defaults_symptom_and_preserves_details_and_similarity(project):
    client = _client()
    fields = {
        "type": "bug",
        "title": "Login loops",
        "description": "OTP reloads",
        "priority": "high",
        "expected": "Login",
        "repro": "Submit OTP",
        "where": "/login",
        "evidence": ["logs.txt"],
        "body": "Detailed notes",
    }
    first = client.post(f"{API}/projects/demo/tasks", json=fields).json()["task"]
    assert first["symptom"] == "OTP reloads" and first["severity"] == "high"
    assert first["expected"] == "Login" and first["repro"] == "Submit OTP" and first["where"] == "/login"
    second = client.post(f"{API}/projects/demo/tasks", json={**fields, "symptom": "OTP keeps refreshing"}).json()["task"]
    assert second["id"] == first["id"] + "-2"
    assert second["similar_to"] == [first["id"]] and second["symptom"] == "OTP keeps refreshing"
    assert load(project).tasks[first["id"]].frontmatter["type"] == "bug"
    assert validate(load(project), check_sources=False).ok


@pytest.mark.parametrize("legacy", [False, True])
def test_patch_maps_status_and_preserves_unknown_fields_and_body(project, legacy):
    from hermes_initiatives_core.docs import dump_markdown, parse_markdown

    fm = (
        writers.report_bug(project, BUG, today=DAY)
        if legacy
        else writers.report_task(project, {"title": "Export", "description": "CSV"}, today=DAY)
    )
    path = project / ("bugs" if legacy else "tasks") / f"{fm['id']}.md"
    fm["extension"] = {"nested": ["keep", {"unknown": True}]}
    path.write_text(dump_markdown(fm, "Body remains.\n\nSecond paragraph.\n"))
    body = parse_markdown(path.read_text(), path).body
    client = _client()
    url = f"{API}/projects/demo/tasks/{fm['id']}"
    for invalid in [
        {},
        {"status": "fixing"},
        {"status": ""},
        {"status": None},
        {"status": "cancelled"},
        {"status": "done"},
        {"status": "in_progress"},
    ]:
        assert client.patch(url, json=invalid).status_code in (400, 422)
    result = client.patch(url, json={"status": "cancelled", "reason": "Not needed"})
    assert result.status_code == 200, result.text
    assert result.json()["task"]["status"] == "cancelled"
    after = parse_markdown(path.read_text(), path)
    assert after.frontmatter["status"] == ("wontfix" if legacy else "cancelled")
    assert after.frontmatter["extension"] == fm["extension"] and after.body == body
    assert client.patch(url, json={"status": "done", "reason": "Delivered elsewhere"}).json()["task"]["status"] == "done"
    assert client.patch(f"{API}/projects/demo/tasks/task-20261018-nope", json={"status": "triaged"}).status_code == 404


@pytest.mark.parametrize("kind", ["task", "bug", "legacy"])
def test_task_card_links_once_with_type_specific_instructions(project, kind):
    from test_bugs import _tasks

    client = _client()
    fm = (
        writers.report_bug(project, BUG)
        if kind == "legacy"
        else writers.report_task(project, {"type": kind, "title": "Export", "description": "CSV download", "repro": "Open chart"})
    )
    url = f"{API}/projects/demo/tasks/{fm['id']}/card"
    made = client.post(url, json={"note": "Include archived sales"})
    assert made.status_code == 200, made.text
    payload = made.json()
    card = _tasks()[payload["card"]]
    assert payload["task"]["status"] == "in_progress" and payload["task"]["card_id"] == card.id
    assert card.assignee == "planner" and "Include archived sales" in card.body and fm["id"] in card.body
    assert ("root cause" in card.body) == (kind != "task")
    assert ("regression test" in card.body) == (kind != "task")
    before = set(_tasks())
    assert client.post(url, json={}).status_code == 409
    assert set(_tasks()) == before
    if kind == "legacy":
        assert client.post(f"{API}/projects/demo/bugs/{fm['id']}/fix-card", json={}).status_code == 409
        assert load(project).bugs[fm["id"]].frontmatter["status"] == "fixing"
    assert client.patch(f"{API}/projects/demo/tasks/{fm['id']}", json={"status": "done"}).status_code == 200


def test_task_card_failure_keeps_note_unchanged(project, monkeypatch):
    import subprocess

    from hermes_initiatives_core import review_exec

    fm = writers.report_task(project, {"title": "Export", "description": "CSV"})
    path = project / "tasks" / f"{fm['id']}.md"
    before = path.read_bytes()
    monkeypatch.setattr(
        review_exec, "default_runner", lambda args, stdin=None: subprocess.CompletedProcess(args, 1, "", "board locked")
    )
    response = _client().post(f"{API}/projects/demo/tasks/{fm['id']}/card", json={})
    assert response.status_code == 502 and "board locked" in response.json()["detail"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("legacy", [False, True])
def test_task_context_and_launch_preserve_source(project, legacy):
    import copy

    fm = (
        writers.report_bug(project, BUG)
        if legacy
        else writers.report_task(project, {"type": "bug", "title": "Login", "description": "Reloads", "symptom": "OTP reloads"})
    )
    doc = (load(project).bugs if legacy else load(project).tasks)[fm["id"]]
    cached = copy.deepcopy(doc.frontmatter)
    before, mtime = doc.path.read_bytes(), doc.path.stat().st_mtime_ns
    client = _client()
    response = client.get(f"{API}/projects/demo/tasks/{fm['id']}/session-context")
    assert response.status_code == 200, response.text
    context = json.loads(response.json()["context"])
    assert context["task"]["id"] == fm["id"] and context["task"]["symptom"] == fm["symptom"]
    assert doc.frontmatter == cached and doc.path.read_bytes() == before and doc.path.stat().st_mtime_ns == mtime
    base = {"project": "demo", "connection_id": "local", "profile": "default", "target_profile": "x", "kind": "task"}
    result = client.post(f"{API}/launches", json={**base, "subject": fm["id"]})
    assert result.status_code == 200, result.text
    assert result.json()["subject"] == fm["id"] and result.json()["title"].startswith(f"Task: {fm['id']}")
    assert [r["id"] for r in client.get(f"{API}/projects/demo/sessions?kind=task").json()["launches"]] == [result.json()["id"]]
    assert client.post(f"{API}/launches", json=base).status_code == 400
    assert client.post(f"{API}/launches", json={**base, "subject": "task-20261018-absent"}).status_code == 404
    assert client.get(f"{API}/projects/demo/tasks/task-20261018-absent/session-context").status_code == 404


@pytest.mark.parametrize("first_alias", ["task", "bug"])
def test_partial_card_success_retries_across_legacy_aliases(project, monkeypatch, first_alias):
    from test_bugs import _tasks

    fm = writers.report_bug(project, BUG)
    client = _client()
    task_url = f"{API}/projects/demo/tasks/{fm['id']}/card"
    bug_url = f"{API}/projects/demo/bugs/{fm['id']}/fix-card"
    first, second = (task_url, bug_url) if first_alias == "task" else (bug_url, task_url)
    function = "update_task" if first_alias == "task" else "update_bug"
    original = getattr(writers, function)

    def fail_link(*args, **kwargs):
        raise writers.WriteRefused("simulated failed link")

    monkeypatch.setattr(writers, function, fail_link)
    before = set(_tasks())
    response = client.post(first, json={})
    assert response.status_code == 500, response.text
    created = set(_tasks()) - before
    assert len(created) == 1
    monkeypatch.setattr(writers, function, original)
    retry = client.post(second, json={})
    assert retry.status_code == 200, retry.text
    assert retry.json()["card"] == next(iter(created)) and set(_tasks()) - before == created


@pytest.mark.parametrize("kind", ["task", "bug"])
def test_report_task_script_creates_canonical_note(project, kind):
    import subprocess
    import sys

    from conftest import ROOT

    response = subprocess.run(
        [
            sys.executable,
            str(ROOT / "skills/initiatives/scripts/report_task.py"),
            "--root",
            str(project),
            "--type",
            kind,
            "--title",
            "Export report",
            "--description",
            "CSV download",
            "--priority",
            "high",
        ],
        capture_output=True,
        text=True,
    )
    assert response.returncode == 0, response.stderr
    fm = json.loads(response.stdout)["task"]
    assert fm["type"] == kind and fm["description"] == "CSV download" and fm["priority"] == "high"
    assert fm["id"] in load(project).tasks


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"status": "in_progress"}, "card_id"),
        ({"status": "cancelled"}, "reason"),
        ({"status": "done"}, "card_id or reason"),
        ({"type": "idea"}, "type"),
        ({"priority": "urgent"}, "priority"),
        ({"type": "bug", "symptom": ""}, "symptom"),
        ({"description": ""}, "description"),
    ],
)
def test_invalid_hand_edited_tasks_are_visible_with_validation_errors(project, changes, expected):
    from hermes_initiatives_core.docs import dump_markdown

    fm = writers.report_task(project, {"title": "Export", "description": "CSV"})
    fm.update(changes)
    (project / "tasks" / f"{fm['id']}.md").write_text(dump_markdown(fm, "Notes"))
    report = validate(load(project), check_sources=False)
    assert any(expected in error for error in report.errors[fm["id"]])
    response = _client().get(f"{API}/projects/demo/tasks")
    assert response.status_code == 200, response.text
    assert response.json()["tasks"][0]["errors"] == report.errors[fm["id"]]


@pytest.mark.parametrize("legacy", [False, True])
def test_ambiguous_task_ids_cannot_be_updated_or_carded(project, legacy):
    fm = writers.report_bug(project, BUG) if legacy else writers.report_task(project, {"title": "Export", "description": "CSV"})
    folder = project / ("bugs" if legacy else "tasks")
    source = folder / f"{fm['id']}.md"
    duplicate = folder / "duplicate.md"
    duplicate.write_bytes(source.read_bytes())
    before = {p: p.read_bytes() for p in (source, duplicate)}
    client = _client()
    assert client.patch(f"{API}/projects/demo/tasks/{fm['id']}", json={"status": "triaged"}).status_code == 409
    assert client.post(f"{API}/projects/demo/tasks/{fm['id']}/card", json={}).status_code == 409
    with pytest.raises(writers.WriteRefused, match="ambiguous"):
        writers.update_task(project, fm["id"], status="triaged")
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("directory_link", [False, True])
def test_task_loader_and_writer_refuse_symlink_escape(project, tmp_path, directory_link):
    from hermes_initiatives_core.docs import dump_markdown

    outside = tmp_path / "external"
    outside.mkdir()
    task_id = "task-20261018-external"
    note = outside / f"{task_id}.md"
    note.write_text(
        dump_markdown(
            {
                "id": task_id,
                "type": "task",
                "title": "Outside",
                "description": "Do not touch",
                "status": "open",
                "priority": "medium",
            },
            "External",
        )
    )
    if directory_link:
        (project / "tasks").symlink_to(outside, target_is_directory=True)
    else:
        (project / "tasks").mkdir()
        (project / "tasks" / note.name).symlink_to(note)
    before = note.read_bytes()
    assert task_id not in load(project).tasks
    with pytest.raises(writers.WriteRefused):
        writers.update_task(project, task_id, status="triaged")
    if directory_link:
        with pytest.raises(writers.WriteRefused, match="tasks directory"):
            writers.report_task(project, {"title": "No escape", "description": "Never outside"})
        assert sorted(p.name for p in outside.iterdir()) == [note.name]
    assert note.read_bytes() == before


def test_bug_task_whitespace_symptom_defaults_to_description(project):
    response = _client().post(
        f"{API}/projects/demo/tasks",
        json={"type": "bug", "title": "Login loops", "description": "OTP reloads", "symptom": "   "},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["symptom"] == "OTP reloads"
    assert validate(load(project), check_sources=False).ok


def test_hand_written_task_defaults_omitted_fields_without_mutating_cache(project):
    import copy

    from hermes_initiatives_core.docs import dump_markdown

    fm = {"id": "task-20261018-minimal", "title": "Export", "description": "CSV"}
    path = project / "tasks" / f"{fm['id']}.md"
    path.parent.mkdir()
    path.write_text(dump_markdown(fm, "Notes"))
    doc = load(project).tasks[fm["id"]]
    cached = copy.deepcopy(doc.frontmatter)
    response = _client().get(f"{API}/projects/demo/tasks")
    row = response.json()["tasks"][0]
    assert (row["type"], row["status"], row["priority"]) == ("task", "open", "medium")
    assert {"card_id", "reason", "created", "source", "related_initiative", "evidence", "similar_to"} <= row.keys()
    assert doc.frontmatter == cached


def test_create_and_list_task(project):
    client = _client()
    response = client.post(f"{API}/projects/demo/tasks", json={"title": "Export sales", "description": "Download sales as CSV"})
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["type"] == "task" and task["status"] == "open" and task["priority"] == "medium"
    assert task["description"] == "Download sales as CSV"
    assert task["id"].startswith("task-")
    assert (project / "tasks" / f"{task['id']}.md").is_file()
    data = client.get(f"{API}/projects/demo/tasks").json()
    assert [row["id"] for row in data["tasks"]] == [task["id"]]
    assert data["statuses"] == ["open", "triaged", "in_progress", "done", "cancelled"]
    assert data["open_statuses"] == ["open", "triaged", "in_progress"]
    assert data["types"] == ["task", "bug"]
    assert {
        "card_id",
        "reason",
        "created",
        "source",
        "related_initiative",
        "evidence",
        "body",
        "path",
        "errors",
        "warnings",
        "similar_to",
        "card",
    } <= data["tasks"][0].keys()


def test_partial_note_without_a_title_serves_context_and_launch(project):
    from conftest import write

    task_id = "task-20261018-partial-note"
    write(project / "tasks" / f"{task_id}.md", f"""---
id: {task_id}
type: task
description: Only a description
status: open
---
""")
    client = _client()
    response = client.get(f"{API}/projects/demo/tasks/{task_id}/session-context")
    assert response.status_code == 200, response.text
    assert json.loads(response.json()["context"])["task"]["title"] == task_id
    base = {"project": "demo", "connection_id": "local", "profile": "default", "target_profile": "x"}
    launch = client.post(f"{API}/launches", json={**base, "kind": "task", "subject": task_id})
    assert launch.status_code == 200, launch.text
    assert launch.json()["title"].startswith(f"Task: {task_id}")


def test_new_bug_names_a_similar_legacy_bug(project):
    legacy = writers.report_bug(project, BUG, today=DAY)["id"]
    task = writers.report_task(
        project, {"type": "bug", "title": BUG["title"], "description": BUG["symptom"]}, today=DAY
    )
    assert task["similar_to"] == [legacy]


def test_long_title_still_writes_a_valid_filename(project):
    task = writers.report_task(project, {"title": "a" * 300, "description": "Long token"}, today=DAY)
    assert len(task["id"]) < 100
    assert (project / "tasks" / f"{task['id']}.md").is_file()


def test_reopen_clears_the_old_reason_and_completion_needs_a_new_one(project):
    task = writers.report_task(project, {"title": "Cancel then reopen", "description": "Work"}, today=DAY)
    writers.update_task(project, task["id"], status="cancelled", reason="Duplicate of another task")
    reopened = writers.update_task(project, task["id"], status="open")
    assert reopened["reason"] is None
    with pytest.raises(writers.WriteRefused, match="done needs"):
        writers.update_task(project, task["id"], status="done")
