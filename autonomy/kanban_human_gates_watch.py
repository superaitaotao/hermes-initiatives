#!/usr/bin/env python3
"""Human gates left after the Kanban cron's recovery pass.

`--gates-json <path>` (the merged recovery + review tick): write the gate list for the daily review and print
nothing. The review turns each listed card into a decision; a card that leaves the list closes its item. On
any failure the file is removed and the exit code is nonzero, so the review never reads a stale list.

Without arguments (legacy): render a change-only digest after the Kanban cron's recovery pass.

The agent-mode cron runs this script only after it has investigated and tried
safe recoveries. Stdout is the final delivery payload; empty stdout means no
change. Errors become zero-exit alerts so failure delivery stays local-only and
cannot bypass the configured proactive-notification window.

A card counts as requiring human intervention when it is:

* `blocked` — the dispatcher never retries it on its own; unblock is a human act;
* `scheduled` with a recorded parking reason — parked for a human decision;
* carrying an active `error`/`critical` diagnostic (crash/retry breaker) while
  non-terminal — automatic recovery has stopped and needs an operator;
* in `triage` after a `block_loop_detected` event — a repeated failure the
  recovery cron could not clear after bounded automatic restructuring.

`review`, `ready`, `running`, plain `todo`, and direct-intake `triage` work stay
out of scope: those are agent-owned lanes, not human gates.

Two filters keep machine waits out of the human digest:

* **Machine-wait statements.** Card comments follow the mandated format
  (`**BLOCKED — MACHINE**`, `**You:** nothing needed`). The newest statement at
  or after the gate event decides: "nothing needed" or `BLOCKED — MACHINE` drops
  the card, any other `**You:**` line or `BLOCKED — NEEDS YOU` keeps it, and an
  unformatted reason keeps it (fail toward alerting). A newer NEEDS-YOU comment
  also replaces the stored reason as the digest text.
* **Queued `ready` cards.** `stranded_in_ready` dates readiness from
  created/promoted/unblocked only, so a dashboard re-queue (`status` event) looks
  hours old. The age is re-derived from the newest re-queue, and a card is not
  stranded while running slots are full: the cap, not a missing worker, holds it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

# The plugin stays self-contained; no profile-local imports or PYTHONPATH required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autonomy.config import (  # noqa: E402
    add_config_arguments,
    board_path,
    profile_home,
    resolve_config,
)
from hermes_initiatives_core.settings import hermes_root, load_settings  # noqa: E402

BOARD_SLUG = ""
HERMES = shutil.which("hermes") or "hermes"
STATE_DIR = profile_home() / "cron/state/kanban-human-gates"
STATE_JSON = STATE_DIR / "state.json"
HISTORY_JSONL = STATE_DIR / "history.jsonl"
ERROR_LOG = STATE_DIR / "last-error.txt"
CONFIG = load_settings()[0]
TERMINAL_STATUSES = ("done", "archived")
HUMAN_DIAGNOSTIC_SEVERITIES = ("error", "critical")
MAX_ENTRIES = 8
MAX_REASON_CHARS = 220
MAX_TITLE_CHARS = 62
STRANDED_DEFAULT_THRESHOLD = 30 * 60
READY_EVENT_KINDS = ("created", "promoted", "reclaimed", "unblocked", "review_reopened")
HERMES_ROOT = hermes_root()
YOU_LINE = re.compile(r"\*\*You:\*\*\s*(?P<ask>[^\n]*)", re.IGNORECASE)
NOTHING_NEEDED = re.compile(r"^\s*nothing(\s+more)?\s+needed\b", re.IGNORECASE)
VERDICT = re.compile(r"BLOCKED\s*[—–-]+\s*(?P<who>MACHINE|NEEDS YOU)", re.IGNORECASE)
CLI_TIMEOUT = 60.0
# Human-action wording: when a stored reason is long, the clause that tells the
# reader what is being asked of them is usually near the end, so prefer it over a
# blind head truncation. Strong wording ("awaiting user merge") outranks weak
# supporting words ("merge", "review") that also appear in concluding sentences.
STRONG_ACTION_PATTERN = re.compile(
    r"\b(await\w*|please|confirm\w*|approv\w*|decision|decide|choos\w*|choose|"
    r"authoriz\w*|authoris\w*|unblock|needs|requires|required|must)\b",
    re.IGNORECASE,
)
WEAK_ACTION_PATTERN = re.compile(
    r"\b(human|merge|manual|review|input|resume|retry|verify)\b", re.IGNORECASE
)


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def collapse(text: str) -> str:
    return " ".join(text.split())


def truncate(text: str, limit: int) -> str:
    text = collapse(text)
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - 3
    return f"{text[:head]} … {text[-tail:]}"


def waiting_summary(reason: str, limit: int = MAX_REASON_CHARS) -> str:
    """Prefer the human-action clause of a long stored reason over a blind head cut."""
    text = collapse(reason)
    if len(text) <= limit:
        return text
    sentences = [part for part in re.split(r"(?<=[.;])\s+", text) if part]

    def score(index: int) -> int:
        sentence = sentences[index]
        return (3 if STRONG_ACTION_PATTERN.search(sentence) else 0) + (
            1 if WEAK_ACTION_PATTERN.search(sentence) else 0
        )

    best = max(range(len(sentences)), key=lambda index: (score(index), index))
    if score(best):
        sentence = sentences[best]
        if len(sentence) < 40 and best:
            sentence = f"{sentences[best - 1]} {sentence}"
        prefix = "" if best == 0 else "…"
        return f"{prefix}{truncate(sentence, limit - len(prefix))}"
    return truncate(text, limit)


def board_db_path() -> Path:
    override = os.environ.get("KANBAN_HUMAN_GATES_DB")
    if override:
        return Path(override)
    return board_path(BOARD_SLUG)


def latest_event_payload(conn: sqlite3.Connection, task_id: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def live_block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    """The card's latest ``blocked`` reason, unless a later ``unblocked`` cleared it.

    A card parked straight out of a blocker records the human gate in the
    ``blocked`` event; when the park itself carries no reason, that reason is
    still the outstanding prerequisite and must stay visible.
    """
    row = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'unblocked') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row or str(row["kind"]) != "blocked" or not row["payload"]:
        return ""
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict) or not payload.get("reason"):
        return ""
    return collapse(str(payload["reason"]))


def breaker_routed(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the card reached triage through the block-loop breaker."""
    row = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'block_loop_detected' LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def last_block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    """Reason from the latest ``block_loop_detected`` or ``blocked`` event, cleared or not."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('block_loop_detected', 'blocked') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row or not row[0]:
        return ""
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict) or not payload.get("reason"):
        return ""
    return collapse(str(payload["reason"]))


def active_diagnostics() -> dict[str, list[dict]]:
    result = subprocess.run(
        [HERMES, "kanban", "--board", BOARD_SLUG, "diagnostics", "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=CLI_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`hermes kanban diagnostics --json` failed (exit {result.returncode}): "
            f"{collapse(result.stderr or result.stdout)[:200]}"
        )
    grouped: dict[str, list[dict]] = {}
    for row in json.loads(result.stdout or "[]"):
        task_id = str(row.get("task_id", ""))
        if not task_id:
            continue
        for item in row.get("diagnostics", []):
            if str(item.get("severity", "")).lower() in HUMAN_DIAGNOSTIC_SEVERITIES:
                grouped.setdefault(task_id, []).append(item)
    return grouped


def latest_event_ts(conn: sqlite3.Connection, task_id: str, kinds: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(kinds))
    row = conn.execute(
        f"SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ({marks})",
        (task_id, *kinds),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def classify_statement(text: str) -> str | None:
    """``machine``/``human`` from a format-conformant statement, else ``None``."""
    verdict = VERDICT.search(text or "")
    if verdict:
        return "machine" if verdict.group("who").upper() == "MACHINE" else "human"
    ask = YOU_LINE.search(text or "")
    if ask:
        return "machine" if NOTHING_NEEDED.match(ask.group("ask")) else "human"
    return None


def gate_statement(
    conn: sqlite3.Connection, task_id: str, gate_reason: str, gate_ts: int
) -> tuple[bool, str]:
    """(machine wait, waiting text) from the newest classified statement since the gate.

    "nothing needed"/MACHINE clears the gate. A later NEEDS-YOU comment replaces the
    stored reason, because the stored reason goes stale when the operator escalates.
    Unclassified text never clears a gate (fail toward alerting)."""
    statements: list[tuple[int, int, str]] = [(gate_ts, 0, gate_reason)]
    for row in conn.execute(
        "SELECT id, created_at, body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, gate_ts),
    ):
        statements.append((int(row["created_at"]), int(row["id"]), str(row["body"] or "")))
    for _, _, text in sorted(statements, reverse=True):
        verdict = classify_statement(text)
        if verdict:
            if text is gate_reason:
                return verdict == "machine", gate_reason
            return verdict == "machine", statement_ask(text)
    return False, gate_reason


def statement_ask(text: str) -> str:
    """Headline plus ``**You:**`` line of a formatted comment; evidence (tick times,
    SHAs) is excluded so a re-posted statement cannot change the digest."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    parts = lines[:1]
    ask = YOU_LINE.search(text)
    if ask and ask.group(0).strip() not in parts:
        parts.append(ask.group(0).strip())
    return collapse(" ".join(parts))


def configured_cap() -> int | None:
    """``kanban.max_in_progress`` from the gateway's root config (no YAML dependency)."""
    try:
        lines = (HERMES_ROOT / "config.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_kanban = False
    for line in lines:
        if re.match(r"^\S", line):
            in_kanban = line.rstrip().startswith("kanban:")
            continue
        match = re.match(r"^\s+max_in_progress:\s*['\"]?(\d+)", line) if in_kanban else None
        if match:
            return int(match.group(1))
    return None


def running_capacity(conn: sqlite3.Connection) -> tuple[int, int | None]:
    """(running tasks host-wide, configured cap). The cap spans every board."""
    running = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0])
    own = Path(conn.execute("PRAGMA database_list").fetchone()[2] or "").resolve()
    for other in [HERMES_ROOT / "kanban.db", *HERMES_ROOT.glob("kanban/boards/*/kanban.db")]:
        if other.resolve() == own or not other.exists():
            continue
        try:
            peer = sqlite3.connect(f"file:{other}?mode=ro", uri=True, timeout=5)
            try:
                running += int(peer.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0])
            finally:
                peer.close()
        except sqlite3.Error:
            continue
    return running, configured_cap()


def stranded_since(conn: sqlite3.Connection, task_id: str, item: dict, capacity) -> int | None:
    """Ready-since epoch when the card is genuinely stranded, else ``None``.

    The diagnostic ignores dashboard re-queues (``status`` events), so the age is
    re-derived; and a card waiting for a free running slot is queued, not stranded.
    """
    data = item.get("data") or {}
    since = max(
        int(data.get("ready_since") or 0),
        latest_event_ts(conn, task_id, READY_EVENT_KINDS),
        latest_event_ts(conn, task_id, ("status",)),
    )
    threshold = int(data.get("threshold_seconds") or STRANDED_DEFAULT_THRESHOLD)
    if since and datetime.now(UTC).timestamp() - since < threshold:
        return None
    running, cap = capacity()
    if cap is not None and running >= cap:
        return None
    return since


def gate_event(conn: sqlite3.Connection, task_id: str, kinds: tuple[str, ...]) -> tuple[str, int]:
    """(reason, created_at) of the newest event of ``kinds`` carrying a reason."""
    marks = ",".join("?" * len(kinds))
    for row in conn.execute(
        f"SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC",
        (task_id, *kinds),
    ):
        try:
            payload = json.loads(row["payload"] or "null")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("reason"):
            return collapse(str(payload["reason"])), int(row["created_at"] or 0)
    return "", 0


def human_gate(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[list[str], list[str], str, int]:
    """(signals, digest signals, waiting reason, gate event ts) for a status-based gate."""
    task_id = str(row["id"])
    status = str(row["status"])
    if status == "blocked":
        kind = row["block_kind"] or "unspecified"
        recurrences = row["block_recurrences"] or 0
        reason, ts = gate_event(conn, task_id, ("blocked",))
        signal = f"block kind: {kind}" + (
            f" (re-blocked x{recurrences})" if recurrences and recurrences > 1 else "")
        # Digest ignores counts so a growing crash/re-block tally cannot re-ping.
        return [signal], [f"blocked:{kind}"], reason, ts
    if status == "scheduled":
        payload = latest_event_payload(conn, task_id, "scheduled")
        if payload and payload.get("reason"):
            reason, ts = gate_event(conn, task_id, ("scheduled",))
        else:
            # A park without a reason still hides a real gate when it came straight
            # out of a blocker that no later unblock cleared.
            reason = live_block_reason(conn, task_id)
            ts = latest_event_ts(conn, task_id, ("blocked",)) if reason else 0
        if not reason:
            return [], [], "", 0  # time-based wait, not a human gate
        return ["parked with an outstanding prerequisite"], ["parked"], reason, ts
    if status == "triage" and breaker_routed(conn, task_id):
        # Breaker-routed triage is a repeated failure; direct-triage intake stays agent work.
        reason, ts = gate_event(conn, task_id, ("block_loop_detected", "blocked"))
        return (["repeated failure: block loop detected"], ["triage:block_loop_detected"],
                live_block_reason(conn, task_id) or reason, ts)
    return [], [], "", 0


def collect(db_path: Path) -> list[dict[str, object]]:
    diagnostics = active_diagnostics()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    capacity_memo: list[tuple[int, int | None]] = []

    def capacity() -> tuple[int, int | None]:
        if not capacity_memo:
            capacity_memo.append(running_capacity(conn))
        return capacity_memo[0]

    try:
        rows = conn.execute(
            "SELECT id, title, status, assignee, block_kind, block_recurrences, "
            "consecutive_failures FROM tasks WHERE status NOT IN (?, ?)",
            TERMINAL_STATUSES,
        ).fetchall()

        entries: list[dict[str, object]] = []
        for row in rows:
            task_id = str(row["id"])
            signals, digest_signals, waiting, gate_ts = human_gate(conn, row)
            if signals:
                machine, waiting = gate_statement(conn, task_id, waiting, gate_ts)
                if machine:
                    signals, digest_signals, waiting = [], [], ""
            gate_waiting = waiting

            for item in diagnostics.get(task_id, []):
                kind = str(item.get("kind", "unknown"))
                if kind == "stranded_in_ready" and stranded_since(
                        conn, task_id, item, capacity) is None:
                    continue
                signals.append(f"stalled: {kind} x{item.get('count', '?')}")
                digest_signals.append(f"stalled:{kind}")
                if not waiting:
                    waiting = collapse(str(item.get("title", "")))

            if not signals:
                continue

            entries.append(
                {
                    "id": task_id,
                    "title": str(row["title"] or ""),
                    "status": str(row["status"]),
                    "assignee": str(row["assignee"] or "unassigned"),
                    "signals": sorted(signals),
                    "digest_signals": sorted(digest_signals),
                    "waiting": waiting,
                    # Diagnostic titles carry growing ages/counts; only the stored
                    # gate reason may change the digest.
                    "digest_waiting": gate_waiting,
                }
            )
        entries.sort(key=lambda entry: (str(entry["status"]), str(entry["id"])))
        return entries
    finally:
        conn.close()


def digest_for(entries: list[dict[str, object]]) -> str:
    stable = [
        {
            "id": entry["id"],
            "status": entry["status"],
            "signals": entry.get("digest_signals") or entry["signals"],
            "waiting": waiting_summary(str(entry.get("digest_waiting", entry["waiting"]))),
        }
        for entry in entries
    ]
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def render(entries: list[dict[str, object]], previous: dict[str, dict]) -> str:
    added = [entry["id"] for entry in entries if str(entry["id"]) not in previous]
    resolved = [task_id for task_id in previous if task_id not in {e["id"] for e in entries}]

    if not entries:
        lines = [
            "✅ **Kanban remaining gates — all clear**",
            f"Board {BOARD_SLUG}: no blocked or parked task remains.",
        ]
        if resolved:
            lines.append("Resolved: " + ", ".join(sorted(str(item) for item in resolved)))
        return "\n".join(lines)

    header = f"📋 **Kanban remaining gates — {len(entries)} blocked/parked** ({BOARD_SLUG})"
    if added or resolved:
        header += f" · {len(added)} new · {len(resolved)} resolved"
    lines = [header, ""]

    for entry in entries[:MAX_ENTRIES]:
        flags = ", ".join(str(signal) for signal in entry["signals"])
        marker = " · NEW" if str(entry["id"]) in added else ""
        lines.append(
            f"• **{entry['id']}**{marker} — "
            f"{truncate(str(entry['title']), MAX_TITLE_CHARS)}"
        )
        lines.append(f"   Status: {entry['status']} ({flags})")
        if entry["waiting"]:
            lines.append(f"   Waiting: {waiting_summary(str(entry['waiting']))}")
    if len(entries) > MAX_ENTRIES:
        lines.append(f"…and {len(entries) - MAX_ENTRIES} more on the board.")

    if resolved:
        lines.append("")
        lines.append("Resolved: " + ", ".join(sorted(str(item) for item in resolved)))

    lines.extend(
        [
            "",
            f"Inspect: hermes kanban --board {BOARD_SLUG} show <task-id>",
        ]
    )
    return "\n".join(lines)


def in_allowed_window(now: datetime) -> bool:
    return now.astimezone(CONFIG.zone()).hour in CONFIG.push_hours()


def emit(message: str) -> None:
    """Print the delivery payload; stdout is the whole contract for a no_agent job."""
    print(message)


def run() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    db_path = board_db_path()
    entries = collect(db_path)
    digest = digest_for(entries)
    current = {str(entry["id"]): entry for entry in entries}

    state = load_json(STATE_JSON, {})
    if not isinstance(state, dict):
        state = {}
    notified_digest = state.get("notified_digest")
    previous = state.get("entries")
    if not isinstance(previous, dict):
        previous = {}

    changed = digest != notified_digest
    allowed = in_allowed_window(now)
    emitted = False
    if changed and allowed:
        emit(render(entries, previous))
        emitted = True

    save_json(
        STATE_JSON,
        {
            "notified_digest": digest if emitted else notified_digest,
            "notified_at": (now.isoformat() if emitted else state.get("notified_at")),
            "entries": current if emitted else previous,
            "last_seen_digest": digest,
            "last_seen_at": now.isoformat(),
            "last_seen_count": len(entries),
            "pending_digest": (None if emitted else (digest if changed else state.get("pending_digest"))),
        },
    )
    with HISTORY_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at": now.isoformat(),
                    "event": ("emitted" if emitted else ("pending" if changed else "unchanged")),
                    "allowed_window": allowed,
                    "count": len(entries),
                    "task_ids": sorted(current),
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    ERROR_LOG.unlink(missing_ok=True)
    return 0


def write_gates(path: Path) -> int:
    """Write ``{generated_at, board, cards}`` atomically. Quiet hours do not apply: the review decides delivery."""
    try:
        entries = collect(board_db_path())
        body = {
            "generated_at": datetime.now(UTC).isoformat(),
            "board": BOARD_SLUG,
            "cards": [
                {
                    "id": entry["id"],
                    "title": entry["title"],
                    "status": entry["status"],
                    "assignee": entry["assignee"],
                    "signals": entry["signals"],
                    "waiting": entry["waiting"],
                }
                for entry in entries
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(path.suffix + ".tmp")
        scratch.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
        scratch.replace(path)
        return 0
    except Exception as exc:  # noqa: BLE001 - the review must see a missing list, never an old one
        path.unlink(missing_ok=True)
        sys.stderr.write(f"gate list failed: {type(exc).__name__}: {exc}\n")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gates-json", type=Path, help="write the gate list here instead of printing a digest")
    add_config_arguments(parser)
    args = parser.parse_args(argv)
    try:
        cfg = resolve_config(project=args.project, board=args.board)
    except (ValueError, OSError) as exc:
        if args.gates_json:
            args.gates_json.unlink(missing_ok=True)
        print(f"gate list failed: {exc}", file=sys.stderr)
        return 1
    global BOARD_SLUG, CONFIG, STATE_DIR, STATE_JSON, HISTORY_JSONL, ERROR_LOG
    BOARD_SLUG, CONFIG = cfg.board, cfg.settings
    STATE_DIR = Path(os.environ.get("KANBAN_HUMAN_GATES_STATE_DIR") or profile_home() / "cron/state" / f"human-gates-{cfg.board}")
    STATE_JSON, HISTORY_JSONL, ERROR_LOG = (STATE_DIR / n for n in ("state.json", "history.jsonl", "last-error.txt"))
    if args.gates_json:
        return write_gates(args.gates_json)
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - monitoring failures must surface as alerts
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        detail = traceback.format_exc(limit=3)
        ERROR_LOG.write_text(
            f"{datetime.now(UTC).isoformat()} {type(exc).__name__}: {exc}\n{detail}",
            encoding="utf-8",
        )
        state = load_json(STATE_JSON, {})
        if not isinstance(state, dict):
            state = {}
        digest = hashlib.sha256(f"error:{type(exc).__name__}:{exc}".encode()).hexdigest()
        if digest != state.get("notified_digest") and in_allowed_window(datetime.now(UTC)):
            emit(
                "\n".join(
                    [
                        "KANBAN REMAINING GATES — monitor failing",
                        f"Board {BOARD_SLUG}: could not read remaining gates "
                        f"({type(exc).__name__}: {truncate(str(exc), 200)}).",
                        "Post-recovery alerts are silent until this is fixed.",
                        f"Detail: {ERROR_LOG}",
                    ]
                )
            )
            state["notified_digest"] = digest
            state["notified_at"] = datetime.now(UTC).isoformat()
        state["last_error_at"] = datetime.now(UTC).isoformat()
        save_json(STATE_JSON, state)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
