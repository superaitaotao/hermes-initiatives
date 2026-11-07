"""Exercise install ordering, cron readback, registration and protective failure paths."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from hermes_initiatives_core.autonomy_setup import build_plan, execute_plan, read_jobs
from hermes_initiatives_core.settings import Settings


def config(tmp_path):
    return Settings.model_validate({"review_target": "telegram:1:2", "projects": [
        {"id": "demo", "label": "Demo", "root": str(tmp_path), "kanban_board": "default",
         "merge_reviewer": "reviewer", "worker_profiles": ["coder"], "policy_skill": "demo-policy"}]})


def test_install_creates_paused_reads_id_then_edits_resumes(tmp_path, capsys):
    home = tmp_path / "profile"
    plans = build_plan(config(tmp_path), "demo", "default", home=home)
    calls = []

    def runner(command, run_home):
        assert run_home == home
        calls.append(command)
        verb = command[4]
        if verb == "create":
            assert "--paused" in command and "--script" in command
            script = command[command.index("--script") + 1]
            assert (home / "scripts" / script).is_file()
            jobs = read_jobs(home)
            jid = f"job{len(jobs)}"
            jobs.append({"id": jid, "name": command[command.index("--name") + 1], "enabled": False, "script": script})
            (home / "cron").mkdir(exist_ok=True)
            (home / "cron/jobs.json").write_text(json.dumps({"jobs": jobs}))
            return subprocess.CompletedProcess(command, 0, f"Created job: {jid}\n", "")
        assert verb in ("edit", "resume")
        return subprocess.CompletedProcess(command, 0, "", "")

    execute_plan(plans, runner=runner)
    assert [c[4] for c in calls] == ["create", "edit", "resume"] * 3
    assert len(read_jobs(home)) == 3
    execute_plan(build_plan(config(tmp_path), "demo", "default", home=home), runner=runner)
    assert len(calls) == 9
    assert "EXISTS" in capsys.readouterr().out


def test_real_hermes_cli_installs_idempotently_in_isolated_home(isolated_hermes, tmp_path):
    """The real CLI may write only the disposable profile selected by the fixture."""
    import os
    import sys

    from hermes_cli import kanban_db_connect as kbc

    kbc.init_db(board="default")
    root = Path(__file__).resolve().parents[1]
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(config(tmp_path).model_dump_json())
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), *[p for p in sys.path if p]])}
    cmd = [sys.executable, "-m", "hermes_initiatives_core.cli", "setup", "--profile", "default", "--project", "demo"]
    first = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stdout + first.stderr
    assert len(read_jobs(isolated_hermes)) == 3
    for job in read_jobs(isolated_hermes):
        assert job["enabled"] is True
        assert (isolated_hermes / "scripts" / job["script"]).is_file()
    second = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(read_jobs(isolated_hermes)) == 3
    assert second.stdout.count("EXISTS") == 3
    # Exercise the actual shell wrapper without inherited PYTHONPATH.
    wrapped = subprocess.run(["bash", str(isolated_hermes / "scripts/initiatives-demo-recovery.sh")],
                             env=env, capture_output=True, text=True, timeout=120)
    assert wrapped.returncode == 0 and "first run of the gate" in wrapped.stdout, wrapped.stdout + wrapped.stderr
    quiet = subprocess.run(["bash", str(isolated_hermes / "scripts/initiatives-demo-recovery.sh")],
                           env=env, capture_output=True, text=True, timeout=120)
    assert quiet.stdout.strip() == '{"wakeAgent": false}', quiet.stdout + quiet.stderr


def test_failed_install_does_not_resume(tmp_path):
    plans = build_plan(config(tmp_path), "demo", "default", home=tmp_path / "profile")
    calls = []

    def runner(command, home):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, "injected failure")

    with pytest.raises(subprocess.CalledProcessError):
        execute_plan(plans, runner=runner)
    assert len(calls) == 1 and "create" in calls[0]


def test_symlink_wrapper_rejected(tmp_path):
    home = tmp_path / "profile"
    (home / "scripts").mkdir(parents=True)
    plan = build_plan(config(tmp_path), "demo", "default", home=home)[0]
    outside = tmp_path / "outside.sh"
    outside.write_text("unchanged")
    plan.wrapper_path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        execute_plan([plan])
    assert outside.read_text() == "unchanged"


def test_duplicate_name_refused(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        build_plan(config(tmp_path), "demo", "test", home=tmp_path,
                   existing=[{"name": "initiatives:demo:review", "id": i} for i in ("a", "b")])


def test_plugin_registers_cli_and_individual_skill_descriptions():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("autonomy_plugin_test", root / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Context:
        def __init__(self):
            self.skills = {}
            self.cli = None

        def register_skill(self, name, path, description):
            self.skills[name] = description

        def register_cli_command(self, *args):
            self.cli = args

        def register_hook(self, *args):
            pass

        def register_platform_handler(self, *args):
            pass

    ctx = Context()
    module.register(ctx)
    assert ctx.cli[0] == "initiatives"
    assert ctx.skills["kanban-human-gates"] != ctx.skills["initiatives"]
    assert "human" in ctx.skills["kanban-human-gates"].lower()
