#!/usr/bin/env python3
"""File a task (or a bug subtype) as a new canonical note in tasks/."""

from __future__ import annotations

import argparse
import json

from _common import fail, resolve_root

from hermes_initiatives_core.writers import WriteRefused, report_task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--type", default="task", choices=("task", "bug"))
    parser.add_argument("--priority", default="medium", choices=("high", "medium", "low"))
    parser.add_argument("--severity", choices=("high", "medium", "low"))
    for name in ("symptom", "expected", "repro", "where", "related-initiative", "source", "body", "project", "root"):
        parser.add_argument(f"--{name}")
    parser.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args()
    root = resolve_root(args.root, args.project)
    fields = {k: v for k, v in vars(args).items() if k not in ("root", "project")}
    try:
        task = report_task(root, fields)
    except WriteRefused as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "task": task}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
