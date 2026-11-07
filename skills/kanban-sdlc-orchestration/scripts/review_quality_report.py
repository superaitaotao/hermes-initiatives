#!/usr/bin/env python3
"""Weekly reviewer-quality report for a Kanban board (read-only).

Measures what a project policy's reviewer-quality metric names:

- rounds to approval: `changes_requested` events per card that reached done in the window;
- finding outcomes: coder replies of the form `fixed <sha>`, `wrong:`, `intentional:`,
  `not worth fixing:` posted by the implementer lane (`--coder`);
- escaped defects: merges on origin/main whose subject starts with `revert` or names an
  earlier PR number in a fix (candidate list; a human or the planner confirms each one).

Usage: review_quality_report.py --board <slug> --repo <path> [--coder <profile>] [--days 7] [--json]
Writes nothing. Opens the board database read-only.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kanban_board_status import resolve_db  # noqa: E402

OUTCOME = re.compile(
    r"^\s*[-*]?\s*(?:\*\*)?(?:[A-Z]?\d+(?:-\d+)?[:.)]?\s*)?(?P<kind>fixed|wrong|intentional|not worth fixing)\b",
    re.IGNORECASE | re.MULTILINE,
)
PR_REF = re.compile(r"#(\d+)")


def rounds(conn: sqlite3.Connection, since: int) -> dict[str, int]:
    done = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE status IN ('done','archived') AND completed_at >= ? "
            "AND id IN (SELECT task_id FROM task_events WHERE kind='review_requested')",
            (since,),
        )
    ]
    out = {}
    for card in done:
        out[card] = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='changes_requested'", (card,)
        ).fetchone()[0]
    return out


def outcomes(conn: sqlite3.Connection, since: int, coder: str) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for (body,) in conn.execute(
        "SELECT body FROM task_comments WHERE author=? AND created_at >= ?", (coder, since)
    ):
        for match in OUTCOME.finditer(body or ""):
            counts[match.group("kind").lower()] += 1
    return counts


def escaped(repo: Path, days: int) -> list[dict[str, str]]:
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "origin/main", f"--since={days}.days", "--first-parent", "--merges",
         "--format=%h%x09%s"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    out = []
    for line in log:
        sha, subject = line.split("\t", 1)
        refs = PR_REF.findall(subject)
        if subject.lower().startswith("revert") or (re.match(r"(fix|hotfix)", subject, re.I) and len(refs) > 1):
            out.append({"merge": sha, "subject": subject})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Reviewer-quality report (read-only).")
    parser.add_argument("--board", required=True)
    parser.add_argument("--repo", type=Path, required=True, help="git checkout with an origin/main ref")
    parser.add_argument("--coder", default="coder", help="implementer profile whose comments carry outcomes")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    since = int(time.time()) - args.days * 86400
    conn = sqlite3.connect(f"file:{resolve_db(args.board)}?mode=ro", uri=True)
    per_card = rounds(conn, since)
    dist = collections.Counter(min(n, 4) for n in per_card.values())
    found = outcomes(conn, since, args.coder)
    total = sum(found.values())
    report = {
        "window_days": args.days,
        "cards_closed_after_review": len(per_card),
        "rounds_distribution": {("4+" if k == 4 else str(k)): v for k, v in sorted(dist.items())},
        "cards_at_round_cap_or_more": sorted(c for c, n in per_card.items() if n >= 3),
        "finding_outcomes": dict(found),
        "finding_precision": (round(found["fixed"] / total, 2) if total else None),
        "escaped_defect_candidates": escaped(args.repo, args.days),
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"Reviewer quality, last {args.days} days")
    print(f"- cards closed after review: {report['cards_closed_after_review']}")
    print(f"- rounds to approval: {report['rounds_distribution'] or 'none'}")
    if report["cards_at_round_cap_or_more"]:
        print(f"- 3+ rounds: {', '.join(report['cards_at_round_cap_or_more'])}")
    if total:
        print(f"- finding outcomes: {dict(found)} (fixed share {report['finding_precision']})")
    else:
        print("- finding outcomes: none recorded in the window")
    cands = report["escaped_defect_candidates"]
    print(f"- escaped-defect candidates: {len(cands)}")
    for item in cands:
        print(f"  - {item['merge']} {item['subject']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
