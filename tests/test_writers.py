"""Agent write paths: concurrency, caps, identity, and the append-only log."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import DECIDED, MEASUREMENT, ROOT, cycle_md, write

from hermes_initiatives_core import store, writers
from hermes_initiatives_core.validate import validate


def _propose(args):
    root, n = args
    sys.path.insert(0, str(ROOT))
    from hermes_initiatives_core import writers as w

    return w.propose_idea(Path(root), {"title": "Same title", "evidence": ["x.md"], "source": f"p{n}/run"})["id"]


def test_parallel_proposals_get_unique_ids_and_files(root):
    # Regression: two agents allocating the same idea id overwrite each other.
    with mp.get_context("fork").Pool(4) as pool:
        ids = pool.map(_propose, [(str(root), n) for n in range(8)])
    assert len(set(ids)) == 8
    assert len(list((root / "ideas").glob("*.md"))) == 8
    assert validate(store.load(root), check_sources=False).ok


def test_open_idea_cap_per_source(root):
    for n in range(writers.MAX_OPEN_IDEAS_PER_SOURCE):
        writers.propose_idea(root, {"title": f"Idea {n} distinct words {n * 7}", "evidence": ["e.md"], "source": "seo/cron:1"})
    with pytest.raises(writers.WriteRefused, match="open ideas"):
        writers.propose_idea(root, {"title": "One more", "evidence": ["e.md"], "source": "seo/cron:2"})


def test_similar_ideas_are_suggested_not_blocked(root):
    first = writers.propose_idea(root, {"title": "Improve CTR for calculator pages", "evidence": ["e.md"]})
    second = writers.propose_idea(root, {"title": "Improve CTR on calculator pages", "evidence": ["e.md"]})
    assert second["similar_to"] == [first["id"]]


def test_idea_without_evidence_is_refused(root):
    with pytest.raises(writers.WriteRefused, match="evidence"):
        writers.propose_idea(root, {"title": "No evidence", "evidence": []})


def test_observation_identity_comes_from_environment(root, monkeypatch):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01"))
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "any-profile")
    monkeypatch.setenv("HERMES_SESSION_ID", "20261010_1")
    row = writers.record_observation(root, "in-demo-c01", {"kind": "note", "detail": "checked"})
    assert row["by"] == "any-profile" and row["run"] == "session:20261010_1"
    log = (root / "in-demo" / "observations" / "in-demo-c01.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["detail"] == "checked"


def test_observation_refuses_unknown_supersedes_and_missing_evidence(root):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01"))
    with pytest.raises(writers.WriteRefused, match="supersedes"):
        writers.record_observation(root, "in-demo-c01", {"kind": "note", "supersedes": "2020-01-01T00:00:00Z"})
    with pytest.raises(writers.WriteRefused, match="evidence"):
        writers.record_observation(root, "in-demo-c01", {"kind": "release_verified"})


def test_skill_script_runs_under_bare_python(root, tmp_path):
    # Regression: agent terminals run a python3 without pyyaml; the script must re-exec under Hermes.
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01"))
    script = ROOT / "skills" / "initiatives" / "scripts" / "record_observation.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_PYTHON"] = sys.executable
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-S",
            str(script),
            "--root",
            str(root),
            "--cycle",
            "in-demo-c01",
            "--kind",
            "note",
            "--detail",
            "from script",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["observation"]["detail"] == "from script"


def _measuring(root, measurement):
    path = write(
        root / "in-demo" / "cycles" / "in-demo-c01.md",
        cycle_md("in-demo-c01", stage="shipping", decisions=DECIDED, measurement=measurement),
    )
    log = root / "in-demo" / "observations" / "in-demo-c01.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"at": "2026-10-11T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}) + "\n")
    return path


def test_cycle_edit_that_adds_a_validator_error_is_refused_and_writes_nothing(root):
    # D163 set stage measuring on a cycle with no observation_window and no release row; nothing stopped it.
    path = write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="shipping", decisions=DECIDED))
    before = path.read_text()
    with pytest.raises(writers.WriteRefused, match="measuring needs measurement fields"):
        writers.edit_frontmatter(path, root, lambda fm: fm.update(stage="measuring"))
    assert path.read_text() == before


def test_cycle_edit_with_its_measurement_fields_is_accepted(root):
    path = _measuring(root, {})

    def change(fm):
        fm.update(stage="measuring", measurement=dict(MEASUREMENT))

    after = writers.edit_frontmatter(path, root, change)
    assert after["stage"] == "measuring" and after["measurement"]["observation_window"]["length_days"] == 28


def test_existing_errors_do_not_block_an_unrelated_edit(root):
    # A cycle already invalid (missing fields) must still accept a decision row; only new errors are refused.
    path = _measuring(root, {})
    after = writers.edit_frontmatter(path, root, lambda fm: fm["decisions"].append({**DECIDED[0], "decision": "Later"}))
    assert after["decisions"][-1]["decision"] == "Later"


def test_release_observation_is_kept_but_reports_the_missing_fields(root):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="shipping", decisions=DECIDED))
    row = writers.record_observation(root, "in-demo-c01", {"kind": "release_verified", "evidence": "r.md"})
    assert any("measurement fields" in w for w in row["cycle_warnings"])
    log = (root / "in-demo" / "observations" / "in-demo-c01.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["kind"] == "release_verified" and "cycle_warnings" not in log[-1]
