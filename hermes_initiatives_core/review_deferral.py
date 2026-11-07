"""Bounded cycle-stage authority and recoverable agent parking."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import review_collect, review_learn, reviews
from .docs import Document, atomic_write, dump_markdown, parse_markdown
from .model import CYCLE_ID, STAGES
from .review_exec import ActionFailed
from .settings import ProjectSettings
from .store import load
from .views import build_detail

RULE = "defer_exploring_cycle"
ARGS = {"authority_decision", "authority_sha256", "source_sha256"}
HASH = re.compile(r"[0-9a-f]{64}")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def user_rows(fm: dict) -> list[dict]:
    rows = fm.get("decisions", [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("malformed decisions")
    users = [r for r in rows if r.get("by") == "user"]
    for row in users:
        if not isinstance(row.get("decision"), str) or not row["decision"].strip():
            raise ValueError("malformed user decision")
        dt.date.fromisoformat(str(row.get("date")))
    return users


def instruction(row: dict, cycle: str) -> dict:
    obj = row.get("review_deferral")
    if not isinstance(obj, dict) or set(obj) != {"schema_version", "rule", "purpose", "cycle", "prerequisite"}:
        raise ValueError("missing or malformed review_deferral")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise ValueError("unsupported review_deferral version")
    if obj["rule"] != RULE or obj["purpose"] != "routine_prerequisite_wait" or obj["cycle"] != cycle:
        raise ValueError("wrong review_deferral authority")
    pre = obj["prerequisite"]
    if not isinstance(pre, dict) or set(pre) != {"kind", "cycle", "equals"}:
        raise ValueError("malformed prerequisite")
    if pre["kind"] != "cycle_stage" or pre["equals"] not in ("shipping", "measuring", "concluded"):
        raise ValueError("unsupported prerequisite")
    if not isinstance(pre["cycle"], str) or not CYCLE_ID.fullmatch(pre["cycle"]) or pre["cycle"] == cycle:
        raise ValueError("self or invalid prerequisite cycle")
    return pre


def fresh_doc(project: ProjectSettings, snapshot, cycle: str) -> Document:
    doc = snapshot.cycles.get(cycle)
    match = CYCLE_ID.fullmatch(cycle)
    if doc is None or match is None:
        raise ValueError("missing prerequisite or target cycle")
    expected = project.root / match.group(1) / "cycles" / f"{cycle}.md"
    if doc.path != expected or doc.path.resolve() != expected.absolute() or not doc.path.resolve().is_relative_to(
        project.root.resolve()
    ):
        raise ValueError("unsafe cycle path")
    current = parse_markdown(doc.path.read_text(encoding="utf-8"), doc.path)
    owner = snapshot.cycle_initiative.get(cycle)
    if current.frontmatter.get("id") != cycle or owner != match.group(1):
        raise ValueError("invalid cycle identity or ownership")
    initiative = snapshot.initiatives.get(owner)
    if initiative is None or initiative.path.resolve() != (project.root / owner / "initiative.md").absolute():
        raise ValueError("invalid initiative identity or path")
    fm = parse_markdown(initiative.path.read_text(encoding="utf-8"), initiative.path).frontmatter
    if fm.get("id") != owner or fm.get("status") != "active":
        raise ValueError("initiative is not active")
    user_rows(fm)
    return current


def prerequisite_state(project: ProjectSettings, pre: dict, target: str) -> dict:
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise ValueError("ambiguous or malformed sources")
    seen = {target}
    cycle = pre["cycle"]
    first = None
    while True:
        if cycle in seen:
            raise ValueError("cyclic prerequisite")
        seen.add(cycle)
        doc = fresh_doc(project, snapshot, cycle)
        fm = doc.frontmatter
        rows = user_rows(fm)
        stage = fm.get("stage")
        if not rows or stage not in STAGES or stage == "abandoned":
            raise ValueError("prerequisite needs a user decision and valid non-abandoned stage")
        if first is None:
            first = {"cycle": cycle, "stage": stage, "source_sha256": digest(doc.path.read_bytes())}
        if "review_deferral" not in rows[-1]:
            return first
        cycle = instruction(rows[-1], cycle)["cycle"]


def _routine_text(value: Any, patterns: tuple[str, ...], *, empty: bool = False) -> bool:
    if value in (None, ""):
        return empty
    return isinstance(value, str) and any(re.fullmatch(p, value.strip(), re.IGNORECASE) for p in patterns)


def intent(doc: Document, item: dict, authority: dict, pre: dict) -> None:
    cid, dep, stage = re.escape(item["cycle"]), re.escape(pre["cycle"]), re.escape(pre["equals"])
    if not _routine_text(authority["decision"], (
        rf"Park the review of {cid} until {dep} reaches {stage}\.",
    )):
        raise ValueError("ambiguous source intent or review-bar decision")
    fm = doc.frontmatter
    allowed = {"id", "title", "stage", "decisions", "kanban_cards", "excluded_cards", "change_register",
               "measurement", "result", "disposition", "next_action", "question", "hypothesis"}
    if set(fm) - allowed or fm.get("change_register") or fm.get("excluded_cards"):
        raise ValueError("unknown or conflicting source intent fields")
    if not _routine_text(fm.get("title"), (rf"Cycle {cid}", "Routine prerequisite wait")):
        raise ValueError("ambiguous source title")
    if len(user_rows(fm)) != 1:
        raise ValueError("earlier target decisions leave shared intent ambiguous")
    for field in ("next_action", "question", "hypothesis"):
        if not _routine_text(fm.get(field), (
            rf"Wait for {dep} to reach {stage}\.", r"Routine prerequisite wait\.",
        ), empty=True):
            raise ValueError(f"ambiguous source intent in {field}")
    if not _routine_text(doc.body, (r"Body\.", r"Routine prerequisite wait\."), empty=True):
        raise ValueError("ambiguous source body or review-bar ask")
    if set(authority) - {"date", "by", "decision", "review_deferral"}:
        raise ValueError("unknown authority intent fields")
    safe_controls = {
        "measurement": {},
        "result": {"verdict": "unset", "evidence": []},
        "disposition": {"action": "unset", "follow_ups": []},
    }
    for field, safe in safe_controls.items():
        value = fm.get(field, {})
        if not isinstance(value, dict) or set(value) - set(safe) or any(value[k] != safe[k] for k in value):
            raise ValueError(f"ambiguous nested source intent or review-bar ask in {field}")
    version = item["version"]
    for field in ("question", "context"):
        if not _routine_text(version[field], (r"Park the review\?", r"Routine wait\."), empty=True):
            raise ValueError("ambiguous options intent")
    for option in version["options"]:
        if option["action"] != "defer" or option["args"] != {"kind": "state_change", "prerequisite": pre}:
            raise ValueError("review-bar option or mismatched prerequisite")
        if not _routine_text(option["label"], ("Park the review", "Keep the review parked")):
            raise ValueError("ambiguous option label or review-bar ask")


@dataclass(frozen=True)
class Wait:
    item: dict
    doc: Document
    authority: dict
    prerequisite: dict
    readback: dict
    source_sha256: str
    initiative_authority_sha256: str


def eligible(conn, store: reviews.ReviewStore, project: ProjectSettings, sid: str, args: dict, why: str) -> Wait:
    if not isinstance(why, str) or len(why.strip()) < 20:
        raise ValueError("why must explain the approved fact")
    if not isinstance(args, dict) or set(args) != ARGS:
        raise ValueError("args must contain exactly authority_decision, authority_sha256, source_sha256")
    if any(not isinstance(args[k], str) or not HASH.fullmatch(args[k]) for k in ("authority_sha256", "source_sha256")):
        raise ValueError("invalid authority or source SHA-256")
    state = conn.execute("SELECT state FROM rule_states WHERE rule = ? ORDER BY id DESC LIMIT 1", (RULE,)).fetchone()
    if state and state[0] == "suspended":
        raise ValueError("rule is suspended")
    row = conn.execute("SELECT * FROM items WHERE short_id = ?", (sid,)).fetchone()
    if row is None:
        raise ValueError("unknown item")
    item = store._item(conn, row)
    if (item["source"], item["section"], item["status"], item["project"]) != ("cycle", "gated", "open", project.id):
        raise ValueError("only current open cycle/gated items in the configured project qualify")
    cid = item["cycle"]
    if not isinstance(cid, str) or not CYCLE_ID.fullmatch(cid) or item["key"] != f"cycle:{project.id}:{cid}:gate":
        raise ValueError("invalid item cycle identity")
    basis = item["basis"]
    if basis.get("cycle") != cid or basis.get("stage") != "exploring" or basis.get("reasons") != ["exploring"]:
        raise ValueError("basis must be exploring-only")
    if item["urgent_rule"] or conn.execute("SELECT 1 FROM answers WHERE short_id = ?", (sid,)).fetchone():
        raise ValueError("urgency or historical answers prevent parking")
    if conn.execute("SELECT 1 FROM agent_deferral_ops WHERE short_id = ? AND state != 'completed'", (sid,)).fetchone():
        raise ValueError("unresolved operation")
    if not reviews.current(item) or sid in {i["short_id"] for i in store.needs_options()}:
        raise ValueError("current options required")
    version = item["version"]
    chosen = [o for o in version["options"] if o["id"] == version["recommend"]]
    if len(chosen) != 1 or chosen[0]["action"] != "defer" or chosen[0]["args"].get("kind") != "state_change":
        raise ValueError("unique explicit state_change defer recommendation required")
    if review_learn.signature(item, chosen[0]) != "cycle:exploring:defer:state_change":
        raise ValueError("wrong signature")
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise ValueError("malformed or duplicate sources")
    doc = fresh_doc(project, snapshot, cid)
    owner = snapshot.cycle_initiative[cid]
    if item["initiative"] != owner or item["owner_profile"] is not None or item["owner_job"] is not None:
        raise ValueError("invalid item ownership")
    fm = doc.frontmatter
    if fm.get("stage") != "exploring" or fm.get("kanban_cards") or item["blocks"]:
        raise ValueError("target must be exploring without cards")
    if (fm.get("result") or {}).get("verdict", "unset") != "unset" or (
        fm.get("disposition") or {}
    ).get("action", "unset") != "unset":
        raise ValueError("verdict or disposition is a review-bar decision")
    detail = build_detail(project, owner)
    view = next((c for c in detail["cycles"] if c["id"] == cid), None) if detail else None
    if view is None or view["cards"] or (project.kanban_board and detail["freshness"]["kanban"] != "fresh"):
        raise ValueError("derived cards or unavailable card source")
    candidate = next(
        (c for c in review_collect._cycle_gates(project, snapshot, dt.date.today()) if c["key"] == item["key"]), None
    )
    if candidate is None or any(candidate[k] != item[k] for k in ("cycle", "initiative", "project", "source")) or (
        reviews.basis_hash(candidate["basis"]) != item["basis_hash"]
    ):
        raise ValueError("stale collector candidate or basis")
    source_hash = digest(doc.path.read_bytes())
    if source_hash != args["source_sha256"]:
        raise ValueError("stale source SHA-256")
    users = user_rows(fm)
    index = args["authority_decision"]
    if type(index) is not int or not 0 <= index < len(users):
        raise ValueError("authority_decision must index a user row")
    authority = users[index]
    if digest(canonical(authority).encode()) != args["authority_sha256"]:
        raise ValueError("stale authority SHA-256")
    pre = instruction(authority, cid)
    if index != len(users) - 1:
        raise ValueError("authority must be latest target user decision")
    init_doc = snapshot.initiatives[owner]
    init_rows = user_rows(parse_markdown(init_doc.path.read_text(), init_doc.path).frontmatter)
    if any(str(r["date"]) >= str(authority["date"]) for r in init_rows):
        raise ValueError("later or ambiguous initiative user decision")
    readback = prerequisite_state(project, pre, cid)
    if readback["stage"] == pre["equals"]:
        raise ValueError("prerequisite already met")
    intent(doc, item, authority, pre)
    return Wait(item, doc, authority, pre, readback, source_hash, digest(dump_markdown({"decisions": init_rows}, "").encode()))


@contextmanager
def root_lock(project: ProjectSettings):
    path = project.root / ".review-write.lock"
    if path.is_symlink():
        raise ValueError("unsafe root write lock")
    try:
        handle = path.open("r")
    except FileNotFoundError as exc:
        raise ValueError("existing root write lock required") from exc
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def operation_key(item: dict, args: dict, pre: dict) -> str:
    return digest(canonical([RULE, item["short_id"], item["basis_hash"], item["version"]["n"],
                             args["authority_sha256"], pre]).encode())


def controls(doc: Document) -> str:
    return digest(canonical([doc.frontmatter, doc.body]).encode())


def initiative_authority(project: ProjectSettings, data: dict) -> None:
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise ActionFailed("ambiguous initiative authority during recovery")
    fresh_doc(project, snapshot, data["cycle"])
    owner = snapshot.cycle_initiative[data["cycle"]]
    doc = snapshot.initiatives[owner]
    rows = user_rows(parse_markdown(doc.path.read_text(), doc.path).frontmatter)
    if digest(dump_markdown({"decisions": rows}, "").encode()) != data.get("initiative_authority_sha256"):
        raise ActionFailed("initiative user authority changed after preparation")


def current_operation(conn, sid: str) -> dict | None:
    latest = conn.execute("SELECT id FROM deferrals WHERE short_id = ? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if latest is None:
        return None
    ops = conn.execute("SELECT * FROM agent_deferral_ops WHERE short_id = ? AND state = 'completed'", (sid,)).fetchall()
    for row in ops:
        if json.loads(row["receipt_json"])["deferral_id"] == latest[0]:
            return dict(row)
    return None


def matching_operation(conn, sid: str, args: dict) -> dict | None:
    rows = conn.execute("SELECT * FROM agent_deferral_ops WHERE short_id = ? ORDER BY at DESC", (sid,)).fetchall()
    return next((dict(r) for r in rows if canonical(json.loads(r["data_json"])["args"]) == canonical(args)), None)


def project_audit(project: ProjectSettings, op: dict) -> str:
    data = json.loads(op["data_json"])
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise ActionFailed("ambiguous source during projection")
    doc = fresh_doc(project, snapshot, data["cycle"])
    current = doc.path.read_bytes()
    audits = [r for r in doc.frontmatter.get("decisions", []) if r.get("operation") == op["operation"]]
    audit = data["audit"]
    if audits:
        if audits != [audit] or doc.frontmatter["decisions"][-1] != audit:
            raise ActionFailed("audit projection is ambiguous")
        fm = dict(doc.frontmatter)
        fm["decisions"] = fm["decisions"][:-1]
        if controls(Document(doc.path, fm, doc.body)) != data["controls"]:
            raise ActionFailed("newer source edits prevent safe recovery")
    else:
        if digest(current) != data["source_sha256"] or controls(doc) != data["controls"]:
            raise ActionFailed("source changed before audit projection")
        fm = dict(doc.frontmatter)
        fm["decisions"] = [*fm.get("decisions", []), audit]
        text = dump_markdown(fm, doc.body)
        if doc.path.read_bytes() != current:
            raise ActionFailed("source changed immediately before replace")
        atomic_write(doc.path, text)
        if doc.path.read_bytes() != text.encode():
            raise ActionFailed("audit bytes failed readback")
    after = parse_markdown(doc.path.read_text(), doc.path)
    fm = dict(after.frontmatter)
    if fm["decisions"][-1:] != [audit]:
        raise ActionFailed("audit missing on readback")
    fm["decisions"] = fm["decisions"][:-1]
    if controls(Document(doc.path, fm, after.body)) != data["controls"]:
        raise ActionFailed("preserved fields or body changed")
    return digest(doc.path.read_bytes())


def audit_readback(project: ProjectSettings, op: dict) -> str:
    data = json.loads(op["data_json"])
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise ActionFailed("ambiguous source during readback")
    doc = fresh_doc(project, snapshot, data["cycle"])
    audit = data["audit"]
    rows = doc.frontmatter.get("decisions", [])
    if rows[-1:] != [audit] or len([r for r in rows if r.get("operation") == op["operation"]]) != 1:
        raise ActionFailed("exact single audit required on readback")
    fm = dict(doc.frontmatter)
    fm["decisions"] = rows[:-1]
    if controls(Document(doc.path, fm, doc.body)) != data["controls"]:
        raise ActionFailed("newer source edits prevent verified recovery")
    return digest(doc.path.read_bytes())


def _verify(conn, store, project, op, receipt):
    data = json.loads(op["data_json"])
    row = conn.execute("SELECT * FROM items WHERE short_id = ?", (op["short_id"],)).fetchone()
    item = store._item(conn, row)
    action = conn.execute("SELECT * FROM actions WHERE id = ?", (receipt["action_id"],)).fetchone()
    defer = conn.execute("SELECT * FROM deferrals WHERE id = ?", (receipt["deferral_id"],)).fetchone()
    if item["status"] not in ("deferred", "open") or action is None or defer is None or action["answer_id"] is not None:
        raise ActionFailed("parking projections failed readback")
    if action["state"] != "deferred" or action["action"] != f"settle:{RULE}" or defer["kind"] != "state_change":
        raise ActionFailed("parking receipt failed readback")
    if defer["until"] is not None or defer["basis_hash"] != data["basis_hash"]:
        raise ActionFailed("deferral condition failed readback")
    if action["short_id"] != op["short_id"] or defer["short_id"] != op["short_id"] or (
        json.loads(action["args_json"]) != {"operation": op["operation"], **data["args"]}
    ) or json.loads(action["receipt"]) != receipt or defer["reason"] != data["why"]:
        raise ActionFailed("operation projections disagree")
    if item["basis_hash"] != data["basis_hash"] or item["version"] != data["version"]:
        raise ActionFailed("operation basis or options changed")
    if conn.execute("SELECT 1 FROM answers WHERE short_id = ?", (op["short_id"],)).fetchone():
        raise ActionFailed("answers changed during parking")
    if audit_readback(project, op) != receipt["after_audit_sha256"]:
        raise ActionFailed("source changed after parking")


def park(store: reviews.ReviewStore, project: ProjectSettings, sid: str, args: dict, why: str) -> dict:
    from .review_settle import SettleRefused, _audit_row

    sid = str(sid).upper()
    prepared = None
    try:
        with sqlite3.connect(f"{store.path.resolve().as_uri()}?mode=ro", uri=True) as lookup:
            lookup.row_factory = sqlite3.Row
            prior = matching_operation(lookup, sid, args)
            if prior:
                prepared = prior["operation"]
        with root_lock(project), store._lock, store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                op = matching_operation(conn, sid, args)
                if op:
                    prepared = op["operation"]
                    data = json.loads(op["data_json"])
                    if data["project"] != project.id or data["root"] != str(project.root.resolve()):
                        raise ActionFailed("operation project changed")
                    if op["state"] == "completed":
                        current_item = conn.execute("SELECT status, basis_hash FROM items WHERE short_id = ?", (sid,)).fetchone()
                        owner = current_operation(conn, sid)
                        if current_item["status"] != "deferred" or current_item["basis_hash"] != data["basis_hash"] or (
                            owner is None or owner["operation"] != op["operation"]
                        ):
                            prepared = None
                            raise ValueError("operation no longer owns a deferred item; fresh options required")
                        receipt = json.loads(op["receipt_json"])
                        _verify(conn, store, project, op, receipt)
                        conn.execute("COMMIT")
                        return {"short_id": sid, "rule": RULE, "operation": prepared,
                                "status": "already_deferred", "receipt": receipt}
                else:
                    wait = eligible(conn, store, project, sid, args, why)
                    prepared_key = operation_key(wait.item, args, wait.prerequisite)
                    audit = _audit_row(RULE, sid, "Parked the review pending an authorized cycle-stage prerequisite", why,
                                       operation=prepared_key, basis=wait.item["basis_hash"],
                                       version=wait.item["version"]["n"], authority_sha256=args["authority_sha256"],
                                       prerequisite=wait.prerequisite, prerequisite_readback=wait.readback)
                    data = {"project": project.id, "root": str(project.root.resolve()), "cycle": wait.item["cycle"],
                            "args": args, "audit": audit, "prerequisite": wait.prerequisite,
                            "source_sha256": wait.source_sha256, "controls": controls(wait.doc),
                            "basis_hash": wait.item["basis_hash"], "version": wait.item["version"], "why": why,
                            "initiative_authority_sha256": wait.initiative_authority_sha256}
                    if not conn.execute("UPDATE items SET status = 'applying' WHERE short_id = ? AND status = 'open'",
                                        (sid,)).rowcount:
                        raise ValueError("item changed before preparation")
                    conn.execute("INSERT INTO agent_deferral_ops (operation, short_id, state, data_json, at) "
                                 "VALUES (?, ?, 'prepared', ?, ?)", (prepared_key, sid, canonical(data), reviews.now_iso()))
                    op = {"operation": prepared_key, "short_id": sid, "state": "prepared", "data_json": canonical(data)}
                conn.execute("COMMIT")
                prepared = op["operation"]
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            try:
                initiative_authority(project, data)
                readback = prerequisite_state(project, data["prerequisite"], data["cycle"])
                after_hash = project_audit(project, op)
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM items WHERE short_id = ?", (sid,)).fetchone()
                item = store._item(conn, row)
                if item["status"] != "applying" or item["basis_hash"] != data["basis_hash"] or item["version"] != data["version"]:
                    raise ActionFailed("item or options changed after preparation")
                if conn.execute("SELECT 1 FROM answers WHERE short_id = ?", (sid,)).fetchone():
                    raise ActionFailed("answer appeared after preparation")
                initiative_authority(project, data)
                current = prerequisite_state(project, data["prerequisite"], data["cycle"])
                if current != readback:
                    raise ActionFailed("prerequisite changed before finalization")
                defer_id = conn.execute("INSERT INTO deferrals (short_id, kind, until, reason, basis_hash, at) "
                                        "VALUES (?, 'state_change', NULL, ?, ?, ?)",
                                        (sid, data["why"], data["basis_hash"], reviews.now_iso())).lastrowid
                receipt = {"verified": True, "operation": prepared, "source_sha256": data["source_sha256"],
                           "after_audit_sha256": after_hash, "authority_sha256": data["args"]["authority_sha256"],
                           "basis_hash": data["basis_hash"], "version": data["version"]["n"],
                           "prerequisite": data["prerequisite"], "prerequisite_readback": current,
                           "agent": data["audit"]["agent"], "deferral_id": defer_id}
                cursor = conn.execute("INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
                                      "VALUES (NULL, ?, ?, ?, 'deferred', ?, ?)",
                                      (sid, f"settle:{RULE}", canonical({"operation": prepared, **args}),
                                       canonical(receipt), reviews.now_iso()))
                receipt["action_id"] = cursor.lastrowid
                conn.execute("UPDATE actions SET receipt = ? WHERE id = ?", (canonical(receipt), cursor.lastrowid))
                conn.execute("UPDATE items SET status = 'deferred', updated_at = ? WHERE short_id = ?",
                             (reviews.now_iso(), sid))
                conn.execute("UPDATE agent_deferral_ops SET state = 'completed', receipt_json = ?, error = NULL "
                             "WHERE operation = ?", (canonical(receipt), prepared))
                _verify(conn, store, project, op, receipt)
                conn.execute("COMMIT")
                _verify(conn, store, project, op, receipt)
                state = conn.execute("SELECT state FROM rule_states WHERE rule = ? ORDER BY id DESC LIMIT 1", (RULE,)).fetchone()
                if current["stage"] == data["prerequisite"]["equals"] or (state and state[0] == "suspended"):
                    _reopen(conn, store, sid, prepared, "Prepared wait completed or rule suspended; return to user review")
                status = conn.execute("SELECT status FROM items WHERE short_id = ?", (sid,)).fetchone()[0]
                return {"short_id": sid, "rule": RULE, "operation": prepared, "status": status, "receipt": receipt}
            except Exception as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                conn.execute("UPDATE agent_deferral_ops SET error = ?, state = CASE WHEN state = 'completed' "
                             "THEN state ELSE 'failed' END WHERE operation = ?", (str(exc)[:1000], prepared))
                raise ActionFailed(f"operation {prepared} failed: {exc}") from exc
    except (ActionFailed, ValueError, TypeError, AttributeError, KeyError, OSError, sqlite3.Error) as exc:
        if prepared:
            raise ActionFailed(f"operation {prepared} failed: {exc}") from exc
        raise SettleRefused(str(exc)) from exc


def _reopen(conn, store, sid: str, operation: str, reason: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM items WHERE short_id = ?", (sid,)).fetchone()
        item = store._item(conn, row)
        if item["status"] != "deferred":
            raise reviews.ReviewError("item is not deferred")
        conn.execute("UPDATE items SET status = 'open', updated_at = ? WHERE short_id = ?", (reviews.now_iso(), sid))
        conn.execute("INSERT INTO actions (answer_id, short_id, action, args_json, state, receipt, at) "
                     "VALUES (NULL, ?, 'reopen_deferral', ?, 'reopened', ?, ?)",
                     (sid, canonical({"version": item["version"]["n"], "operation": operation}), reason, reviews.now_iso()))
        conn.execute("COMMIT")
        return {"short_id": sid, "status": "open", "operation": operation, "reason": reason}
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def reopen(store: reviews.ReviewStore, project: ProjectSettings, sid: str, reason: str) -> dict:
    with root_lock(project), store._lock, store._connect() as conn:
        op = current_operation(conn, sid)
        if op is None:
            raise reviews.ReviewError("completed operation required")
        data = json.loads(op["data_json"])
        if data["project"] != project.id or data["root"] != str(project.root.resolve()):
            raise reviews.ReviewError("operation project mismatch")
        readback = prerequisite_state(project, data["prerequisite"], data["cycle"])
        if readback["stage"] != data["prerequisite"]["equals"]:
            raise reviews.ReviewError("prerequisite still pending")
        _verify(conn, store, project, dict(op), json.loads(op["receipt_json"]))
        return _reopen(conn, store, sid, op["operation"], reason)
