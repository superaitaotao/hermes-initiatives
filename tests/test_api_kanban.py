"""Read-only Kanban join and the dashboard API, against a real Hermes Kanban schema."""

from __future__ import annotations

import importlib.util
import json
import sqlite3

import pytest
from conftest import DECIDED, ROOT, cycle_md, write
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_initiatives_core import kanban, launches, reviews

kb = pytest.importorskip("hermes_cli.kanban_db")
kbc = pytest.importorskip("hermes_cli.kanban_db_connect")

PR = "https://github.com/acme/app/pull/381"


@pytest.fixture
def board(isolated_hermes):
    kbc.init_db(board="default")
    conn = kbc.connect(board="default")
    done = kb.create_task(conn, title="Ship it", assignee="coder")
    kb.complete_task(
        conn,
        done,
        result="shipped",
        metadata={"published_pr": PR, "merged_at": "2026-10-01T00:00:00Z", "merge_commit": "abcdef1234567890"},
    )
    open_card = kb.create_task(conn, title="Still open", assignee="coder")
    conn.close()
    return {"done": done, "open": open_card, "path": kb.kanban_db_path("default")}


def test_cards_and_pr_come_from_run_metadata(board):
    result = kanban.read_cards("default", [board["done"], board["open"], "t_ffffffff"])
    cards = result["cards"]
    assert result["state"] == "fresh"
    assert cards[board["done"]]["status"] == "done"
    assert cards[board["done"]]["pr"] == {
        "state": "merged",
        "url": PR,
        "merged_at": "2026-10-01T00:00:00Z",
        "merge_commit": "abcdef123456",
    }
    assert cards[board["open"]]["pr"]["state"] == "none"
    assert cards["t_ffffffff"]["state"] == "not_found"


def test_kanban_connection_cannot_write(board):
    conn = kanban._connect(board["path"])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE tasks SET title = 'x'")
    conn.close()


def test_missing_board_is_unavailable_not_an_error(isolated_hermes):
    result = kanban.read_cards("no-such-board", ["t_00000000"])
    assert result["state"] == "unavailable" and result["cards"] == {}


def _client():
    spec = importlib.util.spec_from_file_location("initiatives_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/initiatives")
    return TestClient(app)


@pytest.fixture
def configured(root, board, tmp_path):
    write(
        root / "in-demo" / "cycles" / "in-demo-c01.md",
        cycle_md("in-demo-c01", stage="shipping", decisions=DECIDED, kanban_cards=[board["done"], board["open"]]),
    )
    write(
        root / "in-demo" / "cycles" / "in-demo-c00.md",
        cycle_md("in-demo-c00", stage="abandoned", decisions=DECIDED, disposition={"action": "stop", "reason": "x"}),
    )
    settings = {"projects": [{"id": "demo", "label": "Demo", "root": str(root), "kanban_board": "default"}]}
    (tmp_path / "plugin-data").mkdir(exist_ok=True)
    (tmp_path / "plugin-data" / "settings.json").write_text(json.dumps(settings))
    return board


def test_list_is_summary_only_and_detail_has_cards(configured):
    client = _client()
    listing = client.get("/api/plugins/initiatives/projects/demo/initiatives").json()
    initiative = listing["initiatives"][0]
    assert [c["id"] for c in initiative["open_cycles"]] == ["in-demo-c01"]
    assert "observations" not in initiative["open_cycles"][0]
    assert initiative["open_cycles"][0]["next_action"] == "Finish delivery: 1 of 2 cards open"
    detail = client.get("/api/plugins/initiatives/initiatives/in-demo").json()
    assert {c["id"] for c in detail["cycles"]} == {"in-demo-c00", "in-demo-c01"}
    pr = next(c for c in detail["cycles"][0]["cards"] if c["id"] == configured["done"])["pr"]
    assert pr["url"] == PR


def test_card_detail_shows_the_card_read_only(configured):
    # The page opens a card in place: Desktop's Kanban route has no deep link to one card.
    conn = kbc.connect(board="default")
    kb.add_comment(conn, configured["open"], "reviewer", "Needs a test")
    kb.link_tasks(conn, configured["done"], configured["open"])
    conn.close()
    client = _client()
    card = client.get(f"/api/plugins/initiatives/projects/demo/cards/{configured['open']}").json()
    assert card["title"] == "Still open" and card["board"] == "default"
    assert card["comments"][0]["body"] == "Needs a test"
    assert card["parents"] == [configured["done"]]
    done = client.get(f"/api/plugins/initiatives/projects/demo/cards/{configured['done']}").json()
    assert done["pr"]["state"] == "merged" and done["runs"][0]["outcome"] == "completed"
    assert client.get("/api/plugins/initiatives/projects/demo/cards/t_ffffffff").status_code == 404
    assert client.get("/api/plugins/initiatives/projects/demo/cards/x' OR 1").status_code == 400


def test_cycle_and_card_launches_carry_their_subject(configured):
    # A session started on one cycle or card must get that subject's context, and a retry must rebuild it.
    client = _client()
    base = {"project": "demo", "initiative": "in-demo", "connection_id": "local", "profile": "default", "target_profile": "x"}
    card = client.post("/api/plugins/initiatives/launches", json={**base, "kind": "card", "subject": configured["open"]}).json()
    assert card["subject"] == configured["open"] and card["title"].startswith(f"Card: {configured['open']} Still open · ")
    cycle = client.post("/api/plugins/initiatives/launches", json={**base, "kind": "cycle", "subject": "in-demo-c01"}).json()
    assert cycle["kind"] == "cycle" and cycle["subject"] == "in-demo-c01"
    ctx = client.get(
        "/api/plugins/initiatives/initiatives/in-demo/subject-context", params={"kind": "card", "subject": configured["open"]}
    ).json()
    data = json.loads(ctx["context"])
    assert data["card"]["id"] == configured["open"] and [c["id"] for c in data["cycles"]] == ["in-demo-c01"]
    ctx = client.get(
        "/api/plugins/initiatives/initiatives/in-demo/subject-context", params={"kind": "cycle", "subject": "in-demo-c01"}
    )
    assert json.loads(ctx.json()["context"])["cycle"]["cards"][0]["id"] == configured["done"]


def test_attend_launch_only_for_a_card_that_waits_on_the_user(configured):
    # The Review tab's "Waiting on you" list starts these; the card need not be linked to a cycle.
    conn = kbc.connect(board="default")
    waits = kb.create_task(conn, title="Needs a login", assignee="coder")
    kb.block_task(conn, waits, reason="login", kind="needs_input")
    kb.add_comment(conn, waits, "orchestrator", "**BLOCKED — NEEDS YOU** — x\n\n**You:** log in to the TEST console.\n")
    kb.add_comment(conn, configured["open"], "orchestrator", "**FYI** — x\n\n**You:** nothing needed.\n")
    conn.close()
    client = _client()
    review = client.get("/api/plugins/initiatives/review").json()
    assert [(w["card"], w["you"]) for w in review["waiting_on_you"]] == [(waits, "log in to the TEST console.")]
    base = {"project": "demo", "connection_id": "local", "profile": "default", "target_profile": "x"}
    launch = client.post("/api/plugins/initiatives/launches", json={**base, "kind": "attend", "subject": waits}).json()
    assert launch["kind"] == "attend" and launch["title"].startswith(f"Attend: {waits} Needs a login · ")
    # Once the card has a Review item, its id leads the title, as for Discuss sessions.
    gate, _ = reviews.ReviewStore().upsert_item(
        f"kanban:default:{waits}:gate", section="gated", source="kanban", title="Needs a login", basis={}, project="demo"
    )
    linked = client.post("/api/plugins/initiatives/launches", json={**base, "kind": "attend", "subject": waits}).json()
    assert linked["title"].startswith(f"{gate['short_id']} Attend: {waits} Needs a login · ")
    ctx = client.get("/api/plugins/initiatives/projects/demo/attend-context", params={"card": waits}).json()
    data = json.loads(ctx["context"])
    assert data["waiting_on_you"]["you"] == "log in to the TEST console." and data["card"]["id"] == waits
    nothing = client.post("/api/plugins/initiatives/launches", json={**base, "kind": "attend", "subject": configured["open"]})
    assert nothing.status_code == 404
    assert client.post("/api/plugins/initiatives/launches", json={**base, "kind": "attend"}).status_code == 400


def test_blocker_launch_explains_one_review_item(configured):
    # Every Review item can start a session that explains it, with the live card for a Kanban gate.
    from hermes_initiatives_core import review_collect, reviews, settings

    conn = kbc.connect(board="default")
    kb.block_task(conn, configured["open"], reason="CI times out on Windows", kind="needs_input")
    kb.add_comment(conn, configured["open"], "coder", "**BLOCKED — NEEDS YOU** — x\n\n**You:** choose a runner plan.\n")
    conn.close()
    store = reviews.ReviewStore()
    review_collect.sync(
        store, settings.load_settings()[0].projects, gates={"default": {configured["open"]: {"id": configured["open"]}}}
    )
    gate = store.get_by_key(f"kanban:default:{configured['open']}:gate")
    assert gate is not None
    sid = gate["short_id"]
    store.add_version(
        sid,
        question="Which runner plan?",
        options=[
            {"id": "A", "label": "Amend the plan", "action": "kanban_unblock", "args": {}},
            {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="",
    )
    client = _client()
    ctx = client.get(f"/api/plugins/initiatives/review/items/{sid.lower()}/session-context").json()
    data = json.loads(ctx["context"])
    assert data["question"] == "Which runner plan?" and data["recommend"] == "A"
    assert data["card"]["id"] == configured["open"] and "choose a runner plan" in data["card"]["comments"][0]["body"]
    assert "CI times out on Windows" in data["collected_context"]
    base = {"project": "demo", "connection_id": "local", "profile": "default", "target_profile": "x"}
    launch = client.post("/api/plugins/initiatives/launches", json={**base, "kind": "blocker", "subject": sid.lower()}).json()
    assert launch["kind"] == "blocker" and launch["subject"] == sid
    # The Review item's id leads the title, so the session list sorts and reads by D-number.
    assert launch["title"].startswith(f"{sid} Blocker: Which runner plan? · ")
    # The item lists only its own Discuss sessions, not other launches in the project.
    client.post("/api/plugins/initiatives/launches", json={**base, "kind": "triage"})
    listed = client.get(f"/api/plugins/initiatives/review/items/{sid.lower()}/sessions").json()["launches"]
    assert [s["id"] for s in listed] == [launch["id"]]
    assert client.get("/api/plugins/initiatives/review/items/D9999/sessions").status_code == 404
    assert client.get("/api/plugins/initiatives/review/items/D9999/session-context").status_code == 404
    assert (
        client.post("/api/plugins/initiatives/launches", json={**base, "kind": "blocker", "subject": "D9999"}).status_code == 404
    )
    assert client.post("/api/plugins/initiatives/launches", json={**base, "kind": "blocker"}).status_code == 400


def test_subject_launch_refuses_cards_outside_the_initiative(configured):
    conn = kbc.connect(board="default")
    stranger = kb.create_task(conn, title="Unrelated", assignee="coder")
    conn.close()
    client = _client()
    base = {"project": "demo", "initiative": "in-demo", "connection_id": "local", "profile": "default", "target_profile": "x"}
    assert client.post("/api/plugins/initiatives/launches", json={**base, "kind": "card", "subject": stranger}).status_code == 404
    assert (
        client.post("/api/plugins/initiatives/launches", json={**base, "kind": "cycle", "subject": "in-demo-c99"}).status_code
        == 404
    )
    assert client.post("/api/plugins/initiatives/launches", json={**base, "kind": "card"}).status_code == 400


def test_session_context_is_bounded_and_names_cwd(configured, root):
    context = _client().get("/api/plugins/initiatives/initiatives/in-demo/session-context").json()
    assert context["cwd"] == str(root)
    assert len(context["context"]) <= 12_000
    assert json.loads(context["context"])["cycles"][0]["id"] == "in-demo-c01"


def test_oversized_context_is_trimmed_to_valid_json(configured, root):
    # Regression: cutting serialized context mid-string hands the agent broken JSON.
    for n in range(2, 40):
        write(
            root / "in-demo" / "cycles" / f"in-demo-c{n:02d}.md",
            cycle_md(f"in-demo-c{n:02d}", stage="decided", decisions=DECIDED * 8, hypothesis="h" * 400),
        )
    context = _client().get("/api/plugins/initiatives/initiatives/in-demo/session-context").json()["context"]
    assert len(context) <= 12_000
    assert isinstance(json.loads(context), dict)


def test_launch_state_machine_over_http(configured):
    client = _client()
    base = "/api/plugins/initiatives/launches"
    launch = client.post(
        base,
        json={
            "project": "demo",
            "initiative": "in-demo",
            "kind": "discuss",
            "connection_id": "local",
            "profile": "default",
            "target_profile": "any",
        },
    ).json()
    assert launch["state"] == "pending" and launch["title"].endswith(launch["id"])
    assert client.patch(f"{base}/{launch['id']}", json={"state": "submitted"}).status_code == 409
    linked = client.patch(f"{base}/{launch['id']}", json={"state": "linked", "stored_session_id": "s1"}).json()
    assert linked["state"] == "linked"
    bad = client.patch(f"{base}/{launch['id']}", json={"state": "prompt_failed", "stored_session_id": "s2"})
    assert bad.status_code == 409
    assert client.patch(f"{base}/{launch['id']}", json={"state": "submitted"}).json()["state"] == "submitted"
    sessions = client.get("/api/plugins/initiatives/initiatives/in-demo/sessions").json()["launches"]
    assert [s["stored_session_id"] for s in sessions] == ["s1"]
    assert client.delete(f"{base}/{launch['id']}").status_code == 200


def test_unknown_project_and_initiative_are_404(configured):
    client = _client()
    assert client.get("/api/plugins/initiatives/projects/nope/initiatives").status_code == 404
    assert client.get("/api/plugins/initiatives/initiatives/in-nope").status_code == 404


def test_existing_launch_store_gains_subject_column(tmp_path):
    # Stores written before cycle and card launches have no subject column; opening one must migrate it.
    db = tmp_path / "launches.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE launches (id TEXT PRIMARY KEY, project TEXT NOT NULL, initiative TEXT, kind TEXT NOT NULL, "
        "connection_id TEXT NOT NULL, profile TEXT NOT NULL, target_profile TEXT NOT NULL, title TEXT NOT NULL, "
        "state TEXT NOT NULL, stored_session_id TEXT, runtime_session_id TEXT, error TEXT, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO launches VALUES ('Lold', 'demo', 'in-demo', 'discuss', 'local', 'p', 'p', 't', 'submitted', "
        "'s', 'r', NULL, '2026-10-10', '2026-10-10')"
    )
    conn.commit()
    conn.close()
    store = launches.LaunchStore(db)
    assert store.get("Lold")["subject"] is None
    made = store.create(
        project="demo",
        initiative="in-demo",
        kind="card",
        subject="t_00000001",
        connection_id="local",
        profile="p",
        target_profile="p",
        title_prefix="Card: x",
    )
    assert store.get(made["id"])["subject"] == "t_00000001"


def test_session_defaults_round_trip_and_reject_bad_values(configured):
    # One host-wide default applies to every launch; a typo must not silently reach session.create.
    client = _client()
    url = "/api/plugins/initiatives/session-defaults"
    assert client.get(url).json() == {"profile": "", "model": "", "provider": "", "reasoning_effort": ""}
    saved = client.put(
        url, json={"profile": "planner", "model": " model-x ", "provider": "example-provider", "reasoning_effort": "high"}
    ).json()
    assert saved == {"profile": "planner", "model": "model-x", "provider": "example-provider", "reasoning_effort": "high"}
    assert client.get(url).json() == saved
    assert client.put(url, json={"reasoning_effort": "hgih"}).status_code == 422
    assert client.put(url, json={"provider": "example-provider"}).status_code == 422
    assert client.put(url, json={"profile": "../etc"}).status_code == 422
    assert client.get(url).json() == saved
    assert client.put(url, json={}).json() == {"profile": "", "model": "", "provider": "", "reasoning_effort": ""}


def test_review_item_links_the_documents_its_ask_cites_and_previews_them(configured, root, tmp_path):
    # A plan gate cites its plan by absolute path; the Review item carries it, and the page can preview it.
    from hermes_initiatives_core import review_collect, settings

    plan = write(root / "tasks" / "2026-10-17-plan.md", "# Plan v1\n\nBody.\n")
    outside = write(tmp_path / "elsewhere" / "secret.md", "# Not in the vault\n")
    conn = kbc.connect(board="default")
    kb.block_task(conn, configured["open"], reason="approve the plan", kind="needs_input")
    kb.add_comment(
        conn,
        configured["open"],
        "planner",
        f"**Needs you** — approve plan v1.\n\n**You:** reply `approve`.\n\n---\n- Plan: `{plan}`, 17 bytes.\n"
        f"- Also `{outside}` and `{root}/tasks/missing.md`.\n",
    )
    conn.close()
    store = reviews.ReviewStore()
    review_collect.sync(
        store, settings.load_settings()[0].projects, gates={"default": {configured["open"]: {"id": configured["open"]}}}
    )
    gate = store.get_by_key(f"kanban:default:{configured['open']}:gate")
    assert [link for link in gate["links"] if link["kind"] == "doc"] == [{"kind": "doc", "path": str(plan.resolve())}], (
        "only existing Markdown files inside the project roots are linked"
    )
    client = _client()
    doc = client.get("/api/plugins/initiatives/projects/demo/doc", params={"path": str(plan)}).json()
    assert doc["text"] == "# Plan v1\n\nBody.\n" and doc["bytes"] == 17 and len(doc["sha256"]) == 64
    assert doc["rel"] == "tasks/2026-10-17-plan.md"
    for bad in (str(outside), f"{root}/../elsewhere/secret.md", str(root / "in-demo" / "cycles"), "/etc/passwd"):
        assert client.get("/api/plugins/initiatives/projects/demo/doc", params={"path": bad}).status_code == 404, bad
    store.add_version(
        gate["short_id"],
        question="Approve plan v1?",
        options=[
            {"id": "A", "label": "Approve", "action": "kanban_unblock", "args": {}},
            {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="",
    )
    ctx = client.get(f"/api/plugins/initiatives/review/items/{gate['short_id']}/session-context").json()
    assert [d["path"] for d in json.loads(ctx["context"])["documents"]] == [str(plan.resolve())]


def test_a_long_plan_gate_keeps_its_documents_within_the_context_limit(configured, root):
    # A real plan gate (5 KB body, three long comments) once overflowed and fell back to a truncated summary,
    # which dropped the cited plan. The trims must keep the documents and valid structure.
    from hermes_initiatives_core import settings, views

    plan = write(root / "tasks" / "plan.md", "# Plan\n")
    conn = kbc.connect(board="default")
    kb.block_task(conn, configured["open"], reason="approve " + "x" * 3000, kind="needs_input")
    for n in range(3):
        kb.add_comment(conn, configured["open"], "planner", f"Comment {n}: plan `{plan}`. " + "y" * 2900)
    conn.close()
    store = reviews.ReviewStore()
    item, _ = store.upsert_item(
        f"kanban:default:{configured['open']}:gate",
        section="gated",
        source="kanban",
        title="Plan gate",
        basis={"board": "default", "card": configured["open"]},
        project="demo",
    )
    store.set_context(item["short_id"], "z" * 1500)
    long = [{"id": c, "label": "L" * 300, "action": "defer", "args": {"reason": "r" * 900}} for c in "ABC"]
    store.add_version(item["short_id"], question="Approve?", options=long, recommend="A", context="c" * 2000)
    project = settings.load_settings()[0].projects[0]
    ctx = views.blocker_context(project, store.get(item["short_id"]), [], [])
    data = json.loads(ctx["context"])
    assert "truncated" not in data and len(ctx["context"]) <= views.CONTEXT_LIMIT
    assert [d["path"] for d in data["documents"]] == [str(plan.resolve())]
