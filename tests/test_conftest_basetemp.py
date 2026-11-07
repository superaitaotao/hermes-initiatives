"""The suite must not need a hand-made --basetemp, and must not leave one behind."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ROOT, _prune_idle, relocate_basetemp


def fake_config(given=None):
    return SimpleNamespace(_tmp_path_factory=SimpleNamespace(_given_basetemp=given), option=SimpleNamespace(basetemp=None))


def test_relocates_out_of_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch = home / ".hermes" / "cache" / "scratch"
    scratch.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr("tempfile.tempdir", None)
    config = fake_config()

    safe = relocate_basetemp(config, relocated_root=tmp_path / "relocated")

    assert safe is not None and safe.parent == tmp_path / "relocated"
    assert config._tmp_path_factory._given_basetemp == safe == Path(config.option.basetemp)
    assert not safe.resolve().is_relative_to(home.resolve())


def test_leaves_explicit_or_safe_basetemp_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr("tempfile.tempdir", None)

    assert relocate_basetemp(fake_config(given=tmp_path / "mine"), relocated_root=tmp_path / "r") is None
    assert relocate_basetemp(fake_config(), relocated_root=tmp_path / "r") is None
    assert not (tmp_path / "r").exists()


def test_prunes_only_idle_basetemps(tmp_path):
    idle, fresh, other = tmp_path / "b-idle", tmp_path / "b-fresh", tmp_path / "keep"
    for d in (idle, fresh, other):
        (d / "x").mkdir(parents=True)
        (d / "x" / "f").write_text("data")
    old = time.time() - 7 * 3600
    for p in (idle, idle / "x", idle / "x" / "f", other, other / "x", other / "x" / "f"):
        os.utime(p, (old, old))

    _prune_idle(tmp_path, 6)

    assert not idle.exists() and fresh.exists() and other.exists()


def test_worker_style_run_cleans_up_after_itself(tmp_path):
    """A nested pytest with TMPDIR under a Hermes home gets a relocated basetemp that is gone afterwards."""
    home = tmp_path / "home"
    scratch = home / ".hermes" / "cache" / "scratch"
    scratch.mkdir(parents=True)
    probe = tmp_path / "probe"
    probe.mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", probe / "conftest.py")
    out = tmp_path / "seen.txt"
    (probe / "test_probe.py").write_text(f"def test_probe(tmp_path):\n    open({str(out)!r}, 'w').write(str(tmp_path))\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update(HOME=str(home), TMPDIR=str(scratch))
    env.pop("HERMES_HOME", None)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
        cwd=probe,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    seen = Path(out.read_text())
    assert not seen.resolve().is_relative_to(home.resolve())
    basetemp = next(p for p in seen.parents if p.name.startswith("b-"))
    assert not basetemp.exists(), f"relocated basetemp {basetemp} was left behind"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
