"""Kanban health metrics for one board: delivery, retries, failures, review rounds and token use, bucketed in time.

Read-only against the board database (``mode=ro``) and each lane profile's ``state.db``. The only write is the
hourly board snapshot in ``<plugin data>/metrics.db``: the board keeps current state only, so the page records
what it saw (status counts, blocked cards, waiting on you) to build that history going forward.

Every number is timestamped: buckets are local-time days or ISO weeks with explicit ``start``/``end``, the window is
half-open ``[start, end)``, and the totals of the preceding equal window ride beside the current ones.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any

from .kanban import _connect, board_path
from .model import PR_URL
from .settings import data_dir, hermes_root

# Run outcomes, grouped by what they mean for the card. "parked" is a deliberate stop, not a failure.
PRODUCTIVE = ("completed", "review_requested", "handed_off", "changes_requested")
PARKED = ("blocked", "scheduled")
FAILED = ("rate_limited", "crashed", "timed_out", "gave_up", "reclaimed", "spawn_failed")
# A crashed run whose metadata says the worker exited without a terminal Kanban call: counted under its own cause.
PROTOCOL = "protocol_violation"
CAUSES = ("rate_limited", PROTOCOL, "crashed", "timed_out", "gave_up", "reclaimed", "spawn_failed")
DONE = ("done", "archived")
OPEN_STATUSES = ("triage", "todo", "ready", "claimed", "running", "review", "blocked", "scheduled")
WINDOWS = (7, 14, 30, 90)
SNAPSHOT_EVERY_S = 3600
SNAPSHOT_LIMIT = 24 * 120


def _local_tz() -> dt.tzinfo:
    return dt.datetime.now().astimezone().tzinfo or dt.UTC


def _iso(ts: float, tz: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(ts, tz=tz).isoformat(timespec="seconds")


def _buckets(end: dt.datetime, days: int, bucket: str) -> list[tuple[float, float]]:
    """Half-open local-time buckets covering ``days`` up to ``end``. The last bucket is partial (ends at ``end``)."""
    midnight = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        first = midnight - dt.timedelta(days=days - 1)
        first -= dt.timedelta(days=first.weekday())
        step = dt.timedelta(days=7)
    else:
        first = midnight - dt.timedelta(days=days - 1)
        step = dt.timedelta(days=1)
    out = []
    at = first
    while at < end:
        nxt = at + step
        out.append((at.timestamp(), min(nxt, end).timestamp()))
        at = nxt
    return out


def _index(buckets: list[tuple[float, float]], ts: float | None) -> int | None:
    if ts is None:
        return None
    for i, (lo, hi) in enumerate(buckets):
        if lo <= ts < hi:
            return i
    return None


def _meta(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _cause(outcome: str, meta: dict[str, Any]) -> str:
    return PROTOCOL if outcome == "crashed" and meta.get("protocol_violation") else outcome


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def _lane_db(profile: str) -> Path:
    root = hermes_root()
    return root / "state.db" if profile in ("", "default") else root / "profiles" / profile / "state.db"


def _tokens(profiles: list[str], lo: float, hi: float) -> tuple[list[dict[str, Any]], list[str]]:
    """Kanban worker sessions per lane started in ``[lo, hi)``: ``[{profile, started_at, fresh, cache}]``."""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for profile in profiles:
        path = _lane_db(profile)
        if not path.is_file():
            missing.append(profile)
            continue
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                for started, fresh, cache in conn.execute(
                    "SELECT started_at, COALESCE(input_tokens,0) + COALESCE(output_tokens,0), "
                    "COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0) "
                    "FROM sessions WHERE source = 'kanban' AND started_at >= ? AND started_at < ?",
                    (lo, hi),
                ):
                    rows.append({"profile": profile, "started_at": float(started), "fresh": int(fresh), "cache": int(cache)})
            finally:
                conn.close()
        except sqlite3.Error:
            missing.append(profile)
    return rows, missing


def _empty_bucket(lo: float, hi: float, tz: dt.tzinfo) -> dict[str, Any]:
    return {
        "start": _iso(lo, tz),
        "end": _iso(hi, tz),
        "created": 0,
        "delivered": 0,
        "delivered_pr": 0,
        "lead_p50_h": None,
        "lead_p90_h": None,
        "runs": 0,
        "runs_ok": 0,
        "runs_parked": 0,
        "runs_failed": 0,
        "failed_by_cause": {cause: 0 for cause in CAUSES},
        "run_hours": 0.0,
        "failed_hours": 0.0,
        "reviews": 0,
        "changes_requested": 0,
        "gave_up": 0,
        "respawn_guarded": 0,
        "sessions": 0,
        "tokens_fresh": 0,
        "tokens_cache": 0,
        "_leads": [],
    }


def _window(conn: sqlite3.Connection, lo: float, hi: float, buckets: list[tuple[float, float]], tz: dt.tzinfo) -> dict[str, Any]:
    """Every series and total for ``[lo, hi)``; ``buckets`` partition the same range."""
    series = [_empty_bucket(b_lo, b_hi, tz) for b_lo, b_hi in buckets]
    lanes: dict[str, dict[str, Any]] = {}

    def lane(profile: str | None) -> dict[str, Any]:
        key = profile or "(none)"
        return lanes.setdefault(
            key,
            {
                "profile": key,
                "runs": 0,
                "runs_failed": 0,
                "run_hours": 0.0,
                "failed_hours": 0.0,
                "sessions": 0,
                "tokens_fresh": 0,
                "tokens_cache": 0,
            },
        )

    for (created,) in conn.execute("SELECT created_at FROM tasks WHERE created_at >= ? AND created_at < ?", (lo, hi)):
        i = _index(buckets, created)
        if i is not None:
            series[i]["created"] += 1

    done = conn.execute(
        f"SELECT id, created_at, completed_at, completion_contract FROM tasks WHERE status IN ({','.join('?' * len(DONE))}) "
        "AND completed_at >= ? AND completed_at < ?",
        (*DONE, lo, hi),
    ).fetchall()
    done_ids = [row["id"] for row in done]
    with_pr: set[str] = set()
    runs_per: dict[str, int] = {}
    reviewed: set[str] = set()
    rounds: dict[str, int] = {}
    if done_ids:
        marks = ",".join("?" * len(done_ids))
        for task_id, meta in conn.execute(f"SELECT task_id, metadata FROM task_runs WHERE task_id IN ({marks})", done_ids):
            runs_per[task_id] = runs_per.get(task_id, 0) + 1
            if isinstance(_meta(meta).get("published_pr"), str) and PR_URL.search(_meta(meta)["published_pr"]):
                with_pr.add(task_id)
        for task_id, kind in conn.execute(
            f"SELECT task_id, kind FROM task_events WHERE task_id IN ({marks}) "
            "AND kind IN ('review_requested', 'changes_requested')",
            done_ids,
        ):
            reviewed.add(task_id)
            if kind == "changes_requested":
                rounds[task_id] = rounds.get(task_id, 0) + 1
    leads: list[float] = []
    for row in done:
        if isinstance(row["completion_contract"], str) and PR_URL.search(row["completion_contract"]):
            with_pr.add(row["id"])
        i = _index(buckets, row["completed_at"])
        if i is None:
            continue
        series[i]["delivered"] += 1
        series[i]["delivered_pr"] += row["id"] in with_pr
        if row["created_at"]:
            hours = (row["completed_at"] - row["created_at"]) / 3600
            series[i]["_leads"].append(hours)
            leads.append(hours)

    # Runs are placed in the bucket where they started; hours are clipped to the window, not to the bucket.
    for profile, outcome, started, ended, meta in conn.execute(
        "SELECT profile, outcome, started_at, ended_at, metadata FROM task_runs WHERE started_at >= ? AND started_at < ?",
        (lo, hi),
    ):
        i = _index(buckets, started)
        if i is None:
            continue
        b, ln = series[i], lane(profile)
        hours = max(0.0, (min(ended or hi, hi) - started) / 3600) if ended else 0.0
        b["runs"] += 1
        ln["runs"] += 1
        b["run_hours"] += hours
        ln["run_hours"] += hours
        if outcome in FAILED:
            cause = _cause(outcome, _meta(meta))
            b["runs_failed"] += 1
            b["failed_by_cause"][cause] = b["failed_by_cause"].get(cause, 0) + 1
            b["failed_hours"] += hours
            ln["runs_failed"] += 1
            ln["failed_hours"] += hours
        elif outcome in PARKED:
            b["runs_parked"] += 1
        elif outcome in PRODUCTIVE:
            b["runs_ok"] += 1

    for kind, created in conn.execute(
        "SELECT kind, created_at FROM task_events WHERE created_at >= ? AND created_at < ? "
        "AND kind IN ('review_requested', 'changes_requested', 'gave_up', 'respawn_guarded')",
        (lo, hi),
    ):
        i = _index(buckets, created)
        if i is None:
            continue
        key = "reviews" if kind == "review_requested" else str(kind)
        series[i][key] += 1

    profiles = sorted(
        {
            p
            for (p,) in conn.execute("SELECT DISTINCT profile FROM task_runs WHERE started_at >= ? AND started_at < ?", (lo, hi))
            if p
        }
    )
    sessions, missing = _tokens(profiles, lo, hi)
    for session in sessions:
        i = _index(buckets, session["started_at"])
        if i is None:
            continue
        b, ln = series[i], lane(session["profile"])
        for target in (b, ln):
            target["sessions"] += 1
            target["tokens_fresh"] += session["fresh"]
            target["tokens_cache"] += session["cache"]

    for b in series:
        b["lead_p50_h"] = _round(statistics.median(b["_leads"]) if b["_leads"] else None)
        b["lead_p90_h"] = _round(_pct(b["_leads"], 0.9))
        del b["_leads"]
        b["run_hours"] = round(b["run_hours"], 2)
        b["failed_hours"] = round(b["failed_hours"], 2)
    for ln in lanes.values():
        ln["run_hours"] = round(ln["run_hours"], 2)
        ln["failed_hours"] = round(ln["failed_hours"], 2)
        ln["failed_rate"] = _ratio(ln["runs_failed"], ln["runs"])

    def total(key: str) -> float:
        return sum(b[key] for b in series)

    delivered = int(total("delivered"))
    fresh = int(total("tokens_fresh"))
    first_pass = [task_id for task_id in done_ids if task_id in reviewed]
    totals = {
        "created": int(total("created")),
        "delivered": delivered,
        "delivered_pr": int(total("delivered_pr")),
        "lead_p50_h": _round(statistics.median(leads) if leads else None),
        "lead_p90_h": _round(_pct(leads, 0.9)),
        "runs": int(total("runs")),
        "runs_failed": int(total("runs_failed")),
        "failed_rate": _ratio(total("runs_failed"), total("runs")),
        "run_hours": round(total("run_hours"), 1),
        "failed_hours": round(total("failed_hours"), 1),
        "failed_by_cause": {cause: sum(b["failed_by_cause"].get(cause, 0) for b in series) for cause in CAUSES},
        "runs_per_delivery_p50": _round(statistics.median(runs_per.get(i, 0) for i in done_ids) if done_ids else None),
        "runs_per_delivery_p90": _pct([runs_per.get(i, 0) for i in done_ids], 0.9),
        "reviewed_deliveries": len(first_pass),
        "first_pass_rate": _ratio(sum(1 for i in first_pass if not rounds.get(i)), len(first_pass)),
        "review_rounds_p90": _pct([rounds.get(i, 0) for i in first_pass], 0.9),
        "changes_requested": int(total("changes_requested")),
        "gave_up": int(total("gave_up")),
        "respawn_guarded": int(total("respawn_guarded")),
        "sessions": int(total("sessions")),
        "tokens_fresh": fresh,
        "tokens_cache": int(total("tokens_cache")),
        "tokens_fresh_per_delivery": round(fresh / delivered) if delivered else None,
    }
    lane_list = sorted(lanes.values(), key=lambda ln: (-ln["tokens_fresh"], -ln["runs"], ln["profile"]))
    return {"series": series, "totals": totals, "lanes": lane_list, "token_profiles": profiles, "token_missing": missing}


def _now(conn: sqlite3.Connection, now: float, tz: dt.tzinfo) -> dict[str, Any]:
    counts = {status: n for status, n in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}
    blocked = conn.execute(
        "SELECT t.id, t.title, t.block_kind, t.assignee, "
        "(SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = t.id AND e.kind = 'blocked') AS since "
        "FROM tasks t WHERE t.status = 'blocked'"
    ).fetchall()
    by_kind: dict[str, int] = {}
    oldest = []
    for row in blocked:
        kind = row["block_kind"] or "unspecified"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if row["since"]:
            oldest.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "kind": kind,
                    "assignee": row["assignee"],
                    "since": _iso(row["since"], tz),
                    "age_h": round((now - row["since"]) / 3600, 1),
                }
            )
    oldest.sort(key=lambda item: -item["age_h"])
    return {
        "at": _iso(now, tz),
        "counts": counts,
        "open": sum(counts.get(status, 0) for status in OPEN_STATUSES),
        "blocked_by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "needs_you": by_kind.get("needs_input", 0),
        "oldest_blocked": oldest[:8],
    }


class SnapshotStore:
    """Hourly board snapshots in ``<plugin data>/metrics.db``: the only history the board does not keep itself."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "metrics.db"
        with self._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS snapshots (board TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS snapshots_board_at ON snapshots (board, at)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    def record(self, board: str, at: float, now: dict[str, Any]) -> bool:
        """Store one snapshot unless the newest one for ``board`` is under an hour old."""
        data = {
            "open": now["open"],
            "counts": now["counts"],
            "blocked_by_kind": now["blocked_by_kind"],
            "needs_you": now["needs_you"],
        }
        with self._conn() as conn:
            last = conn.execute("SELECT MAX(at) FROM snapshots WHERE board = ?", (board,)).fetchone()[0]
            if last is not None and at - last < SNAPSHOT_EVERY_S:
                return False
            conn.execute(
                "INSERT INTO snapshots (board, at, data) VALUES (?, ?, ?)", (board, at, json.dumps(data, sort_keys=True))
            )
        return True

    def since(self, board: str, lo: float, tz: dt.tzinfo) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT at, data FROM snapshots WHERE board = ? AND at >= ? ORDER BY at LIMIT ?", (board, lo, SNAPSHOT_LIMIT)
            ).fetchall()
        out = []
        for at, data in rows:
            item = json.loads(data)
            counts = item.get("counts", {})
            out.append(
                {
                    "at": _iso(at, tz),
                    "open": item.get("open", 0),
                    "blocked": counts.get("blocked", 0),
                    "needs_you": item.get("needs_you", 0),
                    "running": counts.get("running", 0) + counts.get("claimed", 0),
                    "ready": counts.get("ready", 0),
                }
            )
        return out


NOTES = [
    "Buckets are local-time days or ISO weeks; the newest bucket is partial and ends at the generated time.",
    "Delivered = cards that reached done in the bucket; 'with PR' = a PR URL is recorded on the card. "
    "Merge state is not re-read from GitHub.",
    "Lead time = card creation to done. It mixes card sizes and backlog age, so a shift is a prompt to look, not proof of speed.",
    "Runs are counted in the bucket where they started; failed = rate limited, crashed, protocol violation, timed out, "
    "gave up, reclaimed, spawn failed. Blocked and scheduled runs are parked, not failed.",
    "An OOM kill shows as crashed or protocol violation: the board does not record the kernel's reason.",
    "First pass = reviewed cards delivered in the window with no changes requested over their whole life.",
    "Tokens come from the Kanban sessions of each lane that ran on this board, by session start. A lane shared with "
    "another board counts its sessions there too. Cache is read + write; fresh is input + output.",
]


def board_metrics(
    board: str | None,
    days: int = 30,
    bucket: str | None = None,
    *,
    path: Path | None = None,
    now: float | None = None,
    snapshots: SnapshotStore | None = None,
) -> dict[str, Any]:
    """Metrics for ``board`` over the last ``days``. Raises ``FileNotFoundError``/``sqlite3.Error`` when unreadable."""
    if days not in WINDOWS:
        raise ValueError(f"days must be one of {', '.join(map(str, WINDOWS))}")
    bucket = bucket or ("week" if days > 30 else "day")
    if bucket not in ("day", "week"):
        raise ValueError("bucket must be day or week")
    if not board and path is None:
        raise FileNotFoundError("no Kanban board configured")
    tz = _local_tz()
    at = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now().timestamp(), tz=tz)
    buckets = _buckets(at, days, bucket)
    lo, hi = buckets[0][0], at.timestamp()
    prev_lo = lo - (hi - lo)
    conn = _connect(path or board_path(str(board)))
    try:
        current = _window(conn, lo, hi, buckets, tz)
        previous = _window(conn, prev_lo, lo, [(prev_lo, lo)], tz)
        state = _now(conn, hi, tz)
    finally:
        conn.close()
    history: list[dict[str, Any]] = []
    recorded = False
    if snapshots is not None and board:
        recorded = snapshots.record(board, hi, state)
        history = snapshots.since(board, lo, tz)
    return {
        "board": board,
        "generated_at": _iso(hi, tz),
        "timezone": at.tzname() or str(tz),
        "utc_offset": at.strftime("%z"),
        "window": {"start": _iso(lo, tz), "end": _iso(hi, tz), "days": days, "bucket": bucket},
        "previous": {"start": _iso(prev_lo, tz), "end": _iso(lo, tz), "totals": previous["totals"]},
        "totals": current["totals"],
        "series": current["series"],
        "lanes": current["lanes"],
        "causes": list(CAUSES),
        "now": state,
        "history": history,
        "snapshot_recorded": recorded,
        "token_profiles": current["token_profiles"],
        "token_missing": current["token_missing"],
        "notes": NOTES,
    }
