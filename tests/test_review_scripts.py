"""The skill CLIs end to end: request, collect, write, digest. Runs the real scripts in subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SUMMARY, cycle_md, write

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"


@pytest.fixture
def env(isolated_hermes, root, tmp_path):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", stage="exploring"))
    data = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(root)}]}))
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def run(env, script, *args, stdin=None):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc


def test_request_then_collect_write_digest(env, tmp_path):
    opt_a = 'A|Pay now|{"action": "handoff", "args": {}}'
    opt_b = 'B|Check first|{"action": "defer", "args": {"kind": "state_change"}}'
    filed = run(
        env,
        "request_decision.py",
        "--source",
        "calendar-sync",
        "--key",
        "req:calendar-sync:evt-1",
        "--title",
        "NLB charge",
        "--question",
        "Pay or check?",
        "--option",
        opt_a,
        "--option",
        opt_b,
        "--recommend",
        "B",
        "--basis-json",
        '{"email": "x"}',
    )
    assert filed.returncode == 0, filed.stderr
    again = run(
        env,
        "request_decision.py",
        "--source",
        "calendar-sync",
        "--key",
        "req:calendar-sync:evt-1",
        "--title",
        "NLB charge",
        "--question",
        "Pay or check?",
        "--option",
        opt_a,
        "--option",
        opt_b,
        "--recommend",
        "B",
        "--basis-json",
        '{"email": "x"}',
    )
    assert json.loads(filed.stdout)["short_id"] == json.loads(again.stdout)["short_id"]
    assert json.loads(again.stdout)["created"] is False

    collected = run(env, "daily_review.py", "collect", "--trigger", "manual")
    assert collected.returncode == 0, collected.stderr
    data = json.loads(collected.stdout)
    needs = [i["short_id"] for i in data["items"] if i["needs_options"]]
    assert len(needs) == 1, "the cycle gate needs options; the request already has them"
    output = {
        "review_id": data["review_id"],
        "items": {
            needs[0]: {
                "question": "Run c01?",
                "summary": SUMMARY,
                "recommend": "A",
                "why": "x",
                "options": [
                    {
                        "id": "A",
                        "label": "Run it",
                        "action": "set_cycle_fields",
                        "args": {"cycle": "in-demo-c01", "fields": {"next_action": "Run c01"}},
                    },
                    {"id": "B", "label": "Not now", "action": "defer", "args": {}},
                ],
            }
        },
        "summaries": {"in-demo": "Goal on track."},
    }
    out = tmp_path / "out.json"
    out.write_text(json.dumps(output))
    written = run(env, "daily_review.py", "write", "--file", str(out))
    assert written.returncode == 0, written.stdout + written.stderr
    from hermes_initiatives_core.reviews import ReviewStore

    store = ReviewStore()
    store.log_action(None, needs[0], "repair_review_gate_closure", {}, "applied", receipt="Historical repair")
    assert store.actions(needs[0])[-1]["action"] == "repair_review_gate_closure"
    assert store.actions_since(None)[-1]["receipt"] == "Historical repair"
    digest = run(env, "daily_review.py", "digest")
    assert digest.returncode == 0, digest.stderr
    assert "Run c01?" in digest.stdout and "Pay or check?" in digest.stdout and "Goal on track." in digest.stdout


def test_tick_instructions_carry_host_paths(env):
    proc = run(env, "daily_review.py", "instructions", "--tick", "--target", "telegram:1:2")
    assert proc.returncode == 0, proc.stderr
    assert "<scripts>" not in proc.stdout and "<gates>" not in proc.stdout and "<target>" not in proc.stdout
    assert "daily_review.py tick --gates" in proc.stdout and "telegram:1:2" in proc.stdout
    assert "review-work/gates.json" in proc.stdout


def test_tick_prints_only_items_needing_options_and_creates_no_review(env, tmp_path):
    first = run(env, "daily_review.py", "tick", "--gates", str(tmp_path / "missing.json"))
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout)
    assert [i["key"] for i in data["items"]] == ["cycle:demo:in-demo-c01:gate"]
    assert data["handoff_cards"] == [], "no answered session request, no card"
    assert "gate list missing" in first.stderr, "a missing gate list is reported, not fatal"
    sid = data["items"][0]["short_id"]
    out = tmp_path / "tick-out.json"
    out.write_text(json.dumps({"items": {sid: {"watch": "agent_owned: t_12345678 owns the next step"}}}))
    assert run(env, "daily_review.py", "write", "--file", str(out)).returncode == 0
    assert json.loads(run(env, "daily_review.py", "tick", "--gates", str(tmp_path / "missing.json")).stdout)["items"] == []
    assert json.loads(run(env, "daily_review.py", "status").stdout)["latest_review"] is None


def test_write_rejects_incomplete_output(env, tmp_path):
    data = json.loads(run(env, "daily_review.py", "collect").stdout)
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"review_id": data["review_id"], "items": {}}))
    written = run(env, "daily_review.py", "write", "--file", str(out))
    assert written.returncode == 2 and "missing" in written.stdout


def review_snapshot(path):
    import sqlite3

    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}


def test_request_refuses_gate_keys(env):
    proc = run(
        env,
        "request_decision.py",
        "--source",
        "x",
        "--key",
        "kanban:b:t_1:gate",
        "--title",
        "t",
        "--question",
        "q",
        "--option",
        'A|a|{"action": "acknowledge"}',
        "--option",
        'B|b|{"action": "defer"}',
        "--recommend",
        "A",
        "--basis-json",
        "{}",
    )
    assert proc.returncode == 2 and "req:" in proc.stderr


def test_learn_and_overrule_cli(env):
    learned = run(env, "daily_review.py", "learn")
    assert learned.returncode == 0, learned.stderr
    assert json.loads(learned.stdout) == {"ok": True, "candidates": [], "filed": [], "rule_states": {}}
    refused = run(env, "daily_review.py", "overrule", "D1", "--reason", "It was not answered.")
    assert refused.returncode != 0 and "unknown item" in (refused.stdout + refused.stderr)


@pytest.fixture
def wait_cli(env, root, tmp_path):
    import hashlib

    from conftest import DECIDED

    for name in ("HERMES_HOME", "HERMES_KANBAN_HOME", "HERMES_INITIATIVES_DATA_DIR"):
        assert Path(env[name]).resolve().is_relative_to(tmp_path.resolve()), name
    settings = json.loads((Path(env["HERMES_INITIATIVES_DATA_DIR"]) / "settings.json").read_text())
    assert Path(settings["projects"][0]["root"]).resolve() == root.resolve()
    prerequisite = {"kind": "cycle_stage", "cycle": "in-demo-c02", "equals": "measuring"}
    row = {
        "date": "2026-10-14",
        "by": "user",
        "decision": "Park the review of in-demo-c01 until in-demo-c02 reaches measuring.",
        "review_deferral": {
            "schema_version": 1,
            "rule": "defer_exploring_cycle",
            "purpose": "routine_prerequisite_wait",
            "cycle": "in-demo-c01",
            "prerequisite": prerequisite,
        },
    }
    target = write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01", decisions=[row]))
    dep = write(root / "in-demo" / "cycles" / "in-demo-c02.md", cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    (root / ".review-write.lock").touch()
    gates = write(
        tmp_path / "gates.json",
        json.dumps(
            {
                "board": "synthetic",
                "cards": [],
                "generated_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
            }
        ),
    )
    tick = run(env, "daily_review.py", "tick", "--gates", str(gates))
    assert tick.returncode == 0, tick.stderr
    sid = next(i["short_id"] for i in json.loads(tick.stdout)["items"] if i["cycle"] == "in-demo-c01")
    options = [
        {"id": i, "label": label, "action": "defer", "args": {"kind": "state_change", "prerequisite": prerequisite}}
        for i, label in [("A", "Park the review"), ("B", "Keep the review parked")]
    ]
    payload = {
        "items": {
            sid: {
                "question": "Park the review?",
                "why": "Routine wait.",
                "summary": SUMMARY,
                "recommend": "A",
                "options": options,
            }
        }
    }
    for item in json.loads(tick.stdout)["items"]:
        if item["short_id"] != sid:
            payload["items"][item["short_id"]] = {"watch": "agent_owned: synthetic control waits for fixture owner"}
    written = run(env, "daily_review.py", "write", "--file", "-", stdin=json.dumps(payload))
    assert written.returncode == 0, written.stdout + written.stderr
    args = {
        "authority_decision": 0,
        "authority_sha256": hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest(),
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    settle = write(
        tmp_path / "settle.json",
        json.dumps(
            {
                "settle": [
                    {
                        "short_id": sid,
                        "rule": "defer_exploring_cycle",
                        "args": args,
                        "why": "The latest user row authorizes the wait.",
                    }
                ]
            }
        ),
    )
    return {
        "env": env,
        "root": root,
        "sid": sid,
        "target": target,
        "dep": dep,
        "gates": gates,
        "settle": settle,
        "transcript": [],
    }


def _cli(wait, command, *args):
    proc = run(wait["env"], "daily_review.py", command, *args)
    wait["transcript"].append(
        {"argv": [command, *args], "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    )
    return proc


def _save_lane(wait, name):
    destination = os.environ.get("REVIEW_CLI_EVIDENCE_DIR")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(json.dumps(wait["transcript"], indent=2) + "\n")


def test_defer_exploring_cycle_cli_parking_and_return(wait_cli):
    from conftest import DECIDED

    from hermes_initiatives_core.reviews import ReviewStore

    wait = wait_cli
    parked = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert parked.returncode == 0, parked.stdout + parked.stderr
    assert json.loads(parked.stdout)["results"][0]["status"] == "deferred"
    replay = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert json.loads(replay.stdout)["results"][0]["status"] == "already_deferred"
    tick = _cli(wait, "tick", "--gates", str(wait["gates"]))
    assert [i["short_id"] for i in json.loads(tick.stdout)["deferred"]] == [wait["sid"]]
    write(wait["dep"], cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    reopened = _cli(wait, "reopen", wait["sid"], "--reason", "The prerequisite reached measuring in its current file.")
    assert reopened.returncode == 0, reopened.stdout + reopened.stderr
    tick = _cli(wait, "tick", "--gates", str(wait["gates"]))
    assert wait["sid"] in {i["short_id"] for i in json.loads(tick.stdout)["items"] if i["needs_options"]}
    store = ReviewStore(Path(wait["env"]["HERMES_INITIATIVES_DATA_DIR"]) / "reviews.db")
    assert len([a for a in store.actions(wait["sid"]) if a["action"] == "settle:defer_exploring_cycle"]) == 1
    _save_lane(wait, "cli-parking-head.json")


def test_defer_exploring_cycle_cli_refusal_and_recovery(wait_cli, tmp_path):
    import sqlite3

    from hermes_initiatives_core.docs import parse_markdown
    from hermes_initiatives_core.reviews import ReviewStore

    wait = wait_cli
    store = ReviewStore(Path(wait["env"]["HERMES_INITIATIVES_DATA_DIR"]) / "reviews.db")

    def snapshot():
        with sqlite3.connect(store.path) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            rows = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
        return rows, {str(p): p.read_bytes() for p in wait["root"].rglob("*") if p.is_file()}

    version = store.get(wait["sid"])["version"]
    options = json.loads(json.dumps(version["options"]))
    options[1]["label"] = "Approve release"
    store.add_version(wait["sid"], question="Park the review?", options=options, recommend="A", context="Routine wait.")
    before = snapshot()
    refused = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert refused.returncode == 2 and json.loads(refused.stdout)["results"][0]["status"] == "refused"
    assert snapshot() == before
    store.add_version(
        wait["sid"], question="Park the review?", options=version["options"], recommend="A", context="Routine wait."
    )
    store.refresh_basis(wait["sid"], {"changed": True})
    before = snapshot()
    refused = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert json.loads(refused.stdout)["results"][0]["status"] == "refused"
    assert snapshot() == before
    store.refresh_basis(
        wait["sid"],
        {"cycle": "in-demo-c01", "stage": "exploring", "reasons": ["exploring"], "verdict": "unset", "disposition": "unset"},
    )
    store.set_rule_state("defer_exploring_cycle", "suspended", "User suspended this rule")
    before = snapshot()
    refused = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert json.loads(refused.stdout)["results"][0]["status"] == "refused"
    assert snapshot() == before
    store.set_rule_state("defer_exploring_cycle", "active", "User restored this rule")
    injector = write(
        tmp_path / "inject_after_audit.py",
        """
        import runpy
        import sys

        from hermes_initiatives_core import review_deferral
        original = review_deferral.atomic_write
        def fail(path, text):
            original(path, text)
            raise OSError("injected after audit")
        review_deferral.atomic_write = fail
        script, payload = sys.argv[1:]
        sys.argv = [script, "settle", "--file", payload]
        sys.path.insert(0, str(__import__("pathlib").Path(script).parent))
        runpy.run_path(script, run_name="__main__")
    """,
    )
    failed = subprocess.run(
        [sys.executable, str(injector), str(SCRIPTS / "daily_review.py"), str(wait["settle"])],
        env=wait["env"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    wait["transcript"].append(
        {"argv": ["injected settle"], "returncode": failed.returncode, "stdout": failed.stdout, "stderr": failed.stderr}
    )
    assert failed.returncode == 2 and json.loads(failed.stdout)["results"][0]["status"] == "failed"
    assert store.get(wait["sid"])["status"] == "applying"
    recovered = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert store.get(wait["sid"])["status"] == "deferred" and store.answers(wait["sid"]) == []
    assert len(store.actions(wait["sid"])) == 1
    fm = parse_markdown(wait["target"].read_text(), wait["target"]).frontmatter
    assert len([r for r in fm["decisions"] if r.get("rule") == "defer_exploring_cycle"]) == 1
    wait["transcript"].append(
        {
            "readback": {
                "status": store.get(wait["sid"])["status"],
                "actions": store.actions(wait["sid"]),
                "answers": store.answers(wait["sid"]),
                "decisions": fm["decisions"],
            }
        }
    )
    _save_lane(wait, "cli-refusal-and-recovery.json")


FOCUS_RULE = "defer_behind_focus_cycle"
FOCUS_WHY = "The initiative's focus cycle in-demo-c02 is still shipping, so this decided cycle waits its turn."


@pytest.fixture
def focus_cli(env, root, tmp_path):
    """A decided no-cards cycle waiting behind another cycle of its initiative, driven through the real CLI."""
    import datetime as dt

    from conftest import DECIDED, MEASUREMENT

    from hermes_initiatives_core.docs import dump_markdown, parse_markdown

    # Complete contracts keep each cycle to its single card gate, so this fixture exercises parking only.
    target = write(
        root / "in-demo" / "cycles" / "in-demo-c01.md",
        cycle_md("in-demo-c01", stage="decided", decisions=DECIDED, measurement=MEASUREMENT),
    )
    focus_cycle = write(
        root / "in-demo" / "cycles" / "in-demo-c02.md",
        cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED, measurement=MEASUREMENT),
    )
    brief = root / "in-demo" / "initiative.md"
    doc = parse_markdown(brief.read_text(encoding="utf-8"), brief)
    doc.frontmatter["focus_cycle"] = "in-demo-c02"
    write(brief, dump_markdown(doc.frontmatter, doc.body))
    (root / ".review-write.lock").touch()
    gates = write(
        tmp_path / "gates.json",
        json.dumps({"board": "synthetic", "cards": [], "generated_at": dt.datetime.now(dt.UTC).isoformat()}),
    )
    tick = run(env, "daily_review.py", "tick", "--gates", str(gates))
    assert tick.returncode == 0, tick.stderr
    sid = next(i["short_id"] for i in json.loads(tick.stdout)["items"] if i["cycle"] == "in-demo-c01")
    options = [
        {
            "id": "A",
            "label": "Start it now",
            "action": "set_cycle_fields",
            "args": {"cycle": "in-demo-c01", "fields": {"stage": "shipping"}},
        },
        {"id": "B", "label": "Keep it behind the focus cycle", "action": "defer", "args": {"kind": "state_change"}},
    ]
    payload = {
        "items": {
            sid: {
                "question": "Start in-demo-c01 while in-demo-c02 is the focus cycle?",
                "why": "The cycle is decided with no cards; nothing else has moved.",
                "summary": SUMMARY,
                "recommend": "B",
                "options": options,
            }
        }
    }
    for item in json.loads(tick.stdout)["items"]:
        if item["short_id"] != sid:
            payload["items"][item["short_id"]] = {"watch": "agent_owned: synthetic control waits for the focus cycle"}
    written = run(env, "daily_review.py", "write", "--file", "-", stdin=json.dumps(payload))
    assert written.returncode == 0, written.stdout + written.stderr
    settle = write(
        tmp_path / "settle-focus.json",
        json.dumps({"settle": [{"short_id": sid, "rule": FOCUS_RULE, "args": {}, "why": FOCUS_WHY}]}),
    )
    return {
        "env": env,
        "root": root,
        "sid": sid,
        "target": target,
        "focus_cycle": focus_cycle,
        "brief": brief,
        "gates": gates,
        "settle": settle,
        "transcript": [],
    }


def test_defer_behind_focus_cycle_cli_focus_wait(focus_cli):
    from conftest import DECIDED, MEASUREMENT

    from hermes_initiatives_core.reviews import ReviewStore

    wait = focus_cli
    parked = _cli(wait, "settle", "--file", str(wait["settle"]))
    second = _cli(wait, "tick", "--gates", str(wait["gates"]))
    write(
        wait["focus_cycle"],
        cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED, measurement=MEASUREMENT),
    )
    third = _cli(wait, "tick", "--gates", str(wait["gates"]))
    store = ReviewStore(Path(wait["env"]["HERMES_INITIATIVES_DATA_DIR"]) / "reviews.db")
    second_tick, third_tick = json.loads(second.stdout), json.loads(third.stdout)
    wait["transcript"].append(
        {
            "readback": {
                "status": store.get(wait["sid"])["status"],
                "actions": store.actions(wait["sid"]),
                "answers": store.answers(wait["sid"]),
                "second_tick_items": [i["short_id"] for i in second_tick["items"]],
                "second_tick_deferred": [i["short_id"] for i in second_tick["deferred"]],
                "third_tick_reopened": third_tick["sync"]["reopened"],
            }
        }
    )
    _save_lane(wait, "cli-focus-wait-head.json")
    assert parked.returncode == 0, parked.stdout + parked.stderr
    assert json.loads(parked.stdout)["results"][0]["status"] == "deferred"
    assert [i["short_id"] for i in second_tick["deferred"]] == [wait["sid"]]
    assert second_tick["items"] == [], "a parked item stays out of the triage list"
    assert third_tick["sync"]["reopened"] == [wait["sid"]]
    items = {i["short_id"]: i for i in third_tick["items"]}
    assert items[wait["sid"]]["needs_options"] is True
    assert [a["action"] for a in store.actions(wait["sid"])] == [f"settle:{FOCUS_RULE}", "reopen_deferral"]
    assert store.answers(wait["sid"]) == []


def test_defer_behind_focus_cycle_cli_refusal_failure_and_overrule(focus_cli, tmp_path):
    import sqlite3

    from conftest import DECIDED

    from hermes_initiatives_core.docs import parse_markdown
    from hermes_initiatives_core.reviews import ReviewStore

    wait = focus_cli
    store = ReviewStore(Path(wait["env"]["HERMES_INITIATIVES_DATA_DIR"]) / "reviews.db")

    def snapshot():
        with sqlite3.connect(store.path) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            rows = {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
        return rows, {str(p): p.read_bytes() for p in wait["root"].rglob("*") if p.is_file()}

    # 1. A refused case: the focus cycle is already measuring, so the wait does not hold.
    write(wait["focus_cycle"], cycle_md("in-demo-c02", stage="measuring", decisions=DECIDED))
    before = snapshot()
    refused = _cli(wait, "settle", "--file", str(wait["settle"]))
    assert refused.returncode == 2, refused.stdout + refused.stderr
    result = json.loads(refused.stdout)["results"][0]
    assert result["status"] == "refused" and "is measuring" in result["error"]
    assert snapshot() == before, "a refusal writes nothing"

    # 2. An eligible case whose audit write fails: open, one failed action, no deferral.
    write(wait["focus_cycle"], cycle_md("in-demo-c02", stage="shipping", decisions=DECIDED))
    injector = write(
        tmp_path / "inject_focus_audit.py",
        """
        import runpy
        import sys

        from hermes_initiatives_core import review_settle

        def fail(project, path, row):
            raise OSError("injected audit write failure")
        review_settle._append_row = fail
        script, payload = sys.argv[1:]
        sys.argv = [script, "settle", "--file", payload]
        sys.path.insert(0, str(__import__("pathlib").Path(script).parent))
        runpy.run_path(script, run_name="__main__")
    """,
    )
    failed = subprocess.run(
        [sys.executable, str(injector), str(SCRIPTS / "daily_review.py"), str(wait["settle"])],
        env=wait["env"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    wait["transcript"].append(
        {"argv": ["injected settle"], "returncode": failed.returncode, "stdout": failed.stdout, "stderr": failed.stderr}
    )
    assert failed.returncode == 2 and json.loads(failed.stdout)["results"][0]["status"] == "failed"
    assert store.get(wait["sid"])["status"] == "open"
    assert [(a["action"], a["state"]) for a in store.actions(wait["sid"])] == [(f"settle:{FOCUS_RULE}", "failed")]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM deferrals WHERE short_id = ?", (wait["sid"],)).fetchone()[0] == 0

    # 3. A park, then the user's overrule: the rule is suspended and the item returns open.
    parked = _cli(wait, "settle", "--file", str(wait["settle"]))
    overruled = _cli(wait, "overrule", wait["sid"], "--reason", "This cycle should not wait behind that focus cycle.")
    fm = parse_markdown(wait["target"].read_text(encoding="utf-8"), wait["target"]).frontmatter
    wait["transcript"].append(
        {
            "readback": {
                "status": store.get(wait["sid"])["status"],
                "rule_state": store.rule_states().get(FOCUS_RULE),
                "actions": store.actions(wait["sid"]),
                "answers": store.answers(wait["sid"]),
                "agent_rows": [r for r in fm["decisions"] if r.get("by") == "agent"],
            }
        }
    )
    _save_lane(wait, "cli-focus-wait-refusal-overrule.json")
    assert json.loads(parked.stdout)["results"][0]["status"] == "deferred"
    assert overruled.returncode == 0, overruled.stdout + overruled.stderr
    done = json.loads(overruled.stdout)
    assert done["ok"] is True and done["rule"] == FOCUS_RULE
    assert store.rule_states()[FOCUS_RULE]["state"] == "suspended"
    assert store.get(wait["sid"])["status"] == "open"
    assert [a["action"] for a in store.actions(wait["sid"])] == [
        f"settle:{FOCUS_RULE}",
        f"settle:{FOCUS_RULE}",
        "overrule",
        "reopen_deferral",
    ]
    assert len([r for r in fm["decisions"] if r.get("rule") == FOCUS_RULE]) == 1
    assert store.answers(wait["sid"]) == []


DONE_RULE = "plan_after_cards_done"
DONE_WHY = "All cards are done and the recommended next step belongs to the configured plan lane."


def _done_cli(wait, command, *args, stdin=None):
    source = Path(os.environ.get("REVIEW_CLI_SOURCE_ROOT", SCRIPTS.parents[2]))
    cli_env = {**wait["env"], "PYTHONPATH": str(source) + os.pathsep + wait["env"]["PYTHONPATH"]}
    proc = subprocess.run(
        [sys.executable, str(source / "skills/initiatives/scripts/daily_review.py"), command, *args],
        env=cli_env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    wait["transcript"].append(
        {"argv": [command, *args], "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    )
    return proc


def _done_tick_write(wait, cycle="in-demo-c01", action="kanban_create"):
    tick = _done_cli(wait, "tick", "--gates", str(wait["gates"]))
    assert tick.returncode == 0, tick.stdout + tick.stderr
    items = json.loads(tick.stdout)["items"]
    item = wait["store"].get_by_key(f"cycle:demo:{cycle}:gate")
    assert item is not None and item["status"] == "open", tick.stdout
    sid = item["short_id"]
    args = {"assignee": "planner", "title": "Next plan", "body": "Do the synthetic next step."}
    if action == "set_cycle_fields":
        args = {"cycle": cycle, "fields": {"stage": "shipping"}}
    payload = {
        "items": {
            sid: {
                "question": "Take the next step?",
                "why": DONE_WHY,
                "summary": SUMMARY,
                "recommend": "A",
                "options": [
                    {"id": "A", "label": "Next step", "action": action, "args": args},
                    {"id": "B", "label": "Wait", "action": "defer", "args": {}},
                ],
            }
        }
    }
    for item in items:
        if item["short_id"] != sid:
            payload["items"][item["short_id"]] = {"watch": "agent_owned: synthetic fixture owner handles this control"}
    written = _done_cli(wait, "write", "--file", "-", stdin=json.dumps(payload))
    assert written.returncode == 0, written.stdout + written.stderr
    wait["sid"] = sid
    write(wait["settle"], json.dumps({"settle": [{"short_id": sid, "rule": DONE_RULE, "args": {}, "why": DONE_WHY}]}))
    return sid


@pytest.fixture
def done_cli(env, root, tmp_path):
    import datetime as dt

    from conftest import DECIDED
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    from hermes_initiatives_core.reviews import ReviewStore

    for name in ("HERMES_HOME", "HERMES_KANBAN_HOME", "HERMES_INITIATIVES_DATA_DIR"):
        assert Path(env[name]).resolve().is_relative_to(tmp_path.resolve()), name
    kbc.init_db(board="default")
    with kbc.connect(board="default") as conn:
        card = kb.create_task(conn, title="Finished synthetic delivery", assignee="planner")
        kb.complete_task(conn, card, result="Done")
    settings = Path(env["HERMES_INITIATIVES_DATA_DIR"]) / "settings.json"
    settings.write_text(
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
    target = write(
        root / "in-demo/cycles/in-demo-c01.md", cycle_md("in-demo-c01", stage="decided", decisions=DECIDED, kanban_cards=[card])
    )
    gates = write(
        tmp_path / "gates.json",
        json.dumps({"board": "default", "cards": [], "generated_at": dt.datetime.now(dt.UTC).isoformat()}),
    )
    wait = {
        "env": env,
        "root": root,
        "target": target,
        "gates": gates,
        "settle": tmp_path / "settle-done.json",
        "transcript": [],
        "store": ReviewStore(),
        "initial_card": card,
    }
    _done_tick_write(wait)
    return wait


def _done_cli_cards():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect(board="default") as conn:
        return {task.id: task for task in kb.list_tasks(conn)}


def _done_cli_snapshot(wait):
    from hermes_cli import kanban_db as kb

    return (
        review_snapshot(wait["store"].path),
        review_snapshot(Path(kb.kanban_db_path("default"))),
        {str(p): p.read_bytes() for p in wait["root"].rglob("*") if p.is_file()},
    )


def _done_cli_complete(card):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect(board="default") as conn:
        kb.complete_task(conn, card, result="Synthetic next step done")


def test_plan_after_cards_done_cli_regression(done_cli):
    from hermes_initiatives_core.docs import parse_markdown

    wait = done_cli
    store, sid = wait["store"], wait["sid"]
    before = _done_cli_snapshot(wait)
    settled = _done_cli(wait, "settle", "--file", str(wait["settle"]))
    result = json.loads(settled.stdout)["results"][0]
    if os.environ.get("REVIEW_CLI_EXPECT_UNKNOWN") == "1":
        assert settled.returncode == 2 and result["status"] == "refused" and "unknown rule" in result["error"]
        assert _done_cli_snapshot(wait) == before
        tick = _done_cli(wait, "tick", "--gates", str(wait["gates"]))
        assert tick.returncode == 0, tick.stdout + tick.stderr
        again = _done_cli(wait, "settle", "--file", str(wait["settle"]))
        assert json.loads(again.stdout)["results"][0]["status"] == "refused"
        assert set(_done_cli_cards()) == {wait["initial_card"]}
        _save_lane(wait, "cli-plan-after-cards-done-trunk.json")
        return
    assert settled.returncode == 0 and result["ok"] is True, settled.stdout + settled.stderr
    cards = _done_cli_cards()
    assert len(cards) == 2
    card = (set(cards) - {wait["initial_card"]}).pop()
    assert result["receipt"] == f"{card} for planner, linked to in-demo-c01"
    assert cards[card].assignee == "planner"
    assert (cards[card].body or "").startswith("- Initiative cycle: in-demo-c01\n")
    fm = parse_markdown(wait["target"].read_text(), wait["target"]).frontmatter
    assert fm["stage"] == "decided" and fm["kanban_cards"] == [wait["initial_card"], card]
    assert fm["decisions"][-1]["rule"] == DONE_RULE and fm["decisions"][-1]["by"] == "agent"
    tick = _done_cli(wait, "tick", "--gates", str(wait["gates"]))
    assert tick.returncode == 0 and json.loads(tick.stdout)["items"] == []
    assert store.get(sid)["status"] == "settled" and store.answers(sid) == []
    _done_cli_complete(card)
    next_sid = _done_tick_write(wait)
    assert next_sid != sid
    before = _done_cli_snapshot(wait)
    refused = _done_cli(wait, "settle", "--file", str(wait["settle"]))
    result = json.loads(refused.stdout)["results"][0]
    assert refused.returncode == 2 and result["status"] == "refused" and "chain guard" in result["error"]
    assert _done_cli_snapshot(wait) == before
    wait["transcript"].append(
        {
            "readback": {
                "settled_item": store.get(sid),
                "next_item": store.get(next_sid),
                "actions": store.actions(sid),
                "answers": store.answers(sid),
                "cycle": fm,
            }
        }
    )
    _save_lane(wait, "cli-plan-after-cards-done-head.json")


def test_plan_after_cards_done_cli_failure_and_overrule(done_cli, tmp_path):
    from conftest import DECIDED

    wait = done_cli
    store = wait["store"]
    sid = _done_tick_write(wait, action="set_cycle_fields")
    before = _done_cli_snapshot(wait)
    refused = _done_cli(wait, "settle", "--file", str(wait["settle"]))
    result = json.loads(refused.stdout)["results"][0]
    assert refused.returncode == 2 and result["status"] == "refused" and "recommended" in result["error"]
    assert _done_cli_snapshot(wait) == before
    _done_tick_write(wait)
    injector = write(
        tmp_path / "inject_done_link.py",
        """
        import runpy
        import sys
        from hermes_initiatives_core import review_settle
        def fail(*args):
            raise OSError("injected link failure")
        review_settle.link_cards = fail
        script, payload = sys.argv[1:]
        sys.argv = [script, "settle", "--file", payload]
        sys.path.insert(0, str(__import__("pathlib").Path(script).parent))
        runpy.run_path(script, run_name="__main__")
    """,
    )
    failed = subprocess.run(
        [sys.executable, str(injector), str(SCRIPTS / "daily_review.py"), str(wait["settle"])],
        env=wait["env"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    wait["transcript"].append(
        {"argv": ["injected settle"], "returncode": failed.returncode, "stdout": failed.stdout, "stderr": failed.stderr}
    )
    result = json.loads(failed.stdout)["results"][0]
    cards = _done_cli_cards()
    assert len(cards) == 2
    card = (set(cards) - {wait["initial_card"]}).pop()
    assert failed.returncode == 2 and result["status"] == "failed" and card in result["error"]
    assert store.get(sid)["status"] == "open"
    actions = store.actions(sid)
    assert [(a["action"], a["state"]) for a in actions] == [(f"settle:{DONE_RULE}", "failed")]
    assert card in actions[0]["error"]
    before = _done_cli_snapshot(wait)
    retry = _done_cli(wait, "settle", "--file", str(wait["settle"]))
    result = json.loads(retry.stdout)["results"][0]
    assert retry.returncode == 2 and result["status"] == "refused" and "collector" in result["error"]
    assert _done_cli_snapshot(wait) == before
    tick = _done_cli(wait, "tick", "--gates", str(wait["gates"]))
    assert tick.returncode == 0 and store.get(sid)["status"] == "resolved_elsewhere"
    _done_cli_complete(card)
    next_sid = _done_tick_write(wait)
    before = _done_cli_snapshot(wait)
    chain = _done_cli(wait, "settle", "--file", str(wait["settle"]))
    result = json.loads(chain.stdout)["results"][0]
    assert chain.returncode == 2 and result["status"] == "refused" and "chain guard" in result["error"]
    assert _done_cli_snapshot(wait) == before
    assert set(_done_cli_cards()) == set(cards)
    write(
        wait["root"] / "in-demo/cycles/in-demo-c02.md",
        cycle_md("in-demo-c02", stage="decided", decisions=DECIDED, kanban_cards=[wait["initial_card"]]),
    )
    second_sid = _done_tick_write(wait, cycle="in-demo-c02")
    settled = _done_cli(wait, "settle", "--file", str(wait["settle"]))
    assert settled.returncode == 0 and json.loads(settled.stdout)["results"][0]["ok"] is True
    overruled = _done_cli(wait, "overrule", second_sid, "--reason", "This next step needs my own decision first.")
    assert overruled.returncode == 0, overruled.stdout + overruled.stderr
    assert store.rule_states()[DONE_RULE]["state"] == "suspended"
    assert len(_done_cli_cards()) == 3 and store.answers(sid) == [] and store.answers(second_sid) == []
    wait["transcript"].append(
        {
            "readback": {
                "failed_item": store.get(sid),
                "next_item": store.get(next_sid),
                "actions": store.actions(sid),
                "overruled_item": store.get(second_sid),
                "rule_state": store.rule_states()[DONE_RULE],
            }
        }
    )
    _save_lane(wait, "cli-plan-after-cards-done-failure-overrule.json")


OUTCOME_RULE = "plan_outcome_followup"
OUTCOME_WHY = "The ready outcome assessment and the bounded repair plan belong to the configured plan lane."
OUTCOME_DECISIONS = [{"date": "2026-09-01", "by": "user", "decision": "Ship the approved change to all current users."}]
OUTCOME_CYCLES = ("in-demo-c01", "in-demo-c02")


def _outcome_options(cycle: str, protected: bool = False) -> dict:
    """The review's options for one ready outcome review; *protected* swaps in a verdict alternative."""
    second = (
        {
            "id": "B",
            "label": "Record a verdict and conclude",
            "action": "set_cycle_fields",
            "args": {"cycle": cycle, "fields": {"result": {"verdict": "no_effect"}}},
        }
        if protected
        else {"id": "B", "label": "Acknowledge the assessment", "action": "acknowledge", "args": {}}
    )
    return {
        "question": "What should follow the ready outcome assessment?",
        "why": OUTCOME_WHY,
        "summary": SUMMARY,
        "recommend": "A",
        "options": [
            {
                "id": "A",
                "label": "Plan the bounded repair",
                "action": "kanban_create",
                "args": {
                    "assignee": "planner",
                    "title": "Repair the outcome",
                    "body": "Start the optional pilot in October and widen the cohort to every account.",
                },
            },
            second,
        ],
    }


def _outcome_args(wait, cycle: str) -> dict:
    """The rule's caller arguments for *cycle*: the latest user row, hashed as the rule hashes it."""
    from hermes_initiatives_core.docs import parse_markdown

    path = wait["root"] / "in-demo" / "cycles" / f"{cycle}.md"
    frontmatter = parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter
    users = [row for row in frontmatter["decisions"] if row.get("by") == "user"]
    canonical = json.dumps(users[-1], sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return {
        "repairs_decision": len(users) - 1,
        "authority_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _outcome_tick_write(wait) -> dict[str, str]:
    """Run the real ``tick`` and ``write`` and return one short id per synthetic cycle."""
    tick = _done_cli(wait, "tick", "--gates", str(wait["gates"]))
    assert tick.returncode == 0, tick.stdout + tick.stderr
    ids = {}
    for cycle in OUTCOME_CYCLES:
        item = wait["store"].get_by_key(f"cycle:demo:{cycle}:gate")
        assert item is not None and item["status"] == "open", tick.stdout
        ids[cycle] = item["short_id"]
    _outcome_write_options(wait, {cycle: False for cycle in OUTCOME_CYCLES}, ids, tick)
    for cycle, sid in ids.items():
        write(
            wait["root"] / "settle" / f"{cycle}.json",
            json.dumps(
                {"settle": [{"short_id": sid, "rule": OUTCOME_RULE, "args": _outcome_args(wait, cycle), "why": OUTCOME_WHY}]}
            ),
        )
    wait["sid"] = ids[OUTCOME_CYCLES[0]]
    return ids


def _outcome_write_options(wait, protected: dict[str, bool], ids: dict[str, str], tick) -> None:
    """Write one option set per cycle through the real ``write`` CLI; *protected* names the verdict-alternative cycles."""
    payload = {"items": {ids[cycle]: _outcome_options(cycle, protected=protected[cycle]) for cycle in ids}}
    for other in json.loads(tick.stdout)["items"]:
        if other["short_id"] not in ids.values():
            payload["items"][other["short_id"]] = {"watch": "agent_owned: synthetic fixture owner handles this control"}
    written = _done_cli(wait, "write", "--file", "-", stdin=json.dumps(payload))
    assert written.returncode == 0, written.stdout + written.stderr


@pytest.fixture
def outcome_signature_cli(env, root, tmp_path):
    """Two ready outcome reviews driven through the real CLI: ``tick``, ``write``, then ``settle`` per cycle."""
    import datetime as dt

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    from hermes_initiatives_core import outcome_loop, writers
    from hermes_initiatives_core.docs import dump_markdown, parse_markdown
    from hermes_initiatives_core.kanban import read_cards
    from hermes_initiatives_core.reviews import ReviewStore

    for name in ("HERMES_HOME", "HERMES_KANBAN_HOME", "HERMES_INITIATIVES_DATA_DIR"):
        assert Path(env[name]).resolve().is_relative_to(tmp_path.resolve()), name
    kbc.init_db(board="default")
    cards = []
    with kbc.connect(board="default") as conn:
        for cycle in OUTCOME_CYCLES:
            card = kb.create_task(conn, title=f"Delivered {cycle}", assignee="planner")
            kb.complete_task(conn, card, result="Delivered the approved change")
            cards.append(card)
    settings = Path(env["HERMES_INITIATIVES_DATA_DIR"]) / "settings.json"
    settings.write_text(
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
    brief = root / "in-demo" / "initiative.md"
    doc = parse_markdown(brief.read_text(encoding="utf-8"), brief)
    doc.frontmatter["outcome_policy"] = {"enabled": True, "owner": "planner", "session": "session-2026-10"}
    write(brief, dump_markdown(doc.frontmatter, doc.body))
    evidence = write(tmp_path / "reports" / "evidence.md", "Clicks 118 against target 120; six flakes.")
    proposal = write(tmp_path / "reports" / "proposal.md", "Alternatives: pilot, hold, or extend. None chosen.")
    for cycle, card in zip(OUTCOME_CYCLES, cards, strict=True):
        target = write(
            root / "in-demo" / "cycles" / f"{cycle}.md",
            cycle_md(
                cycle,
                stage="decided",
                decisions=OUTCOME_DECISIONS,
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
        frontmatter = parse_markdown(target.read_text(encoding="utf-8"), target).frontmatter
        ids = [str(value) for value in frontmatter["kanban_cards"]]
        epoch = outcome_loop.input_epoch(frontmatter, [read_cards("default", ids)["cards"][value] for value in ids])
        writers.record_observation(
            root,
            cycle,
            {
                "kind": "assessment",
                "epoch": epoch,
                "status": "missed",
                "detail": "Clicks 118 against the 120 target.",
                "evidence": str(evidence),
                "proposal": str(proposal),
            },
        )
    gates = write(
        tmp_path / "gates.json",
        json.dumps({"board": "default", "cards": [], "generated_at": dt.datetime.now(dt.UTC).isoformat()}),
    )
    wait = {
        "env": env,
        "root": root,
        "gates": gates,
        "transcript": [],
        "store": ReviewStore(),
        "cards": set(cards),
    }
    wait["ids"] = _outcome_tick_write(wait)
    return wait


def _outcome_settle(wait, cycle):
    return _done_cli(wait, "settle", "--file", str(wait["root"] / "settle" / f"{cycle}.json"))


def _outcome_cards():
    return set(_done_cli_cards())


def _outcome_snapshot(wait):
    from hermes_cli import kanban_db as kb

    return (
        review_snapshot(wait["store"].path),
        review_snapshot(Path(kb.kanban_db_path("default"))),
        {str(p): p.read_bytes() for p in wait["root"].rglob("*") if p.is_file()},
    )


def test_plan_outcome_followup_cli_signature_lane(outcome_signature_cli):
    """Lane 1: one routine review and one with a nonrecommended verdict option, through the real CLI.

    The identical fixture runs on the candidate and, via ``REVIEW_CLI_SOURCE_ROOT``, on the pinned baseline.
    Both support the routine settlement. The baseline settles the protected-alternative item and creates a card;
    the candidate refuses it with every Review row, Kanban row and cycle byte unchanged.
    """
    wait = outcome_signature_cli
    store = wait["store"]
    baseline = os.environ.get("REVIEW_CLI_EXPECT_BASELINE_ESCAPE") == "1"
    routine_sid, protected_sid = wait["ids"][OUTCOME_CYCLES[0]], wait["ids"][OUTCOME_CYCLES[1]]
    tick = _done_cli(wait, "tick", "--gates", str(wait["gates"]))
    assert tick.returncode == 0, tick.stdout + tick.stderr
    _outcome_write_options(wait, {OUTCOME_CYCLES[0]: False, OUTCOME_CYCLES[1]: True}, wait["ids"], tick)

    routine = _outcome_settle(wait, OUTCOME_CYCLES[0])
    result = json.loads(routine.stdout)["results"][0]
    assert routine.returncode == 0 and result["ok"] is True, routine.stdout + routine.stderr
    assert result["rule"] == OUTCOME_RULE and _outcome_link_ok(wait, OUTCOME_CYCLES[0], result["receipt"])
    assert store.get(routine_sid)["status"] == "settled" and store.answers(routine_sid) == []

    before = _outcome_snapshot(wait)
    cards_before = _outcome_cards()
    protected = _outcome_settle(wait, OUTCOME_CYCLES[1])
    result = json.loads(protected.stdout)["results"][0]
    if baseline:
        assert protected.returncode == 0 and result["ok"] is True, protected.stdout + protected.stderr
        assert store.get(protected_sid)["status"] == "settled"
        created = _outcome_cards() - cards_before
        assert len(created) == 1, "the baseline settles a nonrecommended verdict alternative and creates a card"
        assert _outcome_link_ok(wait, OUTCOME_CYCLES[1], result["receipt"])
    else:
        assert protected.returncode == 2 and result["status"] == "refused", protected.stdout + protected.stderr
        assert "review bar" in result["error"], result["error"]
        assert store.get(protected_sid)["status"] == "open"
        assert _outcome_cards() == cards_before, "a refusal creates no card"
        assert _outcome_snapshot(wait) == before, "a refusal writes no Review row, Kanban row or cycle byte"
    wait["transcript"].append(
        {
            "readback": {
                "source_root": os.environ.get("REVIEW_CLI_SOURCE_ROOT") or str(SCRIPTS.parents[2]),
                "routine": store.get(routine_sid),
                "protected": store.get(protected_sid),
                "cards": sorted(_outcome_cards()),
                "cycle": _outcome_frontmatter(wait, OUTCOME_CYCLES[1]),
            }
        }
    )
    _save_lane(wait, "cli-outcome-signature-trunk.json" if baseline else "cli-outcome-signature-head.json")


def _outcome_frontmatter(wait, cycle):
    from hermes_initiatives_core.docs import parse_markdown

    path = wait["root"] / "in-demo" / "cycles" / f"{cycle}.md"
    return parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter


def _outcome_link_ok(wait, cycle, receipt):
    """The cycle file links the receipt's card and records exactly one ``by: agent`` audit row for the rule."""
    import re as _re

    card = _re.match(r"(t_[0-9a-f]+) for planner, linked to", receipt)
    assert card, receipt
    frontmatter = _outcome_frontmatter(wait, cycle)
    audits = [row for row in frontmatter["decisions"] if row.get("rule") == OUTCOME_RULE]
    return card.group(1) in [str(value) for value in frontmatter["kanban_cards"]] and len(audits) == 1


def test_plan_outcome_followup_cli_failure_and_overrule(outcome_signature_cli, tmp_path):
    """Lane 2: a failed link write keeps its card identity, the retry is chained, and overrule suspends the rule."""
    from hermes_initiatives_core.docs import parse_markdown

    wait = outcome_signature_cli
    store = wait["store"]
    first, second = (wait["ids"][cycle] for cycle in OUTCOME_CYCLES)
    injector = write(
        tmp_path / "inject_outcome_audit.py",
        """
        import runpy
        import sys
        from hermes_initiatives_core import writers
        def fail(*args, **kwargs):
            raise OSError("injected audit/link failure")
        writers.edit_frontmatter = fail
        script, payload = sys.argv[1:]
        sys.argv = [script, "settle", "--file", payload]
        sys.path.insert(0, str(__import__("pathlib").Path(script).parent))
        runpy.run_path(script, run_name="__main__")
    """,
    )
    payload = wait["root"] / "settle" / f"{OUTCOME_CYCLES[0]}.json"
    failed = subprocess.run(
        [sys.executable, str(injector), str(SCRIPTS / "daily_review.py"), str(payload)],
        env=wait["env"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    wait["transcript"].append(
        {"argv": ["injected settle"], "returncode": failed.returncode, "stdout": failed.stdout, "stderr": failed.stderr}
    )
    result = json.loads(failed.stdout)["results"][0]
    created = _outcome_cards() - wait["cards"]
    assert len(created) == 1, "the failed settlement keeps the card it created"
    card = created.pop()
    assert failed.returncode == 2 and result["status"] == "failed" and card in result["error"]
    assert store.get(first)["status"] == "open"
    assert [(a["action"], a["state"]) for a in store.actions(first)] == [(f"settle:{OUTCOME_RULE}", "failed")]
    assert card in store.actions(first)[0]["error"]

    before = _outcome_snapshot(wait)
    retry = _outcome_settle(wait, OUTCOME_CYCLES[0])
    result = json.loads(retry.stdout)["results"][0]
    assert retry.returncode == 2 and result["status"] == "refused" and "chain guard" in result["error"]
    assert _outcome_snapshot(wait) == before and _outcome_cards() == wait["cards"] | {card}

    settled = _outcome_settle(wait, OUTCOME_CYCLES[1])
    result = json.loads(settled.stdout)["results"][0]
    assert settled.returncode == 0 and result["ok"] is True
    receipt = result["receipt"]
    assert _outcome_link_ok(wait, OUTCOME_CYCLES[1], receipt) and store.get(second)["status"] == "settled"
    repeat = _outcome_settle(wait, OUTCOME_CYCLES[1])
    result = json.loads(repeat.stdout)["results"][0]
    assert repeat.returncode == 2 and result["status"] == "refused" and "only open" in result["error"]
    assert len(_outcome_cards()) == len(wait["cards"]) + 2, "the repeat creates no second card"

    overruled = _done_cli(wait, "overrule", second, "--reason", "This repair plan should wait for my own decision.")
    assert overruled.returncode == 0, overruled.stdout + overruled.stderr
    assert store.rule_states()[OUTCOME_RULE]["state"] == "suspended"
    suspended = _outcome_settle(wait, OUTCOME_CYCLES[0])
    result = json.loads(suspended.stdout)["results"][0]
    assert suspended.returncode == 2 and result["status"] == "refused" and "suspended" in result["error"]
    assert store.answers(first) == [] and store.answers(second) == []
    path = wait["root"] / "in-demo" / "cycles" / f"{OUTCOME_CYCLES[1]}.md"
    wait["transcript"].append(
        {
            "readback": {
                "failed_item": store.get(first),
                "settled_item": store.get(second),
                "actions": store.actions(first) + store.actions(second),
                "rule_state": store.rule_states()[OUTCOME_RULE],
                "cycle": parse_markdown(path.read_text(encoding="utf-8"), path).frontmatter,
            }
        }
    )
    _save_lane(wait, "cli-outcome-signature-failure-overrule.json")

