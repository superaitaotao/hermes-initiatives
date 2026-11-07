"""Learned rules: each auto mark appends a delegated rule, so later ticks settle items like it without asking.

An auto mark says the triage left an item with the user that he did not need to decide. When the recovery tick
applies or resolves an auto item, it must say why the item was not handled automatically (``why_not_auto``) and
append a rule for items like it (``learned_<name>``), or say why the case is one-off. From then on the rule sits next
to the coded rules in ``AGENT_RULES``: the tick reads it from ``tick-in.json`` → ``learned_rules``, settles items
with ``daily_review.py settle`` under its name, and the user suspends, overrules, and restores it like any other rule
(``ReviewStore.rule_states``).

The model writes the rule's condition in plain words (``when``) and a structured ``match``; the code owns the rest.
A rule stores the code-computed signature of the step the agent took (source kind, block kind or cycle reasons,
action, and argument shape), and the match must narrow it (initiative, cycle, owner profile, or words in the title or
context) and must fit the item it was learned from. A later item is covered only when the code finds the same
signature and the same match, the option is reversible, and the rule is active and under its daily cap. The model
still checks ``when`` against the live source before it settles an item under the rule.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from .model import LEARNED_RULE
from .reviews import ReviewStore, current, now_iso

PREFIX = "learned_"
DOS = ("apply", "resolve")
# Why the triage did not handle the item itself. The first word names the gap; the rest names the evidence.
GAPS = ("no_rule:", "rule_refused:", "on_bar:", "missing_evidence:", "misjudged:")
SKIPS = ("one_off:", "covered_by ")
MATCH_KEYS = ("initiative", "cycle", "owner_profile", "title_contains", "context_contains")
NAME = re.compile(r"^[a-z][a-z0-9_]{3,40}$")
MAX_USES_PER_DAY = 5
MIN_TERM = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_rules (
  rule TEXT PRIMARY KEY, do TEXT NOT NULL, signature TEXT NOT NULL, match_json TEXT NOT NULL,
  when_text TEXT NOT NULL, why_not_auto TEXT NOT NULL, from_short_id TEXT NOT NULL, option_label TEXT,
  project TEXT, created_at TEXT NOT NULL
);
"""


class RuleRefused(ValueError):
    """The rule is not specific, not grounded, or does not fit; nothing was written."""


def _ensure(store: ReviewStore) -> None:
    with store._connect() as conn:
        conn.executescript(_SCHEMA)


def signature(item: dict[str, Any], option: dict[str, Any] | None, do: str) -> str:
    """The step's shape: source kind, block kind or cycle reasons, action, and argument shape (code-computed)."""
    source = str(item.get("source") or "")
    basis = item.get("basis") or {}
    parts = [source]
    if source == "cycle":
        parts.append("+".join(sorted(str(r) for r in basis.get("reasons") or [])))
    elif source == "kanban":
        parts.append(f"block={basis.get('block_kind') or 'none'}")
    if do == "resolve" or option is None:
        return ":".join([*parts, "resolve"])
    action, args = str(option.get("action") or ""), option.get("args") or {}
    if action == "set_cycle_fields":
        fields = args.get("fields") or {}
        shape = "+".join(f"{k}={fields[k]}" if isinstance(fields[k], (str, int, float, bool)) else k for k in sorted(fields))
    elif action == "idea_status":
        shape = str(args.get("status") or "") + ("+into" if args.get("into") else "")
    elif action == "defer":
        shape = str(args.get("kind") or "state_change")
    elif action == "record_decision":
        # The decision's verb (keep, ship, stop, ...) so a rule to keep going never covers a stop.
        words = str(args.get("decision") or option.get("label") or "").lower().split()
        shape = words[0] if words else ""
    elif action == "kanban_create":
        shape = str(args.get("assignee") or "")
    else:
        shape = ""
    return ":".join([*parts, action, shape])


def _clean_match(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise RuleRefused(f"match must narrow the rule with at least one of {', '.join(MATCH_KEYS)}")
    unknown = set(raw) - set(MATCH_KEYS)
    if unknown:
        raise RuleRefused(f"match keys must be among {', '.join(MATCH_KEYS)}, not {', '.join(sorted(unknown))}")
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key.endswith("_contains"):
            terms = [str(t).strip() for t in (value if isinstance(value, list) else [value]) if str(t).strip()]
            if not terms or any(len(t) < MIN_TERM for t in terms):
                raise RuleRefused(f"match.{key} needs words of at least {MIN_TERM} characters")
            clean[key] = sorted({t.lower() for t in terms})
        else:
            if not str(value or "").strip():
                raise RuleRefused(f"match.{key} must not be empty")
            clean[key] = str(value).strip()
    return clean


def fits(match: dict[str, Any], item: dict[str, Any]) -> bool:
    """True when every match term holds for *item* (terms are ANDed; words are case-insensitive substrings)."""
    for key, value in match.items():
        if key == "title_contains":
            if not all(t in str(item.get("title") or "").lower() for t in value):
                return False
        elif key == "context_contains":
            text = f"{item.get('context') or ''}\n{(item.get('version') or {}).get('context') or ''}".lower()
            if not all(t in text for t in value):
                return False
        elif str(item.get(key) or "") != value:
            return False
    return True


def _state(store: ReviewStore, rule: str) -> dict[str, Any]:
    return store.rule_states().get(rule) or {"state": "active"}


def _row(store: ReviewStore, row: Any, states: dict[str, Any]) -> dict[str, Any]:
    rule = dict(row)
    rule["match"] = json.loads(rule.pop("match_json"))
    rule["when"] = rule.pop("when_text")
    state = states.get(rule["rule"]) or {"state": "active"}
    rule["state"] = state["state"]
    rule["state_reason"] = state.get("reason")
    rule["uses"] = uses(store, rule["rule"])
    rule["uses_today"] = uses(store, rule["rule"], days=1)
    return rule


def get(store: ReviewStore, rule: str) -> dict[str, Any] | None:
    _ensure(store)
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM learned_rules WHERE rule = ?", (str(rule),)).fetchone()
    return _row(store, row, store.rule_states()) if row else None


def all_rules(store: ReviewStore, *, active_only: bool = False) -> list[dict[str, Any]]:
    _ensure(store)
    with store._connect() as conn:
        rows = conn.execute("SELECT * FROM learned_rules ORDER BY created_at, rule").fetchall()
    states = store.rule_states()
    out = [_row(store, r, states) for r in rows]
    return [r for r in out if r["state"] == "active"] if active_only else out


def uses(store: ReviewStore, rule: str, *, days: int | None = None) -> int:
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).replace(microsecond=0).isoformat() if days else ""
    with store._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM actions WHERE action = ? AND at >= ?", (f"settle:{rule}", since)).fetchone()[0]


def prepare(item: dict[str, Any], option: dict[str, Any] | None, do: str, entry: Any) -> dict[str, Any] | None:
    """Check the rule part of an auto entry before anything is written. None = a grounded one-off (no rule)."""
    if not isinstance(entry, dict):
        raise RuleRefused(
            'an auto apply or resolve needs "rule": {"name", "why_not_auto", "when", "match"} or {"skip": "one_off: ..."}'
        )
    if "skip" in entry:
        skip = str(entry.get("skip") or "").strip()
        if not skip.startswith(SKIPS) or len(skip) < 20:
            raise RuleRefused(f"rule.skip starts with one of {', '.join(SKIPS)} and says why no rule applies")
        return None
    name = str(entry.get("name") or "").strip().lower().removeprefix(PREFIX)
    if not NAME.fullmatch(name):
        raise RuleRefused("rule.name is a short snake_case name for the kind of item (4-41 characters)")
    why = str(entry.get("why_not_auto") or "").strip()
    if not why.startswith(GAPS) or len(why) < 25:
        raise RuleRefused(f"rule.why_not_auto starts with one of {', '.join(GAPS)} and names the evidence")
    when = str(entry.get("when") or "").strip()
    if len(when) < 20:
        raise RuleRefused("rule.when says in a sentence which items this covers and what must be true")
    match = _clean_match(entry.get("match"))
    if not fits(match, item):
        raise RuleRefused(f"rule.match does not fit {item['short_id']} itself")
    return {
        "rule": PREFIX + name,
        "do": do,
        "signature": signature(item, option, do),
        "match": match,
        "when": when[:1000],
        "why_not_auto": why[:1000],
        "option_label": (option or {}).get("label"),
        "project": item.get("project"),
    }


def append(store: ReviewStore, spec: dict[str, Any], from_short_id: str) -> str:
    """Append the rule. The same signature and match returns the existing rule (in whatever state the user left it);
    a taken name gets a numeric suffix."""
    _ensure(store)
    match_json = json.dumps(spec["match"], sort_keys=True)
    with store._lock, store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        same = conn.execute(
            "SELECT rule FROM learned_rules WHERE signature = ? AND match_json = ?", (spec["signature"], match_json)
        ).fetchone()
        if same:
            conn.execute("COMMIT")
            return same[0]
        rule, n = spec["rule"], 1
        while conn.execute("SELECT 1 FROM learned_rules WHERE rule = ?", (rule,)).fetchone():
            n += 1
            rule = f"{spec['rule']}_{n}"
        assert LEARNED_RULE.fullmatch(rule)
        conn.execute(
            "INSERT INTO learned_rules (rule, do, signature, match_json, when_text, why_not_auto, from_short_id, "
            "option_label, project, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule,
                spec["do"],
                spec["signature"],
                match_json,
                spec["when"],
                spec["why_not_auto"],
                from_short_id,
                spec.get("option_label"),
                spec.get("project"),
                now_iso(),
            ),
        )
        conn.execute("COMMIT")
    store.log_action(None, from_short_id, "rule_learned", {"rule": rule}, "active", receipt=spec["why_not_auto"])
    return rule


def covers(rule: dict[str, Any], item: dict[str, Any], option: dict[str, Any] | None, do: str) -> bool:
    return rule["do"] == do and rule["signature"] == signature(item, option, do) and fits(rule["match"], item)


def matches(store: ReviewStore) -> list[dict[str, Any]]:
    """Open gated items an active learned rule covers now, with the option it covers (``tick-in.json``)."""
    rules = [r for r in all_rules(store, active_only=True) if r["uses_today"] < MAX_USES_PER_DAY]
    if not rules:
        return []
    out = []
    for item in store.items(statuses=("open",)):
        if item["section"] != "gated" or item["source"] == "learn" or item.get("urgent_rule"):
            continue
        for rule in rules:
            if rule["do"] == "resolve":
                if covers(rule, item, None, "resolve"):
                    out.append(_hit(item, rule, None))
                continue
            if not current(item):
                continue
            for option in item["version"]["options"]:
                if not option.get("confirm") and covers(rule, item, option, "apply"):
                    out.append(_hit(item, rule, option))
    return out


def _hit(item: dict[str, Any], rule: dict[str, Any], option: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "short_id": item["short_id"],
        "title": item["title"],
        "rule": rule["rule"],
        "args": {"option": option["id"]} if option else {"resolve": True},
        "option_label": option["label"] if option else None,
        "when": rule["when"],
        "learned_from": rule["from_short_id"],
        "context": (item.get("context") or "")[:800],
    }
