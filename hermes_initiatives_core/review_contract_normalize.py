"""Restore empty measurement fields from exact typed prior user approvals, never inferred prose."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json

from . import review_collect, reviews, writers
from .docs import parse_markdown
from .review_deferral import fresh_doc
from .review_exec import ActionFailed
from .store import load

RULE = "normalize_outcome_contract"
FIELDS = {"primary_metric", "source", "baseline", "observation_window", "guardrails", "insufficient_data"}


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def normalize(settler, item, args, why):
    """Restore selected empty fields from the latest typed user row, audit and dismiss only the old basis."""
    from .review_settle import SettleRefused, _audit_row, _user_decisions

    store, project = settler.store, settler.project
    if item.get("status") != "open":
        raise SettleRefused("only open items can be normalized")
    if (store.rule_states().get(RULE) or {}).get("state") == "suspended":
        raise SettleRefused("normalization rule is suspended")
    if not isinstance(args, dict) or set(args) != {"authority_decision", "authority_sha256", "source_sha256", "fields"}:
        raise SettleRefused("normalization needs exact authority, source hashes and fields")
    if item.get("source") != "cycle" or item.get("project") != project.id:
        raise SettleRefused("normalization needs an attributed cycle item")
    basis = item.get("basis")
    if not isinstance(basis, dict) or basis.get("reasons") != ["outcome_review_ready"]:
        raise SettleRefused("normalization needs a well-formed basis with a current outcome review")
    if any(item.get(k) for k in ("urgent_rule", "owner_profile", "owner_job", "blocks")) or store.answers(item["short_id"]):
        raise SettleRefused("an answered or protected item remains with the user")
    if not isinstance(why, str) or not reviews.current(item) or not item.get("version") or len(why.strip()) < 20:
        raise SettleRefused("current options and a substantive why sentence are required")
    fields = args["fields"]
    if not isinstance(fields, list) or not fields or any(not isinstance(k, str) or k not in FIELDS for k in fields):
        raise SettleRefused("only empty measurement bookkeeping fields can be restored")
    if len(set(fields)) != len(fields):
        raise SettleRefused("fields must be unique")
    snapshot = load(project.root)
    if snapshot.parse_errors or snapshot.duplicate_ids:
        raise SettleRefused("malformed or ambiguous sources")
    try:
        doc = fresh_doc(project, snapshot, str(item.get("cycle") or ""))
    except (ValueError, OSError) as exc:
        raise SettleRefused(str(exc)) from exc
    fm = doc.frontmatter
    initiative = snapshot.initiatives[snapshot.cycle_initiative[item["cycle"]]]
    initiative_bytes = initiative.path.read_bytes()
    initiative_hash = _digest(initiative_bytes)
    if parse_markdown(initiative_bytes.decode("utf-8"), initiative.path).frontmatter != initiative.frontmatter:
        raise SettleRefused("initiative authority changed during collection")
    if item.get("initiative") != initiative.frontmatter.get("id"):
        raise SettleRefused("initiative attribution changed")
    if not (initiative.frontmatter.get("outcome_policy") or {}).get("enabled"):
        raise SettleRefused("outcome policy is not enabled")
    if fm.get("stage") not in ("decided", "shipping", "measuring") or not (fm.get("outcome_review") or {}).get("enabled"):
        raise SettleRefused("cycle must be open with an enabled outcome review")
    if (fm.get("result") or {}).get("verdict", "unset") != "unset" or (fm.get("disposition") or {}).get(
        "action", "unset"
    ) != "unset":
        raise SettleRefused("a verdict or disposition requires user reconciliation")
    users = _user_decisions(fm)
    index = args["authority_decision"]
    if type(index) is not int or index != len(users) - 1 or not users:
        raise SettleRefused("authority must be the latest user decision; older intent is ambiguous")
    authority = users[index]
    try:
        authority_date = dt.date.fromisoformat(str(authority.get("date") or ""))
        opened_date = dt.date.fromisoformat(str(item.get("opened_at") or "")[:10])
    except ValueError as exc:
        raise SettleRefused("authority and question need valid dates") from exc
    canonical = json.dumps(authority, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    if _digest(canonical) != args["authority_sha256"] or _digest(doc.path.read_bytes()) != args["source_sha256"]:
        raise SettleRefused("source or authority hash changed")
    if authority_date > opened_date:
        raise SettleRefused("authority is newer than the question")
    if any(str(row.get("date") or "") >= str(authority.get("date") or "") for row in _user_decisions(initiative.frontmatter)):
        raise SettleRefused("later initiative authority needs reconciliation")
    approved = (authority.get("fields") or {}).get("measurement")
    measurement = fm.get("measurement")
    if not isinstance(approved, dict) or not isinstance(measurement, dict):
        raise SettleRefused("missing exact typed approval")
    updates = {}
    for name in fields:
        if name not in approved or approved[name] in (None, "", [], {}):
            raise SettleRefused(f"{name} has no exact typed approval")
        if measurement.get(name) not in (None, "", [], {}):
            raise SettleRefused(f"{name} is not empty; replacing a value is a user decision")
        updates[name] = copy.deepcopy(approved[name])
    candidate = next(
        (c for c in review_collect._cycle_gates(project, snapshot, dt.date.today()) if c["key"] == item["key"]), None
    )
    if candidate is None or reviews.basis_hash(candidate["basis"]) != item["basis_hash"]:
        raise SettleRefused("stale collector basis or unavailable board")
    sid = item["short_id"]
    if not store.claim_under_rule(sid, basis_hash=item["basis_hash"], version=item["version"]["n"]):
        raise SettleRefused("item changed before claim")
    try:

        def change(current):
            if _digest(initiative.path.read_bytes()) != initiative_hash:
                raise ActionFailed("initiative authority changed under the write lock")
            if _digest(doc.path.read_bytes()) != args["source_sha256"] or current != fm:
                raise ActionFailed("source changed under the write lock")
            current["measurement"].update(updates)
            current["decisions"] = [
                *current.get("decisions", []),
                _audit_row(
                    RULE,
                    sid,
                    "Restored empty measurement fields from an existing typed user approval",
                    why,
                    authority_decision=index,
                    authority_sha256=args["authority_sha256"],
                    source_sha256=args["source_sha256"],
                    restored_fields=sorted(updates),
                ),
            ]

        after = writers.edit_frontmatter(doc.path, project.root, change)
        if any(after["measurement"].get(k) != v for k, v in updates.items()):
            raise ActionFailed("restored fields failed readback")
        receipt = f"Restored {', '.join(sorted(updates))} from user decision {index}; no new criteria or outcome adopted"
        if not store.settle(sid, RULE, receipt, expect=("applying",), args=args):
            raise ActionFailed("normalization claim changed before settlement")
    except Exception as exc:
        store.release_rule_claim(sid, rule=RULE, error=str(exc))
        raise ActionFailed(f"{RULE}: {exc}") from exc
    return {"short_id": sid, "rule": RULE, "receipt": receipt}
