#!/usr/bin/env python3
"""Validate an initiative root: per-file rules and cross-file references. Exit 1 on errors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import resolve_root

from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", help="initiative root folder")
    parser.add_argument("--project")
    parser.add_argument("--vault-root", help="base for vault-relative source paths")
    parser.add_argument("--no-sources", action="store_true", help="skip checking that source paths exist")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = resolve_root(args.root, args.project)
    vault = Path(args.vault_root).expanduser() if args.vault_root else None
    snapshot = load(root)
    report = validate(snapshot, vault, check_sources=not args.no_sources)
    summary = {
        "root": str(root),
        "initiatives": len(snapshot.initiatives),
        "cycles": len(snapshot.cycles),
        "ideas": len(snapshot.ideas),
        "observation_logs": len(snapshot.observations),
        **report.as_dict(),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"{root}: {summary['initiatives']} initiatives, {summary['cycles']} cycles, "
            f"{summary['ideas']} ideas, {summary['observation_logs']} observation logs"
        )
        for key, messages in summary["errors"].items():
            for message in messages:
                print(f"ERROR   {key}: {message}")
        for key, messages in summary["warnings"].items():
            for message in messages:
                print(f"WARNING {key}: {message}")
        print("OK" if report.ok else "FAILED")
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
