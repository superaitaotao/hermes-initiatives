#!/usr/bin/env python3
"""Read-only lint for Kanban card comments against the Card comment format.

Checks recent comments (see ``references/operator-status.md`` -> "Card comment
format") for: a fixed verdict first line, ``You:``/``Next:`` lines, a short
top part above the ``---`` Evidence rule, no full 40-hex SHAs / run IDs above
the rule, and fused words that signal lost line breaks.

The board database is opened read-only; the path is resolved the same way as
``kanban_board_status.py``. Exit status is always 0: this is a report, not a gate.

Usage:
    python3 kanban_comment_lint.py [--board <slug>] [--since-hours 24]
        [--author <profile>] [--task <id>] [--limit 200]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kanban_board_status import resolve_db  # noqa: E402

VERDICTS = (
    "READY FOR REVIEW", "CHANGES REQUIRED", "APPROVED & MERGED",
    "BLOCKED — NEEDS YOU", "BLOCKED — MACHINE", "RECOVERED", "UNBLOCKED", "FYI",
)
TOP_LIMIT = 600
EVIDENCE_LIMIT = 2000
FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")
RUN_ID = re.compile(r"\brun\s*#?\d{8,}\b|\bactions/runs/\d+", re.I)
# A letter word glued directly onto a long hex/number, e.g. "head0829be9a...".
FUSED = re.compile(r"\b[a-z]{3,}(?:[0-9a-f]{12,}|\d{6,})\b")
RULE = re.compile(r"^\s*---\s*$", re.M)


def check(body: str) -> list[str]:
    issues: list[str] = []
    lines = body.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if not any(first.startswith(f"**{v}**") for v in VERDICTS):
        issues.append("no verdict first line")
    parts = RULE.split(body, maxsplit=1)
    top = parts[0]
    evidence = parts[1] if len(parts) > 1 else ""
    if not re.search(r"^\*\*You:\*\*", top, re.M):
        issues.append("no You: line")
    if not re.search(r"^\*\*Next:\*\*", top, re.M):
        issues.append("no Next: line")
    if len(top) > TOP_LIMIT:
        issues.append(f"top too long ({len(top)}>{TOP_LIMIT} chars)")
    if len(evidence) > EVIDENCE_LIMIT:
        issues.append(f"evidence too long ({len(evidence)}>{EVIDENCE_LIMIT} chars; attach a file)")
    if FULL_SHA.search(top):
        issues.append("full SHA above rule")
    if RUN_ID.search(top):
        issues.append("run ID above rule")
    fused = FUSED.findall(body)
    if fused:
        issues.append(f"fused words (lost newlines?): {', '.join(sorted(set(fused))[:3])}")
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--board")
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--author")
    ap.add_argument("--task")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    db = resolve_db(args.board)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sql = "SELECT id, task_id, author, body, created_at FROM task_comments WHERE created_at >= ?"
    params: list[object] = [time.time() - args.since_hours * 3600]
    if args.author:
        sql += " AND author = ?"
        params.append(args.author)
    if args.task:
        sql += " AND task_id = ?"
        params.append(args.task)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()

    total = Counter()
    clean = Counter()
    issue_counts: Counter[str] = Counter()
    for cid, task, author, body, _ts in rows:
        total[author] += 1
        issues = check(body or "")
        if not issues:
            clean[author] += 1
            continue
        for i in issues:
            issue_counts[i.split(" (")[0].split(":")[0]] += 1
        print(f"{task} #{cid} {author} ({len(body or '')} chars): {'; '.join(issues)}")

    print(f"\n# {len(rows)} comments in last {args.since_hours:g}h on {db}")
    for author, n in total.most_common():
        print(f"#   {author}: {clean[author]}/{n} conform")
    for issue, n in issue_counts.most_common():
        print(f"#   {issue}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
