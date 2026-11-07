"""Run the actual bundled gates on disposable Hermes boards; no live cron jobs."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def board_env(isolated_hermes, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kbc.init_db(board="default")
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({
        "digest_at": "23:59", "push_start_hour": 0, "push_end_hour": 24,
        "projects": [{"id": "demo", "label": "Demo", "root": str(tmp_path / "initiatives"),
                      "kanban_board": "default", "worker_profiles": ["coder", "reviewer"],
                      "policy_skill": "demo-policy", "merge_reviewer": "reviewer"}]}))
    (isolated_hermes / "cron").mkdir(exist_ok=True)
    (isolated_hermes / "cron/jobs.json").write_text(json.dumps({"jobs": [
        {"id": "recovery", "name": "initiatives:demo:recovery", "last_status": "ok"},
        {"id": "review", "name": "initiatives:demo:review", "last_status": "ok"}]}))
    # A digest sent today makes quiet-board tests independent of the wall clock.
    from hermes_initiatives_core.reviews import ReviewStore

    store = ReviewStore()
    from hermes_initiatives_core.settings import load_settings

    date = dt.datetime.now(load_settings()[0].zone()).date().isoformat()
    review = store.create_review(trigger="cron", model=None, window_start=None)
    store.update_review(review["id"], telegram_json='[{"part": 1}]')
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}, kb, kbc, store, date


def run_gate(env, name, *args):
    result = subprocess.run([sys.executable, str(ROOT / "autonomy" / name), "--project", "demo", *args],
                            env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "error, waking" not in result.stdout.lower(), result.stdout + result.stderr
    assert "gate list failed" not in result.stdout.lower(), result.stdout + result.stderr
    return result.stdout


def block(kb, kbc):
    with kbc.connect(board="default") as conn:
        card = kb.create_task(conn, title="Needs a human decision", assignee="coder")
        kb.block_task(conn, card, kind="needs_input", reason="Please approve the external cutover")
    return card


def test_recovery_quiet_then_new_block(board_env):
    env, kb, kbc, _, _ = board_env
    args = ["--job", "name:initiatives:demo:recovery", "--no-review-queue", "--recovery-signals"]
    assert "first run" in run_gate(env, "kanban_recovery_wake_gate.py", *args)
    assert json.loads(run_gate(env, "kanban_recovery_wake_gate.py", *args).splitlines()[-1]) == {"wakeAgent": False}
    card = block(kb, kbc)
    assert card in run_gate(env, "kanban_recovery_wake_gate.py", *args)


def test_human_list_contains_new_block(board_env, tmp_path):
    env, kb, kbc, _, _ = board_env
    path = tmp_path / "gates.json"
    args = ["--gates-json", str(path)]
    run_gate(env, "kanban_human_gates_watch.py", *args)
    assert json.loads(path.read_text())["cards"] == []
    card = block(kb, kbc)
    run_gate(env, "kanban_human_gates_watch.py", *args)
    assert card in {c["id"] for c in json.loads(path.read_text())["cards"]}


def test_review_quiet_then_new_block(board_env, tmp_path):
    env, kb, kbc, store, date = board_env
    # The digest is already sent: due-ness is a real source of wakes, not a test stub.
    args = ["--job", "review", "--gates-json", str(tmp_path / "gates.json")]
    first = run_gate(env, "initiatives_review_wake_gate.py", *args)
    assert "first run" in first
    second = run_gate(env, "initiatives_review_wake_gate.py", *args)
    assert second.strip() == '{"wakeAgent": false}', second
    block(kb, kbc)
    assert "human gate list changed" in run_gate(env, "initiatives_review_wake_gate.py", *args)


def test_merge_filer_files_exact_head_once(board_env):
    env, kb, kbc, store, _ = board_env
    sha = "a" * 40
    with kbc.connect(board="default") as conn:
        card = kb.create_task(conn, title="Reviewed deep change", assignee="reviewer")
        kb.add_comment(conn, card, author="reviewer", body=(
            "REVIEW PASSED — DEEP: NEEDS YOUR MERGE APPROVAL\n"
            f"**You:** approve merging PR #42 at {sha}\n"
            "**Next:** reviewer merges the approved head\n"))
        kb.block_task(conn, card, kind="needs_input", reason=f"Deep-tier merge approval: PR #42 at {sha}")
    first = run_gate(env, "deep_merge_gate_filer.py", "--no-notify")
    assert json.loads(first)["filed"][0]["sha"] == sha
    assert run_gate(env, "deep_merge_gate_filer.py", "--no-notify") == ""
    item = store.get_by_key(f"kanban:default:{card}:gate")
    assert sha in item["version"]["question"]
    assert item["version"]["options"][0]["action"] == "kanban_unblock"


def test_failed_gate_config_removes_stale_list(board_env, tmp_path):
    env, _, _, _, _ = board_env
    gates = tmp_path / "gates.json"
    gates.write_text('{"cards": [{"id": "stale"}]}')
    result = subprocess.run([sys.executable, str(ROOT / "autonomy/kanban_human_gates_watch.py"),
                             "--project", "unknown", "--gates-json", str(gates)],
                            env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode != 0
    assert not gates.exists()


def test_merge_filer_quiet_without_matching_gate(board_env):
    env, kb, kbc, _, _ = board_env
    assert run_gate(env, "deep_merge_gate_filer.py", "--no-notify") == ""
    block(kb, kbc)
    assert run_gate(env, "deep_merge_gate_filer.py", "--no-notify") == ""


def test_merge_filer_dry_run_creates_no_store(isolated_hermes, tmp_path):
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({"projects": [
        {"id": "demo", "label": "Demo", "root": str(tmp_path), "kanban_board": "default", "merge_reviewer": "reviewer"}]}))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run([sys.executable, str(ROOT / "autonomy/deep_merge_gate_filer.py"), "--dry-run", "--project", "demo"],
                            env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in data.iterdir()) == ["settings.json"]


def test_merge_filer_dry_run_leaves_existing_store_bytes(board_env):
    env, kb, kbc, store, _ = board_env
    sha = "b" * 40
    with kbc.connect(board="default") as conn:
        card = kb.create_task(conn, title="Reviewed deep change", assignee="reviewer")
        kb.add_comment(conn, card, author="reviewer", body=(
            "REVIEW PASSED — DEEP: NEEDS YOUR MERGE APPROVAL\n"
            f"**You:** approve merging PR #7 at {sha}\n"))
        kb.block_task(conn, card, kind="needs_input", reason=f"Deep-tier merge approval: PR #7 at {sha}")
    before = store.path.read_bytes()
    out = run_gate(env, "deep_merge_gate_filer.py", "--dry-run", "--no-notify")
    assert json.loads(out)["dry_run"] is True
    assert store.path.read_bytes() == before and store.get_by_key(f"kanban:default:{card}:gate") is None


@pytest.mark.parametrize("name", ["../../../escaped", "a/b", ".hidden", ""])
def test_recovery_state_name_cannot_leave_the_gates_dir(board_env, name):
    env, *_ = board_env
    result = subprocess.run([sys.executable, str(ROOT / "autonomy/kanban_recovery_wake_gate.py"), "--project", "demo",
                             "--job", "name:initiatives:demo:recovery", "--no-github", "--state-name", name],
                            env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode != 0 and "--state-name" in result.stderr
    assert not list(Path(os.environ["HERMES_HOME"]).parent.rglob("escaped.json"))
