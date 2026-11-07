from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Relocated basetemps live here, one ``b-*`` dir per pytest process. /var/tmp is disk-backed (never tmpfs).
RELOCATED_ROOT = Path("/var/tmp/hermes-initiatives-pytest")
RELOCATED_IDLE_HOURS = 6


def _hermes_homes() -> list[Path]:
    homes = [Path.home() / ".hermes"]
    if os.environ.get("HERMES_HOME"):
        homes.append(Path(os.environ["HERMES_HOME"]))
    return [h.resolve() for h in homes]


def _prune_idle(root: Path, max_idle_hours: float) -> None:
    """Remove ``b-*`` basetemps left by killed runs: nothing inside written for *max_idle_hours*."""
    cutoff = time.time() - max_idle_hours * 3600
    for entry in root.glob("b-*"):
        try:
            newest = max((p.lstat().st_mtime for p in entry.rglob("*")), default=entry.lstat().st_mtime)
        except OSError:
            continue
        if newest < cutoff:
            shutil.rmtree(entry, ignore_errors=True)


def relocate_basetemp(config, relocated_root: Path = RELOCATED_ROOT) -> Path | None:
    """Give a plain ``pytest`` run a safe, self-cleaning basetemp.

    Kanban workers set TMPDIR under ``~/.hermes``. A basetemp there makes the
    delegated-child fence open test boards read-only (165 setup errors), so workers
    passed ``--basetemp=$(mktemp -d ~/hermes-pytest-...)`` by hand and never removed
    it: 33 such 8-9 GB dirs filled the root disk in one evening. Instead, when no
    ``--basetemp`` is given and the default would sit under a Hermes home, use a
    fresh dir under *relocated_root*, removed when this pytest exits. Runs killed
    before exit are swept on the next run once idle for RELOCATED_IDLE_HOURS.
    """
    factory = config._tmp_path_factory
    if factory._given_basetemp is not None:
        return None  # an explicit --basetemp is the caller's to manage
    candidate = Path(os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()).resolve()
    if not any(candidate.is_relative_to(home) for home in _hermes_homes()):
        return None  # pytest's own pytest-of-<user> root keeps only the last 3 runs
    relocated_root.mkdir(parents=True, exist_ok=True)
    _prune_idle(relocated_root, RELOCATED_IDLE_HOURS)
    safe = Path(tempfile.mkdtemp(prefix="b-", dir=relocated_root))
    factory._given_basetemp = safe
    config.option.basetemp = str(safe)
    config._initiatives_relocated_basetemp = safe
    return safe


def _install_test_hermes_bin(config) -> None:
    """Run the ``hermes`` CLI as ``python -m hermes_cli.main`` from the importable Hermes source, without lazy installs.

    A launch may build a per-install dependency environment under every fresh HERMES_HOME it meets. Tests make
    hundreds of throwaway homes, so one run grew past 80 GB before this guard.
    """
    # Hermes's own CI switch: a launch must not sync or build dependency environments. Without it, every `hermes`
    # subprocess under a fresh throwaway HERMES_HOME builds <home>/installs/<key>/environments/... (about 1 GB).
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    if os.environ.get("HERMES_BIN"):
        return
    try:
        import hermes_cli
    except ImportError:
        return  # tests that need the CLI skip or fail on their own
    source = Path(hermes_cli.__file__).resolve().parents[1]
    wrapper = Path(tempfile.mkdtemp(prefix="hermes-bin-")) / "hermes"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{source}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" -m hermes_cli.main "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    os.environ["HERMES_BIN"] = str(wrapper)
    os.environ["PATH"] = f"{wrapper.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    config._initiatives_hermes_bin = wrapper.parent


@pytest.hookimpl(trylast=True)  # after _pytest.tmpdir has built config._tmp_path_factory
def pytest_configure(config):
    relocate_basetemp(config)
    _install_test_hermes_bin(config)


def pytest_unconfigure(config):
    wrapper_dir = getattr(config, "_initiatives_hermes_bin", None)
    if wrapper_dir is not None:
        shutil.rmtree(wrapper_dir, ignore_errors=True)
    safe = getattr(config, "_initiatives_relocated_basetemp", None)
    if safe is not None:
        shutil.rmtree(safe, ignore_errors=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def cycle_md(cycle_id: str, **overrides) -> str:
    data = {
        "id": cycle_id,
        "title": f"Cycle {cycle_id}",
        "stage": "exploring",
        "decisions": [],
        "kanban_cards": [],
        "change_register": [],
        "measurement": {},
        "result": {"verdict": "unset", "evidence": []},
        "disposition": {"action": "unset", "follow_ups": []},
    }
    data.update(overrides)
    return "---\n" + json.dumps(data, indent=1) + "\n---\nBody.\n"


DECIDED = [{"date": "2026-09-01", "by": "user", "decision": "Run it"}]
MEASUREMENT = {
    "primary_metric": "clicks",
    "source": "GSC",
    "baseline": "Sep 2026",
    "observation_window": {"starts": "verified_release", "length_days": 28},
    "success_criteria": "clicks +10%",
    "guardrails": "CTR not down",
    "insufficient_data": "fewer than 100 impressions",
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "initiatives"
    write(
        base / "in-demo" / "initiative.md",
        """
        ---
        id: in-demo
        title: Demo initiative
        status: active
        rank: 1
        goal: Grow demo
        observe_every_days: 7
        decisions:
          - {date: 2026-09-01, by: user, decision: Start demo}
        ---
        Demo brief.
    """,
    )
    return base


@pytest.fixture(autouse=True)
def isolated_hermes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # tmp_path can sit under ~/.hermes (TMPDIR=~/.hermes/cache/scratch); then get_default_hermes_root() resolves
    # to the real root and Kanban tests write to the user's real default board. Pin the Kanban root explicitly.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_INITIATIVES_DATA_DIR", str(tmp_path / "plugin-data"))
    for name in ("HERMES_SESSION_ID", "HERMES_SESSION_PROFILE", "HERMES_CRON_SESSION", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(name, raising=False)
    return home


# A valid blocker summary for review options written in tests (review_write requires one).
SUMMARY = {"what": "The demo card is blocked and holds up two others.", "why": "Its CI check failed twice on the same test file."}
