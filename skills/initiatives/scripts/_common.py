#!/usr/bin/env python3
"""Shared entry for the initiatives skill scripts: pick a Python with the plugin's dependencies,
then locate the core package and the initiative root."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn


def _ensure_dependencies() -> None:
    """Agent terminals may run a bare ``python3``. Re-exec under the Hermes interpreter when
    ``yaml`` or ``pydantic`` is missing, so the scripts work from any profile's shell."""
    try:
        import pydantic  # noqa: F401
        import yaml  # noqa: F401

        return
    except ImportError:
        pass
    if os.environ.get("HERMES_INITIATIVES_REEXEC") == "1":
        sys.exit("initiatives: no Python with pyyaml and pydantic found; set HERMES_PYTHON")
    home = os.environ.get("HERMES_HOME", "").strip() or str(Path.home() / ".hermes")
    root = Path(home).parent.parent if Path(home).parent.name == "profiles" else Path(home)
    candidates = [os.environ.get("HERMES_PYTHON", "").strip(), str(root / "hermes-agent" / "venv" / "bin" / "python")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            env = {**os.environ, "HERMES_INITIATIVES_REEXEC": "1"}
            env.pop("PYTHONPATH", None)
            os.execve(candidate, [candidate, *sys.argv], env)
    sys.exit("initiatives: no Python with pyyaml and pydantic found; set HERMES_PYTHON")


_ensure_dependencies()

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _add_hermes_agent() -> None:
    """Kanban reads import ``hermes_cli``, which imports root modules of the agent tree (``hermes_yaml``).
    The venv exposes the package but not those modules, so put the agent tree itself on the path."""
    home = os.environ.get("HERMES_HOME", "").strip() or str(Path.home() / ".hermes")
    root = Path(home).parent.parent if Path(home).parent.name == "profiles" else Path(home)
    agent = root / "hermes-agent"
    if (agent / "hermes_cli").is_dir() and str(agent) not in sys.path:
        sys.path.append(str(agent))


_add_hermes_agent()

from hermes_initiatives_core import settings  # noqa: E402


def fail(message: str) -> NoReturn:
    print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
    sys.exit(2)


def resolve_root(root: str | None, project: str | None) -> Path:
    if root:
        return Path(root).expanduser().resolve()
    env_root = os.environ.get("HERMES_INITIATIVES_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    config, error = settings.load_settings()
    if error:
        fail(f"settings file is invalid: {error}")
    if project:
        match = config.project(project)
        if match is None:
            fail(f"unknown project '{project}'; known: {', '.join(p.id for p in config.projects) or 'none'}")
        return match.root
    if len(config.projects) == 1:
        return config.projects[0].root
    fail("pass --project or --root; settings list " + str(len(config.projects)) + " projects")
