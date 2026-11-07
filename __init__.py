"""Hermes initiatives plugin: registers the agent-facing ``initiatives`` skill and the daily review answer hook.

The dashboard backend lives in ``dashboard/plugin_api.py`` and the Desktop page in ``desktop/plugin.js``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent
_SKILLS = _ROOT / "skills"


def register(ctx) -> None:
    for child in sorted(_SKILLS.iterdir()) if _SKILLS.is_dir() else ():
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            description = "Record initiative observations or propose ideas."
            try:
                text = skill_md.read_text(encoding="utf-8")
                frontmatter = yaml.safe_load(text.split("---", 2)[1]) if text.startswith("---") else {}
                if isinstance(frontmatter, dict) and isinstance(frontmatter.get("description"), str):
                    description = frontmatter["description"] or description
            except (OSError, ValueError, IndexError, yaml.YAMLError):
                pass
            ctx.register_skill(child.name, skill_md, description)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from hermes_initiatives_core import review_buttons, review_hook
    from hermes_initiatives_core.cli import command, register_cli

    ctx.register_cli_command("initiatives", "Set up and inspect initiative autonomy jobs", register_cli, command)
    ctx.register_hook("post_gateway_admission", review_hook.post_gateway_admission)
    # Button taps on a pushed decision arrive at the adapter, not at post_gateway_admission (text only).
    ctx.register_platform_handler("telegram", review_buttons.telegram_factory)
