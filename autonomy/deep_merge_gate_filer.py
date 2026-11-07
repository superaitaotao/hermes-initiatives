#!/usr/bin/env python3
"""File the Review decision for a deep-tier merge gate as soon as the reviewer blocks, without an LLM.

A deep-tier PR that passed review is blocked by the configured reviewer with a fixed shape (the project policy skill ->
Deep-tier user merge gate): a newest comment headed ``REVIEW PASSED — DEEP: NEEDS YOUR MERGE APPROVAL`` with
``**You:** approve merging PR #<n> at <40-hex sha>``, then ``kanban_block(kind="needs_input", reason="Deep-tier
merge approval: PR #<n> at <sha>")``. The decision is always the same two options, so this script writes it
directly instead of waiting up to 30 minutes for the recovery tick's generator:

  A  approve that exact head; the reviewer merges with --match-head-commit (recommended).
     ``blocked`` card: kanban_unblock. ``triage`` card: kanban_comment, and the recovery tick routes the card
     back to the configured reviewer (``unblock`` refuses ``triage``).
  B  defer: keep the PR parked unmerged

A second ``needs_input`` block on the same card trips Hermes's unblock-loop breaker (``BLOCK_RECURRENCE_LIMIT``),
so a reviewer's gate block often lands in ``triage`` with a ``block_loop_detected`` event instead of ``blocked``.
Both shapes are the same gate.

It files the item under the collector's own key and basis (``kanban:<board>:<card>:gate``, built by
``review_collect._kanban_gates``), so the 30-minute tick sees current options and writes nothing new. It only adds
options to a card that has no active item or an open item without current options; anything already answered,
deferred, watched, or dismissed is left alone. After filing it runs ``daily_review.py notify`` for the Telegram push
(within the configured push window; the tick retries outside it).

Cron job (current profile, no agent, every 2 minutes). Stdout is empty when nothing was filed.
  deep_merge_gate_filer.py [--dry-run] [--no-notify]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# The plugin stays self-contained; no profile-local imports or PYTHONPATH required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autonomy.config import (  # noqa: E402
    PLUGIN_ROOT,
)
from hermes_initiatives_core.settings import data_dir, load_settings  # noqa: E402

SCRIPTS = PLUGIN_ROOT / "skills/initiatives/scripts"

from hermes_initiatives_core import kanban, review_collect  # noqa: E402
from hermes_initiatives_core.reviews import ReviewStore, basis_hash  # noqa: E402
from hermes_initiatives_core.store import load  # noqa: E402

HEADER = "REVIEW PASSED — DEEP: NEEDS YOUR MERGE APPROVAL"
YOU_RE = re.compile(r"approve merging PR #(\d+) at ([0-9a-f]{40})\b")
REASON_RE = re.compile(r"Deep-tier merge approval: PR #(\d+) at ([0-9a-f]{40})\b")


def gate_cards(board: str, reviewer: str) -> list[dict]:
    """Blocked or triaged needs_input cards whose newest reviewer comment and newest block are the deep merge gate."""
    conn = kanban._connect(kanban.board_path(board))
    try:
        rows = conn.execute(
            "SELECT id, title, status FROM tasks WHERE status IN ('blocked', 'triage') AND block_kind = 'needs_input'"
        ).fetchall()
        out = []
        for row in rows:
            # The newest reviewer comment, not the newest comment: an operator relay or a user note posted
            # after the gate must not hide it. A later unblock or review event means the gate was answered.
            comment = conn.execute(
                "SELECT author, body, created_at FROM task_comments WHERE task_id = ? AND author = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (row["id"], reviewer),
            ).fetchone()
            resumed = conn.execute(
                "SELECT MAX(created_at) FROM task_events WHERE task_id = ? "
                "AND kind IN ('unblocked', 'review_requested', 'claimed')",
                (row["id"],),
            ).fetchone()[0]
            block = conn.execute(
                "SELECT payload, created_at FROM task_events WHERE task_id = ? "
                "AND kind IN ('blocked', 'block_loop_detected') "
                "ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if (
                not comment
                or not block
                or (resumed and resumed > comment["created_at"])
            ):
                continue
            # The header is the comment's first non-empty line, with or without bold. Reviewers write both
            # ``**REVIEW PASSED — DEEP: ...** — summary`` and a bare ``REVIEW PASSED — DEEP: ...`` line.
            first = next((ln for ln in comment["body"].splitlines() if ln.strip()), "")
            header = first.strip().lstrip("*").strip()
            handoff = kanban.parse_handoff(comment["body"])
            you = YOU_RE.search(handoff["you"] or "")
            reason = REASON_RE.search(
                str((json.loads(block["payload"] or "{}") or {}).get("reason") or "")
            )
            if not header.startswith(HEADER) or not you or not reason:
                continue
            if (
                you.groups() != reason.groups()
                or block["created_at"] < comment["created_at"]
            ):
                continue  # the block must be this approval comment's block, for the same PR and head
            out.append(
                {
                    "card": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "pr": int(you.group(1)),
                    "sha": you.group(2),
                    "comment": comment["body"],
                    "reason": f"Deep-tier merge approval: PR #{you.group(1)} at {you.group(2)}",
                }
            )
        return out
    finally:
        conn.close()


def _brief(comment: str, limit: int = 1500) -> str:
    """The reviewer's findings and checks: the comment body after the You/Next lines, before the Evidence section."""
    body = comment.split("\n---", 1)[0].splitlines()[1:]
    lines = [ln for ln in body if not ln.startswith(("**You:**", "**Next:**"))]
    text = "\n".join(lines).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def decision(gate: dict, board: str) -> dict:
    n, sha, card = gate["pr"], gate["sha"], gate["card"]
    title = gate["title"] if len(gate["title"]) <= 140 else gate["title"][:139] + "…"
    if gate.get("status") == "triage":
        approve = {
            "id": "A",
            "label": f"Approve merging PR #{n} at {sha[:8]}: post the approval on {card}; recovery "
            "then routes it from triage to the configured reviewer, who merges that exact head",
            "action": "kanban_comment",
            "args": {"board": board, "card": card},
        }
    else:
        approve = {
            "id": "A",
            "label": f"Approve merging PR #{n} at {sha[:8]} and unblock {card} so the configured reviewer "
            "merges it",
            "action": "kanban_unblock",
            "args": {"board": board, "card": card},
        }
    return {
        "question": f"Approve merging PR #{n} at head {sha}?",
        "options": [
            approve,
            {
                "id": "B",
                "label": f"Keep PR #{n} parked unmerged",
                "action": "defer",
                "args": {"kind": "state_change"},
            },
        ],
        "recommend": "A",
        "context": (
            f"A: the configured reviewer merges PR #{n} with a merge commit pinned to {sha[:8]} (--match-head-commit) and "
            "checks it reached main. A new head needs a new review and a new approval.\n"
            "B: the PR stays open and the card stays blocked.\n"
            f"Reviewer's verdict:\n{_brief(gate['comment'])}"
        ),
        "summary": {
            "what": f"{card} ({title}) passed review and waits only for your approval to merge PR #{n}.",
            "why": f"The PR touches a deep-tier file, so it never merges without your approval of the exact head "
            f"{sha[:8]}. The reviewer found no blocking problems at that head.",
        },
    }


def file_gates(store: ReviewStore, project, *, dry_run: bool = False) -> list[dict]:
    """File one decision per deep merge gate that has no current one. Returns what was (or would be) filed."""
    board = str(project.kanban_board)
    filed = []
    gates = gate_cards(board, project.merge_reviewer) if project.merge_reviewer else []
    if not gates:
        return filed
    owners = review_collect._card_cycles(load(project.root))
    for gate in gates:
        listed = {gate["card"]: {"id": gate["card"], "waiting": gate["reason"]}}
        candidates = review_collect._kanban_gates(project, owners, listed)
        if not candidates:
            continue
        c = candidates[0]
        item = store.get_by_key(c["key"])
        if item is None and store.is_dismissed(c["key"], basis_hash(c["basis"])):
            continue  # answered and applied with this exact basis
        if item is not None and (
            item["status"] != "open"
            or (
                item["version"]
                and item["version"]["basis_hash"] == basis_hash(c["basis"])
            )
        ):
            continue  # already answerable, answered, parked, or in flight
        entry = {
            "card": gate["card"],
            "pr": gate["pr"],
            "sha": gate["sha"],
            "short_id": item and item["short_id"],
        }
        if dry_run:
            filed.append(entry)
            continue
        item, _ = store.upsert_item(
            c["key"],
            section="gated",
            source=c["source"],
            title=c["title"],
            basis=c["basis"],
            blocks=c["blocks"],
            links=c["links"],
            opened_at=c["opened_at"],
            project=c["project"],
            initiative=c["initiative"],
            cycle=c["cycle"],
            owner_profile=c["owner_profile"],
            evidence_hash=basis_hash(c["context"]),
        )
        store.set_context(item["short_id"], c["context"])
        d = decision(gate, board)
        store.add_version(
            item["short_id"],
            question=d["question"],
            options=d["options"],
            recommend=d["recommend"],
            context=d["context"],
            summary=d["summary"],
        )
        entry["short_id"] = item["short_id"]
        filed.append(entry)
    return filed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be filed; write nothing",
    )
    ap.add_argument(
        "--no-notify", action="store_true", help="file without the Telegram push"
    )
    ap.add_argument("--project", help="limit filing to this configured project")
    args = ap.parse_args()
    config, error = load_settings()
    if error:
        print(f"deep merge gate filer: {error}", file=sys.stderr)
        return 2
    if args.project and config.project(args.project) is None:
        ap.error(f"unknown project {args.project!r}")
    projects = [
        p for p in config.projects if p.kanban_board and p.merge_reviewer and (not args.project or p.id == args.project)
    ]
    if not projects:
        return 0  # no merge reviewer configured: nothing to file, and no store is created
    store_path = data_dir() / "reviews.db"
    if args.dry_run and not store_path.exists():
        return 0  # a dry run never creates the review store; with no store there is no item to compare against
    store = ReviewStore(read_only=True) if args.dry_run else ReviewStore()
    filed = []
    for project in projects:
        filed += file_gates(store, project, dry_run=args.dry_run)
    if not filed:
        return 0
    result = {"filed": filed, "dry_run": args.dry_run}
    target = config.review_target
    if not args.dry_run and not args.no_notify and target:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        source = Path(sys.executable).absolute().parents[2]
        if (source / "hermes_cli").is_dir():
            env["PYTHONPATH"] = str(source)
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "daily_review.py"),
                "notify",
                "--target",
                target,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        )
        result["notify"] = proc.stdout.strip() or proc.stderr.strip()
        if proc.returncode != 0:
            print(json.dumps(result), file=sys.stderr)
            return 1  # filed; the push failed and the 30-minute tick retries it
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, sqlite3.Error) as exc:
        print(f"deep merge gate filer: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
