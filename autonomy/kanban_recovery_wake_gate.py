#!/usr/bin/env python3
"""Zero-token wake gate for the Kanban recovery crons.

Cron pre-run script. The scheduler skips the agent run when the last stdout line is
``{"wakeAgent": false}`` (cron/scheduler_prompt.py::_parse_wake_gate). Any other output wakes the
agent, and that output becomes the run's "Script Output" context.

Wake when anything an operator could act on changed since the last wake:
  - a lifecycle event on the board (block/unblock/complete/review/crash/...); heartbeats and
    claim/spawn/promote churn do not count
  - a worker attachment or comment on a card that is not running (plan bytes landed on a parked card)
  - a comment from anyone who is not a worker lane or this job (user and operator answers), or a
    worker comment that starts with `OWNER DECISION:` (workers cannot reassign other cards)
  - a respawn-guard reason that changed for a card
  - a running card whose heartbeat went stale
  - an active_pr dispatch hold (24h after a PR-link comment) expired: no board event marks it
  - an open PR head SHA / check rollup / merge state, or the newest main CI run, changed
  - the review queue changed (answers, requests, item states), or the daily digest is due
  - the previous run of this job did not end ok
  - the backstop: no wake for --max-quiet-hours

--recovery-signals: wake only on what the recovery cron acts on. Normal
progress (attachments on live cards, handoffs, review requests, completions with no parked dependant,
unblocks, cardless PR churn, main CI) is the dispatcher's job and no longer wakes it. Recovery signals:
  - failure events (blocked, crashed, gave_up, protocol_violation, timed_out, block_loop_detected,
    spawn_failed, rate_limited, terminal_worker_reaped, scheduled)
  - a card entering blocked/scheduled/triage, or created there
  - a completion/archive whose dependant is parked (its predicate may have cleared)
  - an attachment on a parked card; user/operator comments; OWNER DECISION comments
  - respawn guard changes, stale heartbeats, expired active_pr holds, a scheduled card's
    SCHEDULED_UNTIL time passing
  - head/checks change of a PR whose branch belongs to a parked card; a PR of a non-terminal card
    closed or merged (ledger closeout)
  - --candidates: the production bug/feature candidate set or the Cloud Run serving revisions changed,
    or the previous run asked to resume (cron/gates/<job>.resume)
  - previous run not ok; the --max-quiet-hours backstop, which also asks for the full inventory

Fail open: any error prints the error and wakes the agent. The board database is read-only here.
State: <HERMES_HOME>/cron/gates/<job>.json, written only on a wake.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# The plugin stays self-contained; no profile-local imports or PYTHONPATH required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autonomy.config import (  # noqa: E402
    add_config_arguments,
    board_path,
    job_id,
    resolve_config,
)
from hermes_initiatives_core.settings import data_dir, hermes_root, load_settings  # noqa: E402

HERMES_ROOT = hermes_root()
BOARD_DB = Path()
REVIEWS_DB = data_dir() / "reviews.db"
REPO = None
WORKERS = set()
NOISE = {"heartbeat", "claimed", "spawned", "promoted", "edited", "claim_extended", "progress",
         "dependency_wait", "pr_acceptance", "workspace_cleanup_deferred_shared", "linked"}
LIVE_ONLY = {"attached"}  # quiet only while the card has a live run
# "commented" events are judged from task_comments instead, where the author is known.
OWNER_DECISION_RE = re.compile(r"\s*\**OWNER DECISION:", re.IGNORECASE)
STALE_HEARTBEAT_S = 900
PARKED = {"blocked", "scheduled", "triage"}
RECOVERY_KINDS = {"blocked", "crashed", "gave_up", "protocol_violation", "timed_out", "block_loop_detected",
                  "spawn_failed", "rate_limited", "terminal_worker_reaped", "scheduled"}
CLOUD_RUN = {}
CLOUD_RUN_REGION = None
SCHEDULED_UNTIL_RE = re.compile(r"SCHEDULED_UNTIL=(\d{4}-\d\d-\d\dT[\d:]+(?:Z|[+-]\d\d:\d\d)?)")
ZONE = load_settings()[0].zone()


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _recovery_event(b: sqlite3.Connection, kind: str, task: str, status: dict[str, str]) -> bool:
    """--recovery-signals: does this event give the recovery cron something to act on?"""
    if kind in RECOVERY_KINDS:
        return True
    if kind in ("created", "attached"):
        return status.get(task) in PARKED
    if kind in ("completed", "archived"):
        return any(status.get(c) in PARKED for (c,) in b.execute(
            "SELECT child_id FROM task_links WHERE parent_id=?", (task,)))
    return False


def board_state(self_authors: set[str], since_event: int, since_comment: int,
                recovery_only: bool = False) -> tuple[dict, list[str]]:
    reasons: list[str] = []
    b = _ro(BOARD_DB)
    running = {r[0] for r in b.execute("SELECT id FROM tasks WHERE status='running'")}
    all_status = dict(b.execute("SELECT id, status FROM tasks"))
    max_event = since_event
    for eid, task, kind, _payload in b.execute(
            "SELECT id, task_id, kind, payload FROM task_events WHERE id > ? ORDER BY id", (since_event,)):
        max_event = eid
        if kind in NOISE or kind in ("respawn_guarded", "commented") or (kind in LIVE_ONLY and task in running):
            continue
        if recovery_only and not _recovery_event(b, kind, task, all_status):
            continue
        reasons.append(f"event {kind} {task}")
    max_comment = since_comment
    for cid, task, author, body in b.execute(
            "SELECT id, task_id, author, body FROM task_comments WHERE id > ? ORDER BY id", (since_comment,)):
        max_comment = cid
        if author not in WORKERS and author not in self_authors:
            reasons.append(f"comment by {author} on {task}")
        elif OWNER_DECISION_RE.match(body or ""):
            # Workers are fenced from reassigning other cards; the cron applies their recorded owner.
            reasons.append(f"owner decision by {author} on {task}")
    guards = {}
    for task, payload in b.execute(
            "SELECT task_id, payload FROM task_events WHERE kind='respawn_guarded' AND id IN "
            "(SELECT MAX(id) FROM task_events WHERE kind='respawn_guarded' GROUP BY task_id)"):
        if task in {r[0] for r in b.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived')")}:
            try:
                guards[task] = (json.loads(payload or "{}") or {}).get("reason")
            except ValueError:
                guards[task] = payload
    now = time.time()
    stale = sorted(t for t, hb in b.execute(
        "SELECT id, last_heartbeat_at FROM tasks WHERE status='running'") if not hb or now - hb > STALE_HEARTBEAT_S)
    statuses = dict(b.execute("SELECT id, status FROM tasks WHERE status NOT IN ('done','archived')"))
    branches = {br: (t, st) for t, st, br in b.execute(
        "SELECT id, status, branch_name FROM tasks WHERE branch_name IS NOT NULL AND status NOT IN ('done','archived')")
    } if recovery_only else {}
    return {"event": max_event, "comment": max_comment, "guards": guards, "stale": stale,
            "statuses": statuses, "branches": branches}, reasons


# Mirrors hermes_cli/kanban_db_dispatch.py::check_respawn_guard step 4 ("active_pr"): a GitHub PR
# URL in a card comment holds the ready lane for 24h unless a handoff event follows it. The hold
# ends silently (no board event), so without this the gate sleeps through cards that became
# dispatchable/unblockable just by time passing (no event is emitted when time alone clears the hold).
PR_HOLD_WINDOW_S = 86400
PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)
HOLD_IRRELEVANT = ("done", "archived", "running", "review")  # review lane skips the guard


def _is_handoff(kind: str, payload: str | None) -> bool:
    """Same rule as kanban_db_dispatch._is_handoff_event (fail closed)."""
    if kind != "assigned":
        return True
    try:
        data = json.loads(payload or "{}")
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


def pr_hold_expiries(b: sqlite3.Connection, since: float, now: float,
                     tasks: set[str] | None = None) -> list[tuple[str, float]]:
    """(task, expiry) for every active_pr hold that ended in (since, now] without being renewed
    by a newer PR-link comment or lifted early by a handoff. ``tasks`` limits the cards checked
    (default: cards whose status can still be held)."""
    if tasks is None:
        tasks = {r[0] for r in b.execute(
            f"SELECT id FROM tasks WHERE status NOT IN ({','.join('?' * len(HOLD_IRRELEVANT))})",
            HOLD_IRRELEVANT)}
    out = []
    lo = since - PR_HOLD_WINDOW_S
    for task in sorted(tasks):
        if b.execute("SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id WHERE l.child_id=? "
                     "AND p.status NOT IN ('done','archived') LIMIT 1", (task,)).fetchone():
            continue  # still waits on a parent: the hold ending cannot make it dispatchable
        pr_times = sorted(int(t) for t, body in b.execute(
            "SELECT created_at, body FROM task_comments WHERE task_id=? AND created_at>? AND created_at<=?",
            (task, lo, now)) if PR_URL_RE.search(body or ""))
        for i, p in enumerate(pr_times):
            exp = p + PR_HOLD_WINDOW_S
            if not (since < exp <= now):
                continue
            if i + 1 < len(pr_times) and pr_times[i + 1] <= exp:
                continue  # renewed by a newer PR-link comment
            if any(_is_handoff(k, pl) for k, pl in b.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id=? AND created_at>? AND created_at<=? "
                    "AND kind IN ('assigned','changes_requested','review_reopened')", (task, p, exp))):
                continue  # hold lifted earlier by a handoff, which already woke the gate
            out.append((task, exp))
    return out


def github_state() -> dict:
    """Open PR heads/checks and the newest main CI run. Raises on failure (fail open upstream)."""
    def gh(*args: str) -> list:
        exe = shutil.which("gh") or str(Path.home() / ".local" / "bin" / "gh")
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=60, check=True)
        return json.loads(out.stdout or "[]")
    prs = gh("pr", "list", "--repo", REPO, "--state", "open", "--limit", "50",
             "--json", "number,headRefName,headRefOid,statusCheckRollup")
    # Only settled facts count: head SHA and finished check conclusions. Queued -> in_progress and
    # GitHub's transient mergeStateStatus flips are not actionable and would wake every tick.
    pr_digest = {str(p["number"]): hashlib.sha256(json.dumps(
        [p["headRefOid"],
         sorted((c.get("name") or c.get("context") or "", c.get("conclusion") or c.get("state") or "")
                for c in p.get("statusCheckRollup") or []
                if (c.get("conclusion") or c.get("state") or "").upper() not in ("", "PENDING", "EXPECTED"))],
        sort_keys=True).encode()).hexdigest()[:16]
        for p in prs}
    runs = gh("run", "list", "--repo", REPO, "--branch", "main", "--limit", "8",
              "--json", "databaseId,headSha,status,conclusion,workflowName")
    main = sorted(f"{r['workflowName']}|{r['headSha'][:12]}|{r.get('conclusion') or ''}"
                  for r in runs if r.get("status") == "completed")[-5:]
    return {"prs": pr_digest, "main": main, "pr_branches": {str(p["number"]): p["headRefName"] for p in prs}}


def serving_state() -> dict:
    """Cloud Run revisions with nonzero traffic, per service. Raises on failure (fail open upstream)."""
    exe = shutil.which("gcloud") or str(Path.home() / ".local" / "bin" / "gcloud")
    out = {}
    for name, svc in CLOUD_RUN.items():
        res = subprocess.run([exe, "run", "services", "describe", svc, "--region", CLOUD_RUN_REGION,
                              "--format=json(status.traffic)"], capture_output=True, text=True, timeout=60, check=True)
        traffic = (json.loads(res.stdout or "{}").get("status") or {}).get("traffic") or []
        out[name] = sorted(f"{t.get('revisionName')}:{t.get('percent')}" for t in traffic if t.get("percent"))
    return out


def candidate_state(scripts: list[str]) -> tuple[str, str]:
    """(fingerprint, raw output) of the production candidate inventories."""
    raw = []
    for script in scripts:
        res = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=120)
        res.check_returncode()
        raw.append((res.stdout + res.stderr).strip())
    text = "\n".join(r for r in raw if r)
    return hashlib.sha256(text.encode()).hexdigest()[:16], text


def review_state() -> dict:
    if not REVIEWS_DB.exists():
        return {}
    d = _ro(REVIEWS_DB)
    one = lambda q: d.execute(q).fetchone()[0]  # noqa: E731
    return {"answers": one("SELECT COALESCE(MAX(id),0) FROM answers"),
            "requests": one("SELECT COALESCE(MAX(id),0) FROM requests"),
            "actions": one("SELECT COALESCE(MAX(id),0) FROM actions"),
            "items": hashlib.sha256(json.dumps(d.execute(
                "SELECT short_id, status FROM items ORDER BY short_id").fetchall()).encode()).hexdigest()[:16]}


def digest_due(daily_review: str | None) -> bool:
    if not daily_review:
        return False
    rc = subprocess.run([sys.executable, daily_review, "daily", "--due"], capture_output=True, timeout=60).returncode
    return rc == 0


def last_run_ok(jobs_file: Path, job_id: str) -> bool:
    data = json.loads(jobs_file.read_text())
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    job = next((j for j in jobs if j.get("id") == job_id), {})
    return job.get("last_status") in (None, "ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--self-author", action="append", default=[], help="this job's comment authors")
    ap.add_argument("--daily-review", help="daily_review.py path; wake when the digest is due")
    ap.add_argument("--max-quiet-hours", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true", help="decide but do not save state")
    ap.add_argument("--state-name", help="state file name (default: --job); lets a shadow run keep its own cursor")
    ap.add_argument("--shadow-log", help="shadow mode: append the decision as JSONL here and print nothing")
    ap.add_argument("--no-github", action="store_true", help="skip PR/main-CI readback (jobs that never act on GitHub)")
    ap.add_argument("--no-review-queue", action="store_true", help="do not wake on review-queue changes")
    ap.add_argument("--recovery-signals", action="store_true",
                    help="wake only on recovery signals (see module docstring), not on normal progress")
    ap.add_argument("--candidates", action="append", default=[],
                    help="production candidate inventory script; wake when its output or serving revisions change")
    add_config_arguments(ap, github=True)
    ap.add_argument("--cloud-run-service", action="append", default=[])
    ap.add_argument("--cloud-run-region")
    args = ap.parse_args()
    cfg = resolve_config(project=args.project, board=args.board, repo=args.repo, workers=args.worker)
    global BOARD_DB, REPO, WORKERS, CLOUD_RUN, CLOUD_RUN_REGION, ZONE
    BOARD_DB, REPO, WORKERS = board_path(cfg.board), cfg.repo, cfg.workers
    ZONE = cfg.settings.zone()
    CLOUD_RUN = {name: name for name in args.cloud_run_service}
    CLOUD_RUN_REGION = args.cloud_run_region
    if CLOUD_RUN and not CLOUD_RUN_REGION:
        ap.error("--cloud-run-region is required with --cloud-run-service")
    args.job = job_id(args.job)
    if args.state_name is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", args.state_name or ""):
        ap.error("--state-name must be a plain file name (letters, digits, '.', '_', '-')")

    home = Path(os.environ.get("HERMES_HOME") or HERMES_ROOT)
    state_file = home / "cron" / "gates" / f"{args.state_name or args.job}.json"
    prev = json.loads(state_file.read_text()) if state_file.exists() else {}
    reasons: list[str] = []
    if not prev:
        # Seed the cursors at the current board head: history before the gate existed is not news.
        b = _ro(BOARD_DB)
        prev_cursor = (b.execute("SELECT COALESCE(MAX(id),0) FROM task_events").fetchone()[0],
                       b.execute("SELECT COALESCE(MAX(id),0) FROM task_comments").fetchone()[0])
        reasons.append("first run of the gate")
    else:
        prev_cursor = (prev.get("event", 0), prev.get("comment", 0))

    rec = args.recovery_signals
    board, board_reasons = board_state(set(args.self_author), *prev_cursor, recovery_only=rec)
    reasons += board_reasons
    for task, why in board["guards"].items():
        if prev.get("guards", {}).get(task) != why:
            reasons.append(f"respawn guard {task}: {why}")
    new_stale = set(board["stale"]) - set(prev.get("stale", []))
    reasons += [f"stale heartbeat {t}" for t in sorted(new_stale)]
    if prev:
        for task, exp in pr_hold_expiries(_ro(BOARD_DB), prev.get("woke_at", 0), time.time()):
            at = dt.datetime.fromtimestamp(exp, ZONE).strftime("%H:%M %Z")
            reasons.append(f"active_pr hold expired {task} at {at}")
    if prev and rec:
        # A scheduled card's SCHEDULED_UNTIL time passing emits no board event.
        b = _ro(BOARD_DB)
        for task, payload in b.execute(
                "SELECT e.task_id, e.payload FROM task_events e JOIN tasks t ON t.id=e.task_id "
                "WHERE t.status='scheduled' AND e.kind='scheduled' AND e.id IN "
                "(SELECT MAX(id) FROM task_events WHERE kind='scheduled' GROUP BY task_id)"):
            m = SCHEDULED_UNTIL_RE.search(payload or "")
            if not m:
                continue
            try:
                until = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if prev.get("woke_at", 0) < until <= time.time():
                reasons.append(f"scheduled time reached {task} ({m.group(1)})")
    for task, status in board["statuses"].items():
        if prev.get("statuses", {}).get(task) not in (None, status):
            if rec and status not in PARKED:
                continue
            reasons.append(f"status {task}: {prev['statuses'][task]} -> {status}")

    try:
        gh_state = {} if args.no_github or not REPO else github_state()
    except Exception as exc:  # noqa: BLE001 - fail open
        gh_state = prev.get("github", {})
        reasons.append(f"github readback failed ({type(exc).__name__}); waking to be safe")
    branches = board.get("branches", {})
    prev_branches = prev.get("github", {}).get("pr_branches", {})
    for pr, digest in gh_state.get("prs", {}).items():
        if prev.get("github", {}).get("prs", {}).get(pr) != digest:
            card = branches.get(gh_state.get("pr_branches", {}).get(pr, ""))
            if rec and not (card and card[1] in PARKED):
                continue  # live cards' own PRs and cardless PRs are the dispatcher's and workers' job
            reasons.append(f"PR #{pr} head/checks changed" + (f" ({card[0]} {card[1]})" if card else ""))
    gone = set(prev.get("github", {}).get("prs", {})) - set(gh_state.get("prs", {}))
    for pr in sorted(gone):
        card = branches.get(prev_branches.get(pr, ""))
        if rec and pr in prev_branches and not card:
            continue  # cardless or its card is already done: nothing to close out
        reasons.append(f"PR #{pr} closed or merged" + (f" ({card[0]} {card[1]})" if card else ""))
    if not rec and prev and gh_state.get("main") != prev.get("github", {}).get("main"):
        reasons.append("main CI changed")

    candidates: dict = {}
    candidate_text = ""
    if args.candidates or CLOUD_RUN:
        try:
            fp, candidate_text = candidate_state(args.candidates)
            candidates["fingerprint"] = fp
        except Exception as exc:  # noqa: BLE001 - fail open
            reasons.append(f"candidate inventory failed ({type(exc).__name__}); waking to be safe")
        try:
            candidates["serving"] = serving_state()
        except Exception as exc:  # noqa: BLE001 - fail open
            candidates["serving"] = prev.get("candidates", {}).get("serving")
            reasons.append(f"serving readback failed ({type(exc).__name__}); waking to be safe")
        pc = prev.get("candidates", {})
        if args.candidates and candidates.get("fingerprint") != pc.get("fingerprint"):
            reasons.append("production candidates changed")
        if CLOUD_RUN and candidates.get("serving") != pc.get("serving"):
            reasons.append(f"production serving changed: {candidates.get('serving')}")
    resume_flag = home / "cron" / "gates" / f"{args.state_name or args.job}.resume"
    if resume_flag.exists():
        reasons.append("previous run checkpointed unfinished work (resume flag)")

    reviews = {} if args.no_review_queue else review_state()
    if prev and reviews != prev.get("reviews"):
        reasons.append("review queue changed")
    if digest_due(args.daily_review):
        reasons.append("daily digest due")
    jobs_file = home / "cron" / "jobs.json"
    if jobs_file.exists() and not last_run_ok(jobs_file, args.job):
        reasons.append("previous run did not end ok")
    quiet_h = (time.time() - prev.get("woke_at", 0)) / 3600
    full_due = (time.time() - prev.get("full_at", prev.get("woke_at", 0))) / 3600
    # Targeted wakes do not reset the full-inventory clock, so the backstop also fires on it.
    backstop = bool(prev) and max(quiet_h, full_due if rec else 0) >= args.max_quiet_hours
    quiet_h = max(quiet_h, full_due) if rec else quiet_h
    full = not rec or not prev or backstop or full_due >= args.max_quiet_hours or any(
        r.startswith(("previous run did not end ok", "github readback failed", "serving readback failed",
                      "candidate inventory failed")) for r in reasons)
    if backstop:
        reasons.append(f"backstop: {quiet_h:.1f}h since last wake")
    elif rec and full and reasons:
        reasons.append(f"full inventory due: {full_due:.1f}h since the last one")

    wake = bool(reasons)
    if args.shadow_log:
        Path(args.shadow_log).parent.mkdir(parents=True, exist_ok=True)
        with open(args.shadow_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "job": args.job, "wake": wake, "reasons": reasons[:40]}) + "\n")
    if not wake:
        if not args.shadow_log:
            print(json.dumps({"wakeAgent": False}))
        return 0
    if not args.dry_run:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".tmp")
        now_ts = time.time()
        tmp.write_text(json.dumps({**board, "github": gh_state, "reviews": reviews, "woke_at": now_ts,
                                   "full_at": now_ts if full else prev.get("full_at", prev.get("woke_at", now_ts)),
                                   "candidates": candidates or prev.get("candidates", {}),
                                   "woke_reasons": reasons[:40]}))
        tmp.replace(state_file)
        resume_flag.unlink(missing_ok=True)
    if args.shadow_log:
        return 0  # shadow: decision logged, no output, so the no-agent job stays silent
    now = dt.datetime.now(ZONE).strftime("%Y-%m-%d %H:%M %Z")
    if full:
        print(f"Wake gate ({now}): {len(reasons)} change(s) since the last agent run. Start with these; "
              "still run the full inventory.")
    else:
        print(f"Wake gate ({now}): {len(reasons)} recovery signal(s) since the last agent run. SCOPE: TARGETED. "
              "Run diagnostics and dispatch --dry-run, then assess only the cards named below plus any card "
              "diagnostics flags; skip the board-wide per-card inventory (the gate runs a full one at least "
              f"every {args.max_quiet_hours:g}h).")
    for r in reasons[:25]:
        print(f"- {r}")
    if len(reasons) > 25:
        print(f"- ... {len(reasons) - 25} more")
    if candidate_text:
        print("\n" + candidate_text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - fail open: wake the agent
        print(f"Wake gate error, waking the agent: {type(exc).__name__}: {exc}")
        sys.exit(0)
