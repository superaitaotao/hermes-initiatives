"""Bugs: filed by report_bug.py, moved by the user in the Tasks tab, fixed through one linked Kanban card.

The fix-card route runs the real ``hermes kanban`` CLI against the isolated test board (conftest pins
HERMES_HOME and HERMES_KANBAN_HOME under tmp_path).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shlex
import subprocess
import sys

import pytest
from conftest import ROOT, write
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_initiatives_core import writers
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

API = "/api/plugins/initiatives"
DAY = dt.date(2026, 10, 18)
BUG = {"title": "Chart drops the latest sale", "symptom": "The Sep 1 sale is not plotted", "severity": "high"}


def _client():
    spec = importlib.util.spec_from_file_location("initiatives_plugin_api_bugs", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=API)
    return TestClient(app)


@pytest.fixture
def project(root, tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "hermes"
    write(cli, f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m hermes_cli.main "$@"\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    kbc.init_db(board="default")
    settings = {
        "projects": [{"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default", "intake_assignee": "planner"}]
    }
    (tmp_path / "plugin-data").mkdir(exist_ok=True)
    (tmp_path / "plugin-data" / "settings.json").write_text(json.dumps(settings))
    return root


def _tasks():
    conn = kbc.connect(board="default")
    try:
        return {t.id: t for t in kb.list_tasks(conn)}
    finally:
        conn.close()


def test_report_writes_an_open_bug_that_validates(root):
    bug = writers.report_bug(root, {**BUG, "related_initiative": "in-demo"}, today=DAY)
    assert bug["id"] == "bug-20261018-chart-drops-the-latest-sale" and bug["status"] == "open"
    assert (root / "bugs" / f"{bug['id']}.md").is_file()
    snapshot = load(root)
    assert list(snapshot.bugs) == [bug["id"]] and bug["id"] not in snapshot.other_folders
    assert validate(snapshot, check_sources=False).ok
    again = writers.report_bug(root, BUG, today=DAY)
    assert again["id"] == f"{bug['id']}-2", "same title on the same day gets a new file, never overwrites"
    assert again["similar_to"] == [bug["id"]], "an open bug with the same symptom is named as a likely duplicate"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"symptom": "x"}, "title"),
        ({"title": "x"}, "symptom"),
        ({**BUG, "severity": "urgent"}, "severity"),
        ({**BUG, "related_initiative": "in-nope"}, "related_initiative"),
    ],
)
def test_report_refuses_an_incomplete_bug(root, fields, message):
    with pytest.raises(writers.WriteRefused, match=message):
        writers.report_bug(root, fields, today=DAY)
    assert not (root / "bugs").exists() or not list((root / "bugs").glob("*.md"))


def test_status_needs_its_field(root):
    bug_id = writers.report_bug(root, BUG, today=DAY)["id"]
    with pytest.raises(writers.WriteRefused, match="fix card"):
        writers.update_bug(root, bug_id, status="fixing")
    with pytest.raises(writers.WriteRefused, match="reason"):
        writers.update_bug(root, bug_id, status="wontfix")
    after = writers.update_bug(root, bug_id, status="wontfix", reason="Works as designed")
    assert after["status"] == "wontfix" and after["reason"] == "Works as designed"
    assert validate(load(root), check_sources=False).ok


def test_hand_edited_bug_with_a_bad_status_fails_validation(root):
    bug_id = writers.report_bug(root, BUG, today=DAY)["id"]
    path = root / "bugs" / f"{bug_id}.md"
    path.write_text(path.read_text().replace("status: open", "status: fixing"))
    errors = validate(load(root), check_sources=False).errors[bug_id]
    assert errors == ["status 'fixing' needs fix_card"]


def test_list_puts_open_bugs_first_by_severity(project):
    low = writers.report_bug(project, {**BUG, "title": "Low one", "severity": "low"}, today=DAY)["id"]
    high = writers.report_bug(project, {**BUG, "title": "High one"}, today=DAY)["id"]
    done = writers.report_bug(project, {**BUG, "title": "Done one"}, today=DAY)["id"]
    writers.update_bug(project, done, status="fixed", reason="Fixed in 1a2b3c")
    data = _client().get(f"{API}/projects/demo/bugs").json()
    assert [b["id"] for b in data["bugs"]] == [high, low, done]
    assert data["open_statuses"] == ["open", "triaged", "fixing"]


def test_fix_card_is_created_once_linked_and_moves_the_bug_to_fixing(project):
    bug_id = writers.report_bug(project, {**BUG, "repro": "Open the chart"}, today=DAY)["id"]
    client = _client()
    before = set(_tasks())
    made = client.post(f"{API}/projects/demo/bugs/{bug_id}/fix-card", json={"note": "Check the 24-month cutoff"})
    assert made.status_code == 200, made.text
    card = made.json()["card"]
    assert set(_tasks()) - before == {card}
    task = _tasks()[card]
    assert task.assignee == "planner" and task.title == "Fix: Chart drops the latest sale"
    assert bug_id in task.body and "Open the chart" in task.body and "Check the 24-month cutoff" in task.body
    listed = next(b for b in client.get(f"{API}/projects/demo/bugs").json()["bugs"] if b["id"] == bug_id)
    assert listed["status"] == "fixing" and listed["fix_card"] == card and listed["card"]["status"] == task.status
    again = client.post(f"{API}/projects/demo/bugs/{bug_id}/fix-card", json={})
    assert again.status_code == 409 and set(_tasks()) - before == {card}


def test_fix_card_failure_leaves_the_bug_open(project, monkeypatch):
    bug_id = writers.report_bug(project, BUG, today=DAY)["id"]
    client = _client()
    import hermes_initiatives_core.review_exec as review_exec

    monkeypatch.setattr(
        review_exec, "default_runner", lambda args, stdin=None: subprocess.CompletedProcess(args, 1, "", "board locked")
    )
    failed = client.post(f"{API}/projects/demo/bugs/{bug_id}/fix-card", json={})
    assert failed.status_code == 502 and "board locked" in failed.json()["detail"]
    fm = load(project).bugs[bug_id].frontmatter
    assert fm["status"] == "open" and fm["fix_card"] is None


def test_patch_and_routes_refuse_bad_input(project):
    bug_id = writers.report_bug(project, BUG, today=DAY)["id"]
    client = _client()
    assert client.patch(f"{API}/projects/demo/bugs/{bug_id}", json={"status": "wontfix"}).status_code == 400
    ok = client.patch(f"{API}/projects/demo/bugs/{bug_id}", json={"status": "triaged"})
    assert ok.status_code == 200 and ok.json()["bug"]["status"] == "triaged"
    assert client.patch(f"{API}/projects/demo/bugs/bug-20261018-nope", json={"status": "triaged"}).status_code == 404
    assert client.patch(f"{API}/projects/demo/bugs/..%2Fx", json={"status": "triaged"}).status_code in (400, 404)
    assert client.get(f"{API}/projects/demo/bugs/bug-20261018-nope/session-context").status_code == 404


def test_bug_session_gets_that_bug_as_context(project):
    bug_id = writers.report_bug(project, BUG, today=DAY)["id"]
    client = _client()
    ctx = client.get(f"{API}/projects/demo/bugs/{bug_id}/session-context").json()
    assert json.loads(ctx["context"])["bug"]["symptom"] == BUG["symptom"]
    base = {"project": "demo", "connection_id": "local", "profile": "default", "target_profile": "x"}
    launch = client.post(f"{API}/launches", json={**base, "kind": "bug", "subject": bug_id}).json()
    assert launch["subject"] == bug_id and launch["title"].startswith(f"Bug: {bug_id} Chart drops")
    assert [row["id"] for row in client.get(f"{API}/projects/demo/sessions?kind=bug").json()["launches"]] == [launch["id"]]
    assert client.post(f"{API}/launches", json={**base, "kind": "bug"}).status_code == 400
    assert client.post(f"{API}/launches", json={**base, "kind": "bug", "subject": "bug-20261018-nope"}).status_code == 404


def test_report_bug_script_files_a_bug(project):
    script = ROOT / "skills" / "initiatives" / "scripts" / "report_bug.py"
    out = subprocess.run(
        [sys.executable, str(script), "--root", str(project), "--title", "Login loops", "--symptom", "OTP page reloads"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    bug = json.loads(out.stdout)["bug"]
    assert bug["status"] == "open" and (project / "bugs" / f"{bug['id']}.md").is_file()
