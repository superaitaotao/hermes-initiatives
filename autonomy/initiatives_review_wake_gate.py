#!/usr/bin/env python3
"""Zero-token wake gate for the initiatives review job.

Cron pre-run script (scheduler contract: a last stdout line ``{"wakeAgent": false}`` skips the agent run; any
other output wakes it and becomes the run's "Script Output").

Every run first writes the human gate list (``kanban_human_gates_watch.py --gates-json``). A gate card whose
newest gate event came after the latest finished recovery agent run is held out of the list (at most
--hold-max-min), so the review never pushes a card that recovery has not assessed yet. A card that already has
an active review item is never held: leaving the list would resolve its item.

Then it wakes the review agent only when the review has work:

  - the gate list changed (cards, statuses, signals or waiting text; ``generated_at`` is ignored)
  - the review queue changed (answers, requests, actions, item states), or an item is marked ``auto``
  - an initiative, cycle, observation or idea file changed
  - an ``agent_owned:``/``waiting_on:`` watch can reopen: every card it names is done or archived
  - a card named in a deferral reason changed status (the review checks whether the condition is met)
  - an initiative status summary is out of date. This rides along with any other wake, and wakes alone at most
    once per --summary-min-interval-min. ``status_summaries.py tick`` also re-confirms unchanged summaries, so
    the page does not mark them stale while the agent sleeps.
  - the daily digest is due, or a failed send waits for retry
  - the previous run of this job did not end ok
  - the backstop: no wake for --max-quiet-hours

Self-noise filters:
  - Gate-list hashing ignores ages in waiting text ("Ready for 1.2h with no worker" -> "Ready for <age> ..."), so
    a card that only grows older no longer changes the list every tick.
  - The review run itself writes reviews.db (item syncs, settlements) and initiative files. Item/action changes and
    file changes count only when they happened after this job's previous run finished (``last_run_at``), or when
    the file set changed. New answers, requests and ``auto`` items always count.

Fail open: any error prints the error and wakes the agent. A failed gate list removes the file (the review then
skips Kanban items) and wakes the agent so it can say so.
State: <HERMES_HOME>/cron/gates/<job>.json, written only on a wake.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# The plugin stays self-contained; no profile-local imports or PYTHONPATH required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autonomy.config import (  # noqa: E402
    PLUGIN_ROOT,
    add_config_arguments,
    board_path,
    job_id,
    profile_home,
    resolve_config,
)
from hermes_initiatives_core.settings import data_dir, hermes_root, load_settings  # noqa: E402

HERMES_ROOT = hermes_root()
PROFILE = profile_home()
GATES_WATCH = PLUGIN_ROOT / "autonomy/kanban_human_gates_watch.py"
SKILL_SCRIPTS = PLUGIN_ROOT / "skills/initiatives/scripts"
DAILY_REVIEW = SKILL_SCRIPTS / "daily_review.py"
STATUS_SUMMARIES = SKILL_SCRIPTS / "status_summaries.py"
REVIEWS_DB = data_dir() / "reviews.db"
SETTINGS = data_dir() / "settings.json"
ZONE = load_settings()[0].zone()
GATE_FLAGS = []

# Same rules as hermes_initiatives_core: reviews.ACTIVE and review_collect WATCH_CARD / OWNER_GROUNDS / DONE_STATUSES.
ACTIVE_ITEM_STATUSES = ("open", "deferred", "watching", "auto", "answered", "awaiting_confirm", "applying",
                        "ready_for_owner", "failed")
CARD_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
OWNER_GROUNDS = ("agent_owned:", "waiting_on:")
DONE_STATUSES = ("done", "archived")
GATE_EVENT_KINDS = ("blocked", "scheduled", "block_loop_detected")
AGE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:h|m|min|mins|hours?|minutes?)\b")


def stable_cards(cards: list[dict] | None) -> list[dict]:
    """Gate-list cards with growing ages masked, for change detection only."""
    return [{**c, "waiting": AGE_RE.sub("<age>", str(c.get("waiting") or ""))} for c in cards or []]


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.timestamp()


def last_run_end(jobs_file: Path, job_id: str) -> float:
    if not jobs_file.exists():
        return 0.0
    job = next((j for j in _jobs(jobs_file) if j.get("id") == job_id), {})
    return _ts(job.get("last_run_at"))


def reviews_touched_after(since: float) -> bool:
    """An item or action row written after ``since``."""
    if not REVIEWS_DB.exists():
        return False
    d = _ro(REVIEWS_DB)
    try:
        newest = [d.execute("SELECT MAX(updated_at) FROM items").fetchone()[0],
                  d.execute("SELECT MAX(at) FROM actions").fetchone()[0]]
    finally:
        d.close()
    return any(_ts(v) > since for v in newest)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    source = Path(sys.executable).absolute().parents[2]
    if (source / "hermes_cli").is_dir():
        env["PYTHONPATH"] = str(source)  # trusted Hermes runtime, never inherited workspace imports
    return env


def _jobs(jobs_file: Path) -> list[dict]:
    data = json.loads(jobs_file.read_text())
    return data if isinstance(data, list) else data.get("jobs", [])


def recovery_seen_at(jobs_file: Path, gates_dir: Path, recovery_job: str, fallback: float) -> float:
    """Time up to which the recovery job has assessed the board: its ``last_run_at``, once that passed the
    recovery gate's newest ``woke_at``.

    A tick the recovery gate skipped also counts: the gate skips only when no lifecycle event (a block or park
    is one) happened since its last wake. Events during a woken run are the run's own actions or wake the next
    tick. While a woken run is still going (``last_run_at`` < ``woke_at``), ``fallback`` holds."""
    state = gates_dir / f"{recovery_job}.json"
    if not state.exists() or not jobs_file.exists():
        return fallback
    woke = float(json.loads(state.read_text()).get("woke_at") or 0)
    last = next((j for j in _jobs(jobs_file) if j.get("id") == recovery_job), {}).get("last_run_at")
    if not last or not woke:
        return fallback
    last_ts = dt.datetime.fromisoformat(last).timestamp()
    return last_ts if last_ts >= woke else fallback


def active_gate_keys(reviews: sqlite3.Connection) -> set[str]:
    marks = ",".join("?" * len(ACTIVE_ITEM_STATUSES))
    return {r[0] for r in reviews.execute(f"SELECT key FROM items WHERE status IN ({marks})", ACTIVE_ITEM_STATUSES)}


def held_cards(cards: list[dict], board: str, board_conn: sqlite3.Connection, active_keys: set[str],
               seen_at: float, now: float, hold_max_s: float) -> list[str]:
    """Gate cards whose newest gate event came at or after ``seen_at`` and less than ``hold_max_s`` ago.

    A card with an active review item is never held. The time cap keeps a stopped recovery job from hiding a
    gate for long."""
    marks = ",".join("?" * len(GATE_EVENT_KINDS))
    held = []
    for card in cards:
        card_id = str(card.get("id"))
        if f"kanban:{board}:{card_id}:gate" in active_keys:
            continue
        ts = board_conn.execute(
            f"SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ({marks})",
            (card_id, *GATE_EVENT_KINDS)).fetchone()[0]
        if ts and float(ts) >= seen_at and now - float(ts) < hold_max_s:
            held.append(card_id)
    return held


def write_gate_list(path: Path, recovery_seen: float, hold_max_s: float) -> tuple[str | None, list[str]]:
    """Write the gate list without held cards; return (hash of its cards, held card ids).

    The hash is None when the list could not be written; the file is then gone."""
    proc = subprocess.run(
        [sys.executable, str(GATES_WATCH), *GATE_FLAGS, "--gates-json", str(path)],
        capture_output=True, text=True, timeout=120, env=_clean_env(),
    )
    if proc.returncode != 0 or not path.exists():
        path.unlink(missing_ok=True)
        print(f"gate list failed: {proc.stderr.strip() or proc.stdout.strip()}", file=sys.stderr)
        return None, []
    body = json.loads(path.read_text(encoding="utf-8"))
    board = str(body.get("board") or "")
    cards = body.get("cards") or []
    held: list[str] = []
    if cards and board:
        active = active_gate_keys(_ro(REVIEWS_DB)) if REVIEWS_DB.exists() else set()
        held = held_cards(cards, board, _ro(board_path(board)), active, recovery_seen, time.time(),
                          hold_max_s)
    if held:
        body["cards"] = [c for c in cards if c.get("id") not in held]
        body["held_until_recovery"] = held
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)
    return _digest(stable_cards(body.get("cards"))), held


def card_statuses(board_conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return dict(board_conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({marks})", ids))


def _named(reason: str | None, basis_json: str | None) -> list[str]:
    own = (json.loads(basis_json or "{}") or {}).get("card")
    return sorted(set(CARD_RE.findall(reason or "")) - {own})


def reopenable_watches(reviews: sqlite3.Connection, board_conn: sqlite3.Connection) -> list[str]:
    """Watches whose named owner cards all finished; the review's sync reopens these (review_collect's rule)."""
    out = []
    for short_id, reason, basis in reviews.execute(
            "SELECT short_id, watch_reason, basis_json FROM items WHERE status = 'watching'"):
        if not str(reason or "").startswith(OWNER_GROUNDS):
            continue
        named = _named(reason, basis)
        if not named:
            continue
        statuses = card_statuses(board_conn, named)
        if len(statuses) == len(named) and all(s in DONE_STATUSES for s in statuses.values()):
            out.append(short_id)
    return sorted(out)


def deferral_cards(reviews: sqlite3.Connection, board_conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """{deferred item: {card named in its newest deferral reason: status}}."""
    out = {}
    for short_id, basis, reason in reviews.execute(
            "SELECT i.short_id, i.basis_json, d.reason FROM items i JOIN deferrals d ON d.short_id = i.short_id "
            "WHERE i.status = 'deferred' AND d.id IN (SELECT MAX(id) FROM deferrals GROUP BY short_id)"):
        named = _named(reason, basis)
        if named:
            out[short_id] = card_statuses(board_conn, named)
    return out


def deferral_changes(prev: dict[str, dict[str, str]], now: dict[str, dict[str, str]]) -> list[str]:
    """Items deferred in both snapshots whose named cards changed status. A new deferral is not news."""
    return [
        f"deferral {sid}: a named card changed status ({', '.join(f'{c} {s}' for c, s in sorted(now[sid].items()))})"
        for sid in sorted(now) if sid in prev and prev[sid] != now[sid]
    ]


def summaries_due() -> list[str] | None:
    """Initiatives whose status summary is out of date; None when the check failed.

    ``status_summaries.py tick`` also re-confirms (``checked_at``) every summary whose facts are unchanged."""
    due: list[str] = []
    for project in [p.model_dump() for p in load_settings()[0].projects]:
        out = subprocess.run(
            [sys.executable, str(STATUS_SUMMARIES), "tick", "--project", str(project["id"])],
            capture_output=True, text=True, timeout=120, env=_clean_env(),
        )
        if out.returncode != 0:
            return None
        due += [str(d.get("initiative")) for d in json.loads(out.stdout or "{}").get("due", [])]
    return due


def summary_wakes(due: list[str] | None, other_reasons: list[str], quiet_min: float, interval_min: float) -> bool:
    if due is None:
        return True
    return bool(due) and (bool(other_reasons) or quiet_min >= interval_min)


def review_state() -> dict:
    if not REVIEWS_DB.exists():
        return {}
    d = _ro(REVIEWS_DB)
    try:
        one = lambda q: d.execute(q).fetchone()[0]  # noqa: E731
        return {
            "answers": one("SELECT COALESCE(MAX(id),0) FROM answers"),
            "requests": one("SELECT COALESCE(MAX(id),0) FROM requests"),
            "actions": one("SELECT COALESCE(MAX(id),0) FROM actions"),
            "items": _digest(
                d.execute(
                    "SELECT short_id, status FROM items ORDER BY short_id"
                ).fetchall()
            ),
            "auto": one("SELECT COUNT(*) FROM items WHERE status = 'auto'"),
        }
    finally:
        d.close()


def initiative_file_stats() -> tuple[int, float]:
    """(file count, newest mtime) over the same files as ``initiative_files``."""
    roots = [Path(p["root"]) for p in [p.model_dump() for p in load_settings()[0].projects]]
    stats = [f.stat().st_mtime for root in roots if root.is_dir() for f in root.rglob("*")
             if f.suffix in (".md", ".jsonl", ".yaml") and f.is_file()]
    return len(stats), max(stats, default=0.0)


def initiative_files() -> str:
    """Hash of (path, mtime, size) for every Markdown/JSONL file under each project root."""
    roots = [
        Path(p["root"])
        for p in [p.model_dump() for p in load_settings()[0].projects]
    ]
    files = sorted(
        (str(f), f.stat().st_mtime_ns, f.stat().st_size)
        for root in roots
        if root.is_dir()
        for f in root.rglob("*")
        if f.suffix in (".md", ".jsonl", ".yaml") and f.is_file()
    )
    return _digest(files)


def outcome_signals() -> dict | None:
    """Derived deadlines/card transitions, including time changes with no file edit. Never writes sources."""
    script = SKILL_SCRIPTS / "outcome_status.py"
    if not script.is_file():
        return None
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            timeout=120, env=_clean_env())
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def digest_due() -> bool:
    return (
        subprocess.run(
            [sys.executable, str(DAILY_REVIEW), "daily", "--due"],
            capture_output=True,
            timeout=60,
            env=_clean_env(),
        ).returncode
        == 0
    )


def last_run_ok(jobs_file: Path, job_id: str) -> bool:
    job = next((j for j in _jobs(jobs_file) if j.get("id") == job_id), {})
    return job.get("last_status") in (None, "ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--gates-json", type=Path, required=True)
    ap.add_argument("--recovery-job", default="", help="Kanban recovery job whose pass a new gate waits for")
    ap.add_argument("--hold-max-min", type=float, default=60.0, help="longest hold for a gate recovery has not seen")
    ap.add_argument("--summary-min-interval-min", type=float, default=60.0,
                    help="least time between wakes caused only by out-of-date summaries")
    ap.add_argument("--max-quiet-hours", type=float, default=3.0)
    ap.add_argument(
        "--dry-run", action="store_true", help="decide but do not save state"
    )
    add_config_arguments(ap)
    args = ap.parse_args()
    cfg = resolve_config(project=args.project, board=args.board)
    global GATE_FLAGS, ZONE
    GATE_FLAGS = ["--board", cfg.board]
    if cfg.project:
        GATE_FLAGS += ["--project", cfg.project]
    ZONE = cfg.settings.zone()
    args.job = job_id(args.job)
    if args.recovery_job:
        args.recovery_job = job_id(args.recovery_job)


    home = Path(os.environ.get("HERMES_HOME") or PROFILE)
    state_file = home / "cron" / "gates" / f"{args.job}.json"
    jobs_file = home / "cron" / "jobs.json"
    prev = json.loads(state_file.read_text()) if state_file.exists() else {}
    reasons: list[str] = [] if prev else ["first run of the gate"]

    seen = recovery_seen_at(jobs_file, state_file.parent, args.recovery_job, float(prev.get("recovery_seen") or 0))
    gates, held = write_gate_list(args.gates_json, seen, args.hold_max_min * 60 if args.recovery_job else 0)
    board_conn = None
    if gates is not None:
        board_name = json.loads(args.gates_json.read_text(encoding="utf-8")).get("board")
        if board_name:
            board_conn = _ro(board_path(str(board_name)))
    reviews_conn = _ro(REVIEWS_DB) if REVIEWS_DB.exists() else None
    can_read = board_conn is not None and reviews_conn is not None
    state = {
        "gates": gates,
        "reviews": review_state(),
        "files": initiative_files(),
        "outcome_loops": outcome_signals(),
        "reopenable": reopenable_watches(reviews_conn, board_conn) if can_read else prev.get("reopenable", []),
        "deferrals": deferral_cards(reviews_conn, board_conn) if can_read else prev.get("deferrals", {}),
        "recovery_seen": seen,
    }
    if state["outcome_loops"] is None:
        reasons.append("outcome-loop signal check unavailable")
    elif state["outcome_loops"] != prev.get("outcome_loops"):
        reasons.append("outcome-loop deadline or execution state changed")
    if state["gates"] is None:
        reasons.append("gate list failed: the review must skip Kanban items and say so")
    elif prev and state["gates"] != prev.get("gates"):
        reasons.append("human gate list changed")
    # Changes the previous review run made itself are not news: count item/action/file changes only after it ended.
    since = max(float(prev.get("woke_at") or 0), last_run_end(jobs_file, args.job))
    user_keys = ("answers", "requests", "auto")
    pr = prev.get("reviews") or {}
    if prev and state["reviews"] != pr:
        if any(state["reviews"].get(k) != pr.get(k) for k in user_keys):
            reasons.append("review queue changed: new answer, request or auto item")
        elif reviews_touched_after(since):
            reasons.append("review queue changed after the last review run")
    count, newest = initiative_file_stats()
    state["file_count"] = count
    if state["reviews"].get("auto"):
        reasons.append(f"{state['reviews']['auto']} item(s) marked auto")
    if prev and state["files"] != prev.get("files") and (
            newest > since or count != prev.get("file_count", count)):
        reasons.append("initiative files changed")
    new_reopen = sorted(set(state["reopenable"]) - set(prev.get("reopenable", [])))
    reasons += [f"watch {sid} can reopen: every owner card it names finished" for sid in new_reopen]
    reasons += deferral_changes(prev.get("deferrals", {}), state["deferrals"])
    if digest_due():
        reasons.append("daily digest due")
    if jobs_file.exists() and not last_run_ok(jobs_file, args.job):
        reasons.append("previous run did not end ok")
    quiet_h = (time.time() - prev.get("woke_at", 0)) / 3600
    if prev and quiet_h >= args.max_quiet_hours:
        reasons.append(f"backstop: {quiet_h:.1f}h since last wake")
    due = summaries_due()
    if summary_wakes(due, reasons, quiet_h * 60, args.summary_min_interval_min):
        reasons.append("status summary check failed" if due is None
                       else f"{len(due)} initiative summary(ies) out of date: {', '.join(due)}")

    if not reasons:
        print(json.dumps({"wakeAgent": False}))
        return 0
    if not args.dry_run:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({**state, "woke_at": time.time(), "woke_reasons": reasons})
        )
        tmp.replace(state_file)
    now = dt.datetime.now(ZONE).strftime("%Y-%m-%d %H:%M %Z")
    print(
        f"Review wake gate ({now}): {len(reasons)} reason(s). The gate list at {args.gates_json} is fresh."
    )
    for r in reasons:
        print(f"- {r}")
    if held:
        print(f"- held out of the gate list until the recovery job assesses them: {', '.join(held)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - fail open: wake the agent
        print(f"Review wake gate error, waking the agent: {type(exc).__name__}: {exc}")
        sys.exit(0)
