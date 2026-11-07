"""``plan_outcome_followup``: one bounded, plan-only card for a ready outcome review.

The rule never executes the recommended option. It reads the current assessment receipt, proves the artifacts by
SHA-256, and starts a card that may only investigate and plan against the already approved scope. The tests run the
real collector, the real ``hermes kanban`` CLI, and the isolated stores from ``conftest``.

Kanban writes run the real CLI against the isolated test board (conftest pins HERMES_HOME and HERMES_KANBAN_HOME
under tmp_path).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest
from conftest import cycle_md, write

from hermes_initiatives_core import outcome_loop, review_collect, review_exec, review_settle, reviews, writers
from hermes_initiatives_core.docs import dump_markdown, parse_markdown
from hermes_initiatives_core.kanban import read_cards
from hermes_initiatives_core.settings import load_settings
from hermes_initiatives_core.store import load
from hermes_initiatives_core.validate import validate

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

RULE = "plan_outcome_followup"
CYCLE = "in-demo-c01"
WHY = "The current assessment is ready and the bounded repair plan belongs to the configured plan lane."
CANDIDATE_BODY = "Start the optional pilot in October and widen the cohort to every account."
USER_DECISIONS = [{"date": "2026-09-01", "by": "user", "decision": "Ship the approved change to all current users."}]
# A nonrecommended option carrying one of these makes the whole item ineligible: the routine option beside it is not
# separable from the user's own decision (review_tick.md "The bar").
PROTECTED_ALTERNATIVES = {
    "verdict": ("set_cycle_fields", {"cycle": CYCLE, "fields": {"result": {"verdict": "no_effect"}}}),
    "disposition": ("set_cycle_fields", {"cycle": CYCLE, "fields": {"disposition": {"action": "continue"}}}),
    "stage": ("set_cycle_fields", {"cycle": CYCLE, "fields": {"stage": "measuring"}}),
    "measurement": ("set_cycle_fields", {"cycle": CYCLE, "fields": {"measurement": {"primary_metric": "clicks"}}}),
    "decision": ("record_decision", {"decision": "The outcome is met; close this cycle."}),
}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fm(path: Path) -> dict:
    return parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter


def _tasks() -> dict:
    conn = kbc.connect(board="default")
    try:
        return {task.id: task for task in kb.list_tasks(conn)}
    finally:
        conn.close()


def _sync(env) -> None:
    review_collect.sync(env["store"], [env["project"]], gates={"default": {}})


def _rewrite(path: Path, change) -> None:
    doc = parse_markdown(path.read_text(encoding="utf-8"), path)
    change(doc.frontmatter)
    write(path, dump_markdown(doc.frontmatter, doc.body))


def _epoch(env) -> str:
    fm = _fm(env["target"])
    ids = [str(card) for card in fm.get("kanban_cards") or []]
    cards = read_cards("default", ids)["cards"] if ids else {}
    return outcome_loop.input_epoch(fm, [cards[card] for card in ids])


def _args(env) -> dict:
    users = [row for row in _fm(env["target"])["decisions"] if row.get("by") == "user"]
    row = users[-1]
    return {
        "repairs_decision": len(users) - 1,
        "authority_sha256": _digest(_canonical(row).encode("utf-8")),
        "source_sha256": _digest(env["target"].read_bytes()),
    }


def _settle(env, args=None, why=WHY):
    return review_settle.Settler(env["store"], env["project"]).settle(env["sid"], RULE, _args(env) if args is None else args, why)


def _options(env, sid=None, recommend="A") -> None:
    env["store"].add_version(
        sid or env["sid"],
        question="What should follow the ready outcome assessment?",
        recommend=recommend,
        context="The assessment found a gap and its proposal describes repairs; no policy was chosen.",
        options=[
            {
                "id": "A",
                "label": "Plan the bounded repair",
                "action": "kanban_create",
                "args": {"assignee": "planner", "title": "Repair the outcome", "body": CANDIDATE_BODY},
            },
            {
                "id": "B",
                "label": "Acknowledge the assessment",
                "action": "acknowledge",
                "args": {},
            },
            {"id": "C", "label": "Wait for more evidence", "action": "defer", "args": {"kind": "state_change"}},
        ],
    )


def _snapshot(env):
    with sqlite3.connect(env["store"].path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        rows = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
    with sqlite3.connect(kb.kanban_db_path("default")) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        board = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
    files = {str(p.relative_to(env["root"])): p.read_bytes() for p in env["root"].rglob("*") if p.is_file()}
    return rows, board, files


@pytest.fixture
def outcome(isolated_hermes, root, tmp_path):
    kbc.init_db(board="default")
    with kbc.connect(board="default") as conn:
        card = kb.create_task(conn, title="Delivered outcome work", assignee="planner")
        kb.complete_task(conn, card, result="Delivered the approved change")
    write(
        root / "in-demo" / "initiative.md",
        """
        ---
        id: in-demo
        title: Demo initiative
        status: active
        rank: 1
        goal: Grow demo
        observe_every_days: 7
        outcome_policy:
          enabled: true
          owner: planner
          session: session-2026-10
        decisions:
          - {date: 2026-09-01, by: user, decision: Start demo}
        ---
        Demo brief.
        """,
    )
    target = root / "in-demo" / "cycles" / f"{CYCLE}.md"
    write(
        target,
        cycle_md(
            CYCLE,
            stage="decided",
            decisions=USER_DECISIONS,
            kanban_cards=[card],
            assessment_cards=[],
            measurement={"success_criteria": "clicks +10%"},
            outcome_review={
                "enabled": True,
                "owner": "planner",
                "collector": "Read main CI",
                "release_surface": "main_ci",
                "review_by": "2026-10-20",
                "target_date": None,
            },
        ),
    )
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "demo",
                        "label": "Demo",
                        "root": str(root),
                        "kanban_board": "default",
                        "plan_assignee": "planner",
                        "plan_skills": ["plan"],
                    }
                ]
            }
        )
    )
    store = reviews.ReviewStore()
    project = load_settings()[0].projects[0]
    evidence = write(tmp_path / "reports" / "evidence.md", "Clicks 118 against target 120; six flakes.")
    proposal = write(tmp_path / "reports" / "proposal.md", "Alternatives: pilot, hold, or extend. None chosen.")
    fm = _fm(target)
    ids = [str(c) for c in fm["kanban_cards"]]
    epoch = outcome_loop.input_epoch(fm, [read_cards("default", ids)["cards"][c] for c in ids])
    receipt = writers.record_observation(
        root,
        CYCLE,
        {
            "kind": "assessment",
            "epoch": epoch,
            "status": "missed",
            "detail": "Clicks 118 against the 120 target.",
            "evidence": str(evidence),
            "proposal": str(proposal),
        },
    )
    env = {
        "store": store,
        "root": root,
        "project": project,
        "target": target,
        "card": card,
        "epoch": epoch,
        "evidence": evidence,
        "proposal": proposal,
        "artifacts": receipt["artifact_sha256"],
    }
    _sync(env)
    env["sid"] = store.get_by_key(f"cycle:demo:{CYCLE}:gate")["short_id"]
    _options(env)
    return env


@pytest.mark.parametrize("owner", [None, "Alex"])
def test_plan_outcome_followup_starts_one_bounded_plan_card_and_audits(outcome, owner):
    from hermes_initiatives_core.settings import settings_path

    config = json.loads(settings_path().read_text())
    if owner is not None:
        config["owner_name"] = owner
    settings_path().write_text(json.dumps(config))
    owner_name = owner or "the user"
    env = outcome
    before = parse_markdown(env["target"].read_text(encoding="utf-8"), env["target"])
    before_files = {
        k: v
        for k, v in _snapshot(env)[2].items()
        if k != f"in-demo/cycles/{CYCLE}.md" and k != ".review-write.lock" and not k.startswith("in-demo/observations/")
    }
    tasks = set(_tasks())
    result = _settle(env)

    created = set(_tasks()) - tasks
    assert len(created) == 1
    card = created.pop()
    task = _tasks()[card]
    assert task.assignee == "planner"
    assert task.title == f"Plan outcome follow-up {CYCLE}"
    body = task.body
    assert body.startswith(f"- Initiative cycle: {CYCLE}\n")
    for needle in (
        f"Assessment epoch: {env['epoch']}",
        str(env["evidence"]),
        env["artifacts"]["evidence"],
        str(env["proposal"]),
        env["artifacts"]["proposal"],
        "Planning context only, not authority to execute",
        USER_DECISIONS[0]["decision"],
        "undecided",
        "Do not implement",
        "credentials",
        "deployment",
        "new cycle",
        "verdict",
        "disposition",
        RULE,
        env["sid"],
    ):
        assert needle in body, needle
    assert f"disposition stay with {owner_name}." in body
    assert f"under {owner_name}'s rule {RULE}" in body
    assert CANDIDATE_BODY not in body, "the recommended option body is never executed as the card brief"
    assert "optional pilot" not in body.lower(), "no product policy is adopted"
    assert "Repair the outcome" not in body, "the recommended title is not adopted"
    assert "Plan the bounded repair" not in body, "the option label is not interpolated into the card"
    assert "Clicks 118 against the 120 target." not in body, "the receipt detail is not interpolated into the card"
    assert "Assessment cycle:" not in body and not outcome_loop.ASSESSMENT_MARKER.search(body)
    from hermes_initiatives_core.kanban import CYCLE_MARKER

    assert {m.lower() for m in CYCLE_MARKER.findall(body)} == {CYCLE}
    assert result["receipt"] == f"{card} for planner, linked to {CYCLE}"

    after = parse_markdown(env["target"].read_text(encoding="utf-8"), env["target"])
    assert after.frontmatter.pop("kanban_cards") == [*before.frontmatter.pop("kanban_cards"), card]
    row = after.frontmatter["decisions"].pop()
    assert after.frontmatter == before.frontmatter and after.body == before.body, "only the audit row and link change"
    assert {k: row[k] for k in ("by", "rule", "review", "card", "epoch")} == {
        "by": "agent",
        "rule": RULE,
        "review": env["sid"],
        "card": card,
        "epoch": env["epoch"],
    }
    assert row["authority_sha256"] == _args(env)["authority_sha256"]
    assert row["source_sha256"] != _digest(env["target"].read_bytes()), "the audit names the pre-settlement source"
    assert row["decision"] == (
        f"Started read-only follow-up plan {card} for planner; the outcome review, verdict, disposition, "
        f"and scope stay with {owner_name}"
    )
    assert _fm(env["target"])["result"]["verdict"] == "unset"
    assert _fm(env["target"])["disposition"]["action"] == "unset"
    assert _fm(env["target"])["assessment_cards"] == []

    item = env["store"].get(env["sid"])
    assert item["status"] == "settled" and env["store"].answers(env["sid"]) == []
    assert [(a["action"], a["answer_id"], a["state"]) for a in env["store"].actions(env["sid"])] == [
        (f"settle:{RULE}", None, "settled")
    ]
    assert not validate(load(env["root"]), check_sources=False).errors.get(CYCLE)
    # writers.link_cards records its own "linked ..." note and the root write lock is a file, so only the cycle file
    # and that append-only observation log change; no user-owned source file does.
    after_files = {
        k: v
        for k, v in _snapshot(env)[2].items()
        if k != f"in-demo/cycles/{CYCLE}.md" and k != ".review-write.lock" and not k.startswith("in-demo/observations/")
    }
    assert after_files == before_files, "no other source file is touched"

    _sync(env)
    assert env["store"].get(env["sid"])["status"] == "settled"
    with pytest.raises(review_settle.SettleRefused, match="only open"):
        _settle(env)
    assert set(_tasks()) == tasks | {card}


@pytest.mark.parametrize(
    "case",
    [
        "args_missing",
        "args_extra",
        "index_bool",
        "index_range",
        "authority_sha",
        "source_sha",
        "why",
        "suspended",
        "deferred",
        "applying",
        "urgent",
        "owner_profile",
        "owner_job",
        "blocks",
        "section",
        "source",
        "project",
        "key",
        "cycle_invalid",
        "reasons",
        "stage",
        "verdict",
        "disposition",
        "epoch",
        "receipt",
        "finding",
        "artifacts",
        "stale_options",
        "no_options",
        "needs_options",
        "answer",
        "recommend_stage",
        "lane",
        "board",
        "title",
        "extra_arg",
        "confirm",
        "human_gate",
        "parse_error",
        "duplicate_id",
        "stale_basis",
        "paused",
        "policy_disabled",
        "recorded_concluded",
        "artifact_sha",
        "artifact_missing",
        "artifact_symlink",
        "cycle_symlink",
        "initiative_symlink",
        "authority_newer",
        "initiative_later",
        "basis_omits_verdict",
        "cap",
        "chain",
        "failed_action",
        "no_lane",
        "no_board",
        "protected_verdict",
        "protected_disposition",
        "protected_stage",
        "protected_measurement",
        "protected_decision",
        "malformed_options",
        "malformed_option",
        "malformed_args",
        "malformed_fields",
    ],
)
def test_plan_outcome_followup_refuses_without_writes(outcome, monkeypatch, case):
    env, store, sid = outcome, outcome["store"], outcome["sid"]
    args, why = _args(env), WHY
    message = {
        "args_missing": "arguments",
        "args_extra": "arguments",
        "index_bool": "repairs_decision",
        "index_range": "repairs_decision",
        "authority_sha": "SHA-256",
        "source_sha": "SHA-256",
        "why": "why",
        "suspended": "suspended",
        "deferred": "only open",
        "applying": "only open",
        "urgent": "urgency",
        "owner_profile": "urgency",
        "owner_job": "urgency",
        "blocks": "urgency",
        "section": "gated",
        "source": "gated",
        "project": "project",
        "key": "key",
        "cycle_invalid": "key|cycle",
        "reasons": "basis",
        "stage": "basis",
        "verdict": "basis",
        "disposition": "basis",
        "epoch": "basis",
        "receipt": "basis",
        "finding": "basis",
        "artifacts": "basis",
        "stale_options": "current",
        "no_options": "current",
        "needs_options": "current",
        "answer": "answer",
        "recommend_stage": "recommended",
        "lane": "signature",
        "board": "board",
        "title": "title",
        "extra_arg": "arguments",
        "confirm": "confirm",
        "human_gate": "human gate",
        "parse_error": "malformed",
        "duplicate_id": "malformed",
        "stale_basis": "collector",
        "paused": "active",
        "policy_disabled": "outcome policy",
        "recorded_concluded": "decided|shipping|measuring",
        "artifact_sha": "artifact",
        "artifact_missing": "artifact",
        "artifact_symlink": "artifact",
        "cycle_symlink": "unsafe cycle path",
        "initiative_symlink": "invalid initiative identity or path",
        "authority_newer": "newer than this item",
        "initiative_later": "later initiative",
        "basis_omits_verdict": "omits a verdict",
        "cap": "24 hours",
        "chain": "chain guard",
        "failed_action": "chain guard",
        "no_lane": "plan_assignee",
        "no_board": "kanban_board",
        # A protected alternative anywhere in the item refuses it; the recommended option's exact signature is the
        # only selection this rule may make.
        "protected_verdict": "review bar",
        "protected_disposition": "review bar",
        "protected_stage": "review bar",
        "protected_measurement": "review bar",
        "protected_decision": "review bar",
        "malformed_options": "options are malformed",
        "malformed_option": "not a mapping",
        "malformed_args": "malformed arguments",
        "malformed_fields": "malformed fields",
    }[case]

    def column(name, value):
        with store._connect() as conn:
            conn.execute(f"UPDATE items SET {name}=? WHERE short_id=?", (value, sid))

    def options(change):
        version = store.get(sid)["version"]
        change(version["options"])
        with store._connect() as conn:
            conn.execute(
                "UPDATE item_versions SET options_json=? WHERE short_id=? AND n=?",
                (json.dumps(version["options"]), sid, version["n"]),
            )

    if case == "args_missing":
        args = {"repairs_decision": args["repairs_decision"], "authority_sha256": args["authority_sha256"]}
    elif case == "args_extra":
        args = {**args, "extra": True}
    elif case == "index_bool":
        args = {**args, "repairs_decision": True}
    elif case == "index_range":
        args = {**args, "repairs_decision": 9}
    elif case in {"authority_sha", "source_sha"}:
        args = {**args, case: "0" * 64}
    elif case == "why":
        why = "Short."
    elif case == "suspended":
        store.set_rule_state(RULE, "suspended", "User stopped the rule")
    elif case in {"deferred", "applying"}:
        column("status", case)
    elif case in {"urgent", "owner_profile", "owner_job", "blocks", "section", "source", "project", "key"}:
        name, value = {
            "urgent": ("urgent_rule", "consent"),
            "owner_profile": ("owner_profile", ""),
            "owner_job": ("owner_job", "job"),
            "blocks": ("blocks_json", '["t_abcdef12"]'),
            "section": ("section", "suggested"),
            "source": ("source", "idea"),
            "project": ("project", "other"),
            "key": ("key", "wrong"),
        }[case]
        column(name, value)
    elif case == "cycle_invalid":
        column("cycle", "not-a-cycle")
    elif case in {"reasons", "stage", "verdict", "disposition", "epoch", "receipt", "finding", "artifacts"}:
        name, value = {
            "reasons": ("reasons", ["outcome_review_ready", "cards_done"]),
            "stage": ("stage", "exploring"),
            "verdict": ("verdict", "win"),
            "disposition": ("disposition", "continue"),
            "epoch": ("epoch", "other-epoch"),
            "receipt": ("receipt", "2026-10-01T00:00:00Z"),
            "finding": ("finding", "met"),
            "artifacts": ("artifacts", {"evidence": "0" * 64, "proposal": "0" * 64}),
        }[case]
        basis = store.get(sid)["basis"]
        basis[name] = value
        store.refresh_basis(sid, basis)
    elif case == "stale_options":
        basis = store.get(sid)["basis"]
        basis["epoch"] = "other-epoch"
        store.refresh_basis(sid, basis)
    elif case == "no_options":
        with store._connect() as conn:
            conn.execute("DELETE FROM item_versions WHERE short_id=?", (sid,))
    elif case == "needs_options":
        monkeypatch.setattr(store, "needs_options", lambda: [store.get(sid)])
    elif case == "answer":
        store.answer(sid, "C", surface="tab")
        column("status", "open")
    elif case == "recommend_stage":
        with store._connect() as conn:
            conn.execute("UPDATE item_versions SET recommend='B' WHERE short_id=?", (sid,))
    elif case in {"lane", "board", "title", "extra_arg"}:
        key, value = {
            "lane": ("assignee", "coder"),
            "board": ("board", "other"),
            "title": ("title", "  "),
            "extra_arg": ("extra", True),
        }[case]
        options(lambda opts: opts[0]["args"].update({key: value}))
    elif case == "confirm":
        options(lambda opts: opts[0].update(confirm=True))
    elif case == "human_gate":
        options(lambda opts: opts[2].update(action="kanban_unblock"))
    elif case == "parse_error":
        write(env["root"] / "ideas" / "bad.md", "---\n[broken\n---\n")
    elif case == "duplicate_id":
        write(env["root"] / "in-demo" / "cycles" / "copy.md", env["target"].read_text(encoding="utf-8"))
    elif case == "stale_basis":
        with kbc.connect(board="default") as conn:
            conn.execute("UPDATE tasks SET status='running'")
    elif case == "paused":
        _rewrite(env["root"] / "in-demo" / "initiative.md", lambda fm: fm.update(status="paused"))
    elif case == "policy_disabled":
        _rewrite(env["root"] / "in-demo" / "initiative.md", lambda fm: fm["outcome_policy"].update(enabled=False))
    elif case == "recorded_concluded":
        _rewrite(env["target"], lambda fm: fm.update(stage="concluded"))
    elif case in {"artifact_sha", "artifact_missing", "artifact_symlink"}:
        evidence = env["evidence"]
        if case == "artifact_sha":
            evidence.write_text("Tampered evidence.", encoding="utf-8")
        elif case == "artifact_missing":
            evidence.unlink()
        else:
            copy = env["root"].parent / "artifact-copy.md"
            copy.write_bytes(evidence.read_bytes())
            evidence.unlink()
            evidence.symlink_to(copy)
    elif case in {"cycle_symlink", "initiative_symlink"}:
        path = env["target"] if case == "cycle_symlink" else env["root"] / "in-demo" / "initiative.md"
        copy = env["root"].parent / f"{case}.md"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
    elif case == "authority_newer":
        column("opened_at", (dt.date.today() - dt.timedelta(days=3)).isoformat() + "T09:00:00+00:00")
        _rewrite(
            env["target"],
            lambda fm: fm["decisions"][0].update(date=(dt.date.today() - dt.timedelta(days=1)).isoformat()),
        )
        args = _args(env)
    elif case == "initiative_later":
        _rewrite(
            env["root"] / "in-demo" / "initiative.md",
            lambda fm: fm["decisions"].append(
                {"date": "2026-09-02", "by": "user", "decision": "Restart demo on a narrower scope."}
            ),
        )
        args = _args(env)
    elif case == "basis_omits_verdict":
        # A stored basis whose verdict the collector left null, while the live cycle now records one.
        basis = store.get(sid)["basis"]
        basis["verdict"] = None
        with store._connect() as conn:
            conn.execute("UPDATE items SET basis_json=? WHERE short_id=?", (json.dumps(basis, sort_keys=True), sid))
        _rewrite(env["target"], lambda fm: fm["result"].update(verdict="met"))
    elif case == "cap":
        other, _ = store.upsert_item("request:other", section="gated", source="request:test", title="Other", basis={})
        for _ in range(3):
            store.log_action(None, other["short_id"], f"settle:{RULE}", {}, "settled", receipt="Earlier plan")
    elif case == "chain":
        _rewrite(
            env["target"],
            lambda fm: fm["decisions"].append(
                {"date": "2026-10-24", "by": "agent", "rule": RULE, "review": "D0", "decision": "Earlier plan"}
            ),
        )
    elif case == "failed_action":
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        with monkeypatch.context() as clock:
            clock.setattr(reviews, "now_iso", lambda: yesterday + "T12:00:00+00:00")
            store.log_action(None, sid, f"settle:{RULE}", {}, "failed", error="Create result unknown")
    elif case in {"no_lane", "no_board"}:
        env["project"] = env["project"].model_copy(update={"plan_assignee" if case == "no_lane" else "kanban_board": None})
    elif case.startswith("protected_"):
        action, option_args = PROTECTED_ALTERNATIVES[case.removeprefix("protected_")]
        options(lambda opts: opts[1].update(action=action, args=option_args))
    elif case == "malformed_options":
        with store._connect() as conn:
            conn.execute("UPDATE item_versions SET options_json=? WHERE short_id=?", (json.dumps("not-a-list"), sid))
    elif case == "malformed_option":
        options(lambda opts: opts.__setitem__(1, "acknowledge"))
    elif case == "malformed_args":
        options(lambda opts: opts[1].update(args="acknowledge"))
    elif case == "malformed_fields":
        options(lambda opts: opts[1].update(action="set_cycle_fields", args={"cycle": CYCLE, "fields": ["result"]}))

    before, tasks = _snapshot(env), set(_tasks())
    refusal = None
    try:
        _settle(env, args, why)
    except review_settle.SettleRefused as exc:
        refusal = exc
    assert refusal is not None, f"{case}: settled instead of refusing; created {sorted(set(_tasks()) - tasks)}"
    assert re.search(message, str(refusal)), str(refusal)
    assert set(_tasks()) == tasks, "a refusal creates no card"
    assert _snapshot(env) == before, "a refusal writes nothing"


def test_plan_outcome_followup_chain_guard_needs_a_later_user_decision(outcome):
    env = outcome
    _settle(env)
    first_card = _fm(env["target"])["kanban_cards"][-1]
    with kbc.connect(board="default") as conn:
        kb.complete_task(conn, first_card, result="Repair plan delivered")
    epoch = _epoch(env)
    writers.record_observation(
        env["root"],
        CYCLE,
        {
            "kind": "assessment",
            "epoch": epoch,
            "status": "missed",
            "detail": "Still 118 clicks after the follow-up plan.",
            "evidence": str(env["evidence"]),
            "proposal": str(env["proposal"]),
        },
    )
    _sync(env)
    env["sid"] = env["store"].get_by_key(f"cycle:demo:{CYCLE}:gate")["short_id"]
    _options(env)
    before = _snapshot(env)
    with pytest.raises(review_settle.SettleRefused, match="chain guard"):
        _settle(env)
    assert _snapshot(env) == before

    _rewrite(
        env["target"],
        lambda fm: fm["decisions"].append(
            # Same day as the item opened: a decision dated after the item opened is refused as newer than the
            # question, and the chain guard here is positional (a user row after the earlier rule audit).
            {"date": dt.date.today().isoformat(), "by": "user", "decision": "Plan the next repair after this assessment."}
        ),
    )
    epoch = _epoch(env)
    writers.record_observation(
        env["root"],
        CYCLE,
        {
            "kind": "assessment",
            "epoch": epoch,
            "status": "missed",
            "detail": "Still 118 clicks; the user asked for the next repair plan.",
            "evidence": str(env["evidence"]),
            "proposal": str(env["proposal"]),
        },
    )
    _sync(env)
    env["sid"] = env["store"].get_by_key(f"cycle:demo:{CYCLE}:gate")["short_id"]
    _options(env)
    result = _settle(env)
    assert result["rule"] == RULE
    audits = [row for row in _fm(env["target"])["decisions"] if row.get("rule") == RULE]
    assert len(audits) == 2 and audits[-1]["authority_sha256"] == _args(env)["authority_sha256"]
    assert len(_fm(env["target"])["kanban_cards"]) == 3


def test_plan_outcome_followup_failure_reports_its_card_and_does_not_retry_blindly(outcome, monkeypatch):
    env = outcome
    original = writers.edit_frontmatter

    def fail(*_args, **_kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(writers, "edit_frontmatter", fail)
    tasks = set(_tasks())
    with pytest.raises(review_settle.ActionFailed, match="injected write failure"):
        _settle(env)
    created = set(_tasks()) - tasks
    assert len(created) == 1
    card = created.pop()
    assert _fm(env["target"])["kanban_cards"] == [env["card"]], "the link is not written without the audit row"
    assert not [row for row in _fm(env["target"])["decisions"] if row.get("rule") == RULE], "no audit row"
    assert env["store"].get(env["sid"])["status"] == "open"
    actions = env["store"].actions(env["sid"])
    assert [(a["action"], a["state"]) for a in actions] == [(f"settle:{RULE}", "failed")]
    assert card in actions[0]["error"] and card in actions[0]["error"].split(":")[0]

    monkeypatch.setattr(writers, "edit_frontmatter", original)
    snapshot = _snapshot(env)
    with pytest.raises(review_settle.SettleRefused, match="chain guard"):
        _settle(env)
    assert _snapshot(env) == snapshot
    assert set(_tasks()) == tasks | {card}, "no blind second card"


def test_plan_outcome_followup_race_after_create_writes_no_stale_audit(outcome):
    """A source edit that lands while the card is created must refuse the link and the audit row, not record both."""
    env = outcome
    original = review_exec.default_runner

    def racing(args, stdin=None):
        proc = original(args, stdin)
        if proc.returncode == 0 and args[:1] == ["kanban"] and "create" in args:
            _rewrite(
                env["target"],
                lambda fm: fm["decisions"].append(
                    {"date": dt.date.today().isoformat(), "by": "user", "decision": "A decision the plan did not read."}
                ),
            )
        return proc

    args = _args(env)
    tasks = set(_tasks())
    with pytest.raises(review_settle.ActionFailed, match="changed before the link") as raised:
        review_settle.Settler(env["store"], env["project"], run=racing).settle(env["sid"], RULE, args, WHY)

    created = set(_tasks()) - tasks
    assert len(created) == 1
    card = created.pop()
    fm = _fm(env["target"])
    assert not [row for row in fm["decisions"] if row.get("rule") == RULE], "no stale agent audit row"
    assert card not in fm["kanban_cards"], "the race-created card is not linked to the changed source"
    assert env["store"].get(env["sid"])["status"] == "open"
    actions = env["store"].actions(env["sid"])
    assert [(a["action"], a["state"]) for a in actions] == [(f"settle:{RULE}", "failed")]
    assert card in actions[0]["error"] and card in str(raised.value)


def test_plan_outcome_followup_race_on_the_initiative_writes_no_stale_audit(outcome):
    """The initiative carries the authority guard; an edit to it during the create must refuse under the lock."""
    env = outcome
    original = review_exec.default_runner
    initiative = env["root"] / "in-demo" / "initiative.md"

    def racing(args, stdin=None):
        proc = original(args, stdin)
        if proc.returncode == 0 and args[:1] == ["kanban"] and "create" in args:
            _rewrite(
                initiative,
                lambda fm: fm["decisions"].append(
                    {"date": "2026-09-02", "by": "user", "decision": "A later decision the plan did not read."}
                ),
            )
        return proc

    args = _args(env)
    tasks = set(_tasks())
    with pytest.raises(review_settle.ActionFailed, match="initiative changed before the link") as raised:
        review_settle.Settler(env["store"], env["project"], run=racing).settle(env["sid"], RULE, args, WHY)

    created = set(_tasks()) - tasks
    assert len(created) == 1
    card = created.pop()
    fm = _fm(env["target"])
    assert not [row for row in fm["decisions"] if row.get("rule") == RULE], "no stale agent audit row"
    assert card not in fm["kanban_cards"]
    assert card in str(raised.value)


def test_plan_outcome_followup_claim_blocks_a_concurrent_answer(outcome, monkeypatch):
    env = outcome
    other = reviews.ReviewStore(env["store"].path)
    original = writers.edit_frontmatter

    def during_write(*args, **kwargs):
        assert other.get(env["sid"])["status"] == "applying"
        answer = other.answer(env["sid"], "C", surface="tab")
        assert answer["accepted"] is False and "applying" in answer["reason"]
        return original(*args, **kwargs)

    monkeypatch.setattr(writers, "edit_frontmatter", during_write)
    _settle(env)
    assert env["store"].get(env["sid"])["status"] == "settled" and other.answers(env["sid"]) == []


def test_plan_outcome_followup_overrule_suspends_the_rule(outcome):
    from hermes_initiatives_core import review_learn

    env = outcome
    _settle(env)
    result = review_learn.overrule(env["store"], [env["project"]], env["sid"], "This plan should wait for me.")
    assert result["rule"] == RULE
    assert env["store"].rule_states()[RULE]["state"] == "suspended"
    followup = env["store"].get(result["follow_up"])
    assert followup["source"] == "learn"
    assert followup["version"]["options"][1]["args"] == {"rule": RULE, "state": "active"}


def test_policy_change_during_preflight_refuses_before_card_create(outcome, monkeypatch):
    from hermes_initiatives_core import review_outcome_settle as rule

    env = outcome
    original = rule.fresh_doc

    def changed(*a, **kw):
        doc = original(*a, **kw)
        _rewrite(env["root"] / "in-demo/initiative.md", lambda fm: fm["outcome_policy"].update(enabled=False))
        return doc

    monkeypatch.setattr(rule, "fresh_doc", changed)
    tasks = set(_tasks())
    with pytest.raises(review_settle.SettleRefused, match="initiative.*changed"):
        _settle(env)
    assert set(_tasks()) == tasks
    assert not [row for row in _fm(env["target"])["decisions"] if row.get("rule") == RULE]
