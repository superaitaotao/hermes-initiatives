"""Review API: read-only routes over reviews.db."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT, write
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_initiatives_core import review_attention, reviews

OPTS = [
    {"id": "A", "label": "Approve", "action": "kanban_unblock", "args": {}},
    {"id": "B", "label": "Wait", "action": "defer", "args": {}},
]


@pytest.fixture
def client(isolated_hermes):
    spec = importlib.util.spec_from_file_location("initiatives_plugin_api_review", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    return TestClient(app)


def test_overview_lists_written_items_and_hides_basis(client):
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("kanban:b:t_1:gate", section="gated", source="kanban", title="Gate", basis={"secret": 1})
    store.add_version(item["short_id"], question="Approve?", options=OPTS, recommend="A", context="ctx")
    pending, _ = store.upsert_item("kanban:b:t_2:gate", section="gated", source="kanban", title="New", basis={})
    data = client.get("/api/review").json()
    assert [i["short_id"] for i in data["queue"]] == [item["short_id"]]
    assert [i["short_id"] for i in data["pending"]] == [pending["short_id"]]
    assert "basis" not in data["queue"][0] and data["queue"][0]["version"]["question"] == "Approve?"


def test_outdated_options_wait_for_new_ones_instead_of_being_answerable(client):
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("criteria:p:in-x", section="gated", source="criteria", title="Gate", basis={"v": 1})
    store.add_version(item["short_id"], question="Old?", options=OPTS, recommend="A", context="")
    store.refresh_basis(item["short_id"], {"v": 2})
    data = client.get("/api/review").json()
    assert data["queue"] == []
    assert [(i["short_id"], i["outdated"]) for i in data["pending"]] == [(item["short_id"], True)]
    refused = client.post(f"/api/review/items/{item['short_id']}/answer", json={"option": "A"}).json()
    assert refused["result"]["state"] == "refused" and "out of date" in refused["result"]["message"]


def test_item_route_returns_versions_and_404(client):
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("k", section="gated", source="kanban", title="Gate", basis={})
    store.add_version(item["short_id"], question="Q1", options=OPTS, recommend="A", context="")
    body = client.get(f"/api/review/items/{item['short_id']}").json()
    assert [v["question"] for v in body["versions"]] == ["Q1"] and body["answers"] == []
    assert client.get("/api/review/items/D999").status_code == 404


def test_groups_carry_the_initiative_title(client, root):
    data_dir = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"projects": [{"id": "demo", "label": "Demo", "root": str(root)}]}))
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("k:demo", section="gated", source="kanban", title="Gate", basis={}, initiative="in-demo")
    store.add_version(item["short_id"], question="Q", options=OPTS, recommend="A", context="")
    store.create_review(trigger="cron", model="m", window_start=None)
    groups = client.get("/api/review").json()["groups"]
    assert [(g["initiative"], g["title"], g["decisions"]) for g in groups] == [("in-demo", "Demo initiative", [item["short_id"]])]


def _settings(*projects: tuple[str, Path]) -> None:
    data_dir = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"id": pid, "label": pid.title(), "root": str(root)} for pid, root in projects]
    (data_dir / "settings.json").write_text(json.dumps({"projects": rows}))


def _two_projects(tmp_path: Path) -> tuple[Path, Path]:
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    write(alpha / "in-alpha" / "initiative.md", "---\nid: in-alpha\ntitle: Alpha initiative\nstatus: active\n---\nA.\n")
    write(beta / "in-beta" / "initiative.md", "---\nid: in-beta\ntitle: Beta initiative\nstatus: active\n---\nB.\n")
    _settings(("alpha", alpha), ("beta", beta))
    return alpha, beta


def _open(
    store: reviews.ReviewStore, key: str, *, project: str | None = None, initiative: str | None = None, section: str = "gated"
) -> dict[str, Any]:
    item, _ = store.upsert_item(
        key, section=section, source="kanban", title=key, basis={}, project=project, initiative=initiative
    )
    store.add_version(item["short_id"], question="Q", options=OPTS, recommend="A", context="")
    return item


def test_initiative_groups_follow_the_current_item_initiative(client, root):
    """Groups are read from the live items, not the initiative each item had when the review was written."""
    _settings(("demo", root))
    store = reviews.ReviewStore()
    review = store.create_review(trigger="cron", model="m", window_start=None)
    store.update_review(
        review["id"],
        summaries_json='{"summaries": {"in-demo": "Demo summary."}, "updates": {}}',
    )
    moved, _ = store.upsert_item("k:demo", section="gated", source="kanban", title="Gate", basis={}, initiative="in-missing")
    store.add_version(moved["short_id"], question="Q", options=OPTS, recommend="A", context="")
    groups = {g["initiative"]: g for g in client.get("/api/review").json()["initiative_groups"]}
    assert groups["in-missing"]["decisions"] == [moved["short_id"]], "an unknown initiative keeps its item"
    assert groups["in-missing"]["known"] is False
    assert groups[""]["decisions"] == [], "the unassigned group exists even with nothing in it"
    assert groups[""]["known"] is True
    assert groups["in-demo"]["summary"] == "Demo summary.", "review prose stays with the initiative it names"
    assert groups["in-demo"]["decisions"] == []


def test_untitled_initiative_is_known_and_keeps_review_prose(client, tmp_path):
    root = tmp_path / "untitled"
    write(root / "in-untitled" / "initiative.md", "---\nid: in-untitled\nstatus: active\n---\nBody.\n")
    _settings(("alpha", root))
    store = reviews.ReviewStore()
    item = _open(store, "untitled", project="alpha", initiative="in-untitled")
    review = store.create_review(trigger="cron", model="m", window_start=None)
    store.update_review(review["id"], summaries_json='{"summaries":{"in-untitled":"Still needs review"},"updates":{}}')
    data = client.get("/api/review?project=alpha").json()
    group = {g["initiative"]: g for g in data["initiative_groups"]}["in-untitled"]
    assert group["known"] is True
    assert group["title"] == "in-untitled"
    assert group["summary"] == "Still needs review"
    assert group["decisions"] == [item["short_id"]]


def test_scoped_legacy_group_keeps_visible_unknown_initiative(client, root):
    _settings(("demo", root))
    store = reviews.ReviewStore()
    item = _open(store, "unknown", project="demo", initiative="in-missing")
    store.create_review(trigger="cron", model="m", window_start=None)
    groups = client.get("/api/review?project=demo").json()["groups"]
    assert {g["initiative"]: g["decisions"] for g in groups}["in-missing"] == [item["short_id"]]


def test_initiative_groups_keep_every_lifecycle_collection_with_its_item(client, root):
    _settings(("demo", root))
    store = reviews.ReviewStore()
    waiting = store.upsert_item("k:wait", section="gated", source="kanban", title="W", basis={}, initiative="in-demo")[0]
    store.watch(waiting["short_id"], "Machine block")
    auto = store.upsert_item("k:auto", section="gated", source="request:job", title="A", basis={}, initiative="in-demo")[0]
    store.add_version(auto["short_id"], question="Q", options=OPTS, recommend="A", context="")
    store.mark_auto(auto["short_id"], "", surface="tab")
    stale = store.upsert_item("k:stale", section="gated", source="criteria", title="S", basis={"v": 1}, initiative="in-demo")[0]
    store.add_version(stale["short_id"], question="Q", options=OPTS, recommend="A", context="")
    store.refresh_basis(stale["short_id"], {"v": 2})
    group = {g["initiative"]: g for g in client.get("/api/review").json()["initiative_groups"]}["in-demo"]
    expected = {
        "watching": [waiting["short_id"]],
        "auto": [auto["short_id"]],
        "pending": [stale["short_id"]],
    }
    assert {key: group[key] for key in expected} == expected


def test_scoped_groups_keep_other_projects_out(client, tmp_path):
    """The review-time groups follow the same project scope as the live lists."""
    _two_projects(tmp_path)
    store = reviews.ReviewStore()
    review = store.create_review(trigger="cron", model="m", window_start=None)
    store.update_review(
        review["id"],
        summaries_json=json.dumps(
            {"summaries": {"in-alpha": "Alpha prose.", "in-beta": "Beta prose."}, "updates": {"in-beta": "Beta moved."}}
        ),
    )
    ours = _open(store, "k:alpha", project="alpha", initiative="in-alpha")
    theirs = _open(store, "k:beta", project="beta", initiative="in-beta")
    data = client.get("/api/review?project=alpha").json()
    assert [i["short_id"] for i in data["queue"]] == [ours["short_id"]]
    assert [(g["initiative"], g["title"], g["decisions"], g["summary"]) for g in data["groups"]] == [
        ("in-alpha", "Alpha initiative", [ours["short_id"]], "Alpha prose.")
    ]
    assert theirs["short_id"] not in json.dumps(data), "another project's decision id must not appear"
    both = client.get("/api/review").json()["groups"]
    assert {g["initiative"] for g in both} == {"in-alpha", "in-beta"}, "the unscoped response keeps every group"


def test_scoped_recent_and_settled_are_not_starved_by_other_projects(client, tmp_path):
    """A 20-row head of another project's closes must not empty this project's log."""
    _two_projects(tmp_path)
    store = reviews.ReviewStore()
    for n in range(20):
        closed = _open(store, f"k:beta:closed:{n}", project="beta", initiative="in-beta")
        store.answer(closed["short_id"], "A", surface="tab")
        store.close_item(closed["short_id"], "applied")
    their_settled = _open(store, "k:beta:settled", project="beta", initiative="in-beta")
    assert store.settle(their_settled["short_id"], "rule_beta", "done")
    mine = _open(store, "k:alpha:closed", project="alpha", initiative="in-alpha")
    store.answer(mine["short_id"], "A", surface="tab")
    store.close_item(mine["short_id"], "applied")
    my_settled = _open(store, "k:alpha:settled", project="alpha", initiative="in-alpha")
    assert store.settle(my_settled["short_id"], "rule_alpha", "done")
    with store._connect() as conn:  # the alpha rows are strictly older than every beta row
        conn.execute("UPDATE items SET updated_at = '2026-10-20T00:00:00+00:00' WHERE project = 'alpha'")
        conn.execute("UPDATE items SET updated_at = '2026-10-24T00:00:00+00:00' WHERE project = 'beta'")
    data = client.get("/api/review?project=alpha").json()
    assert [i["short_id"] for i in data["recent"]] == [mine["short_id"]]
    assert [i["short_id"] for i in data["settled"]] == [my_settled["short_id"]]


def test_legacy_item_naming_another_projects_initiative_is_shown_only_there(client, tmp_path):
    """An item written before the project column belongs to the root that defines the initiative it names."""
    _two_projects(tmp_path)
    store = reviews.ReviewStore()
    legacy_beta = _open(store, "k:legacy:beta", initiative="in-beta")
    unowned = _open(store, "k:legacy:none")
    ours = _open(store, "k:alpha", project="alpha", initiative="in-alpha")
    mine = client.get("/api/review?project=alpha").json()
    assert {i["short_id"] for i in mine["queue"]} == {ours["short_id"], unowned["short_id"]}
    groups = {g["initiative"]: g for g in mine["initiative_groups"]}
    assert groups["in-alpha"]["decisions"] == [ours["short_id"]]
    assert groups[""]["decisions"] == [unowned["short_id"]]
    assert "in-beta" not in groups
    theirs = client.get("/api/review?project=beta").json()
    assert {i["short_id"] for i in theirs["queue"]} == {legacy_beta["short_id"], unowned["short_id"]}
    groups = {g["initiative"]: g for g in theirs["initiative_groups"]}
    assert groups["in-beta"]["decisions"] == [legacy_beta["short_id"]]
    assert groups["in-beta"]["known"] is True
    assert groups[""]["decisions"] == [unowned["short_id"]], "a legacy item with no initiative stays visible everywhere"


def test_scoped_review_prose_results_and_facts_hide_other_projects(client, tmp_path):
    """The review object keeps its global diagnostics but not another project's prose or result ids."""
    _two_projects(tmp_path)
    store = reviews.ReviewStore()
    ours = _open(store, "k:alpha", project="alpha", initiative="in-alpha")
    theirs = _open(store, "k:beta", project="beta", initiative="in-beta")
    review = store.create_review(trigger="cron", model="m", window_start="2026-10-24T00:00:00+00:00")
    store.link_items(review["id"], [ours["short_id"], theirs["short_id"]])
    store.update_review(
        review["id"],
        summaries_json=json.dumps(
            {"summaries": {"in-alpha": "Alpha prose.", "in-beta": "Beta prose."}, "updates": {"in-beta": "Beta moved."}}
        ),
        facts_json=json.dumps(
            {
                "unchanged": ["in-alpha", "in-beta"],
                "urgent_pushes": 3,
                "results": [
                    {"short_id": ours["short_id"], "status": "applied", "note": "alpha done"},
                    {"short_id": theirs["short_id"], "status": "applied", "note": "beta done"},
                ],
            }
        ),
        gaps_json=json.dumps([{"source": "Daily job", "ref": "t_00000001", "line": "NEEDS YOU: approve"}]),
    )
    scoped = client.get("/api/review?project=alpha").json()["review"]
    assert scoped["summaries"] == {"summaries": {"in-alpha": "Alpha prose."}, "updates": {}}
    assert scoped["facts"]["unchanged"] == ["in-alpha"]
    assert [r["short_id"] for r in scoped["facts"]["results"]] == [ours["short_id"]]
    assert scoped["facts"]["urgent_pushes"] == 3, "global diagnostic counters stay"
    assert [g["ref"] for g in scoped["gaps"]] == ["t_00000001"], "coverage gaps name no project, so they stay"
    assert scoped["items"] == [ours["short_id"]]
    assert (scoped["id"], scoped["trigger"], scoped["window_start"]) == (
        review["id"],
        "cron",
        "2026-10-24T00:00:00+00:00",
    )
    both = client.get("/api/review").json()["review"]
    assert set(both["summaries"]["summaries"]) == {"in-alpha", "in-beta"}
    assert {r["short_id"] for r in both["facts"]["results"]} == {ours["short_id"], theirs["short_id"]}
    assert both["items"] == [ours["short_id"], theirs["short_id"]]


def test_scoped_groups_carry_suggested_and_in_progress_items(client, tmp_path):
    """Every grouped collection is scoped, not only the decisions."""
    _two_projects(tmp_path)
    store = reviews.ReviewStore()
    suggestion = _open(store, "suggest:alpha", project="alpha", initiative="in-alpha", section="suggested")
    legacy_suggestion = _open(store, "suggest:beta", initiative="in-beta", section="suggested")
    working = _open(store, "k:alpha:work", project="alpha", initiative="in-alpha")
    assert store.set_status(working["short_id"], "awaiting_confirm")
    legacy_working = _open(store, "k:beta:work", initiative="in-beta")
    assert store.set_status(legacy_working["short_id"], "awaiting_confirm")
    data = client.get("/api/review?project=alpha").json()
    group = {g["initiative"]: g for g in data["initiative_groups"]}["in-alpha"]
    assert group["suggested"] == [suggestion["short_id"]]
    assert group["in_progress"] == [working["short_id"]]
    assert [i["short_id"] for i in data["in_progress"]] == [working["short_id"]]
    theirs = client.get("/api/review?project=beta").json()
    assert [i["short_id"] for i in theirs["in_progress"]] == [legacy_working["short_id"]]
    assert [i["short_id"] for i in theirs["suggested"]] == [legacy_suggestion["short_id"]]


def test_waiting_cards_are_grouped_by_their_items_initiative(client, root, monkeypatch):
    """A card that waits on the user lands in the group of the Review item that parked it."""
    _settings(("demo", root))
    store = reviews.ReviewStore()
    item = _open(store, "k:wait", project="demo", initiative="in-demo")
    entry = {
        "project": "demo",
        "board": "default",
        "card": "t_00000001",
        "title": "Card",
        "status": "blocked",
        "assignee": "coder",
        "you": "Set up the TEST login.",
        "header": "BLOCKED",
        "since": "2026-10-24T00:00:00+00:00",
        "comment_author": "agent",
        "item": {"short_id": item["short_id"], "initiative": "in-demo", "project": "demo", "status": "open"},
    }
    monkeypatch.setattr(review_attention, "collect", lambda *args, **kwargs: {"items": [entry], "errors": []})
    data = client.get("/api/review").json()
    group = {g["initiative"]: g for g in data["initiative_groups"]}["in-demo"]
    assert group["waiting"] == ["t_00000001"]
    assert data["waiting_on_you"] == [entry]


def test_invalid_settings_fail_before_the_store_is_opened(client):
    """Settings are validated first: a broken file is a 500, and no review database is created."""
    data_dir = Path(os.environ["HERMES_INITIATIVES_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text('{"projects": [{"id": "BAD ID"}]}')
    for path in ("/api/review", "/api/review?project=demo"):
        response = client.get(path)
        assert response.status_code == 500, path
        assert "settings file is invalid" in response.json()["detail"]
    assert not (data_dir / "reviews.db").exists()


def test_an_empty_project_parameter_means_unscoped(client, root):
    _settings(("demo", root))
    store = reviews.ReviewStore()
    item = _open(store, "k:mine", project="demo", initiative="in-demo")
    for query in ("?project=", "?project=%20"):
        assert [i["short_id"] for i in client.get("/api/review" + query).json()["queue"]] == [item["short_id"]]
    assert client.get("/api/review?project=elsewhere").status_code == 404


def test_answer_route_applies_and_second_answer_is_refused(client):
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("req:job:1", section="gated", source="request:job", title="Ask", basis={})
    opts = [{"id": "A", "label": "Saw it", "action": "acknowledge", "args": {}}, OPTS[1]]
    store.add_version(item["short_id"], question="Q", options=opts, recommend="A", context="")
    first = client.post(f"/api/review/items/{item['short_id']}/answer", json={"option": "A", "note": "ok"}).json()
    assert first["result"]["state"] == "applied" and first["item"]["status"] == "applied"
    second = client.post(f"/api/review/items/{item['short_id']}/answer", json={"option": "B"}).json()
    assert second["result"]["state"] == "refused"
    assert [a["option"] for a in store.answers(item["short_id"])] == ["A"]
    store.create_review(trigger="cron", model="m", window_start=None)
    data = client.get("/api/review").json()
    assert {g["initiative"] for g in data["groups"]} <= {""}, "no initiative on this item"
    recent = data["recent"]
    assert [i["short_id"] for i in recent] == [item["short_id"]] and recent[0]["actions"][0]["receipt"] == "acknowledged"


def test_overrule_route_suspends_the_rule_and_marks_the_settled_row(client):
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("suggest:a", section="suggested", source="suggestion", title="Old ask", basis={"e": 1})
    assert store.settle(item["short_id"], "retire_suggestion", "replaced by D2")
    short = client.post(f"/api/review/items/{item['short_id']}/overrule", json={"reason": "no"})
    assert short.status_code == 422
    body = client.post(f"/api/review/items/{item['short_id']}/overrule", json={"reason": "It was not replaced."}).json()
    assert body["result"]["state"] == "overruled" and "retire_suggestion suspended" in body["result"]["message"]
    data = client.get("/api/review").json()
    assert data["rule_states"]["retire_suggestion"]["state"] == "suspended"
    assert [(i["short_id"], i["overruled"]) for i in data["settled"]] == [(item["short_id"], True)]
    again = client.post(f"/api/review/items/{item['short_id']}/overrule", json={"reason": "It was not replaced."})
    assert again.status_code == 409


def test_rule_route_suspends_and_restores_a_learned_rule(client):
    from hermes_initiatives_core import review_learned

    store = reviews.ReviewStore()
    spec = {
        "rule": "learned_x_items",
        "do": "apply",
        "signature": "s",
        "match": {"initiative": "in-x"},
        "when": "w" * 20,
        "why_not_auto": "no_rule: x",
    }
    rule = review_learned.append(store, spec, "D1")
    assert [x["rule"] for x in client.get("/api/review").json()["learned_rules"]] == [rule]
    body = client.post(f"/api/review/rules/{rule}", json={"state": "suspended", "reason": "too wide"}).json()
    assert body["result"]["message"] == f"rule {rule} suspended"
    assert store.rule_states()[rule]["state"] == "suspended"
    assert client.get("/api/review").json()["learned_rules"][0]["state"] == "suspended"
    assert client.post("/api/review/rules/learned_nope", json={"state": "active", "reason": "again"}).status_code == 404
    assert client.post("/api/review/rules/auto_delegated", json={"state": "active", "reason": "again"}).status_code == 404
    restored = client.post(f"/api/review/rules/{rule}", json={"state": "active", "reason": "fine now"}).json()
    assert restored["result"]["state"] == "active"


def test_auto_and_manual_routes_hand_an_item_over_and_back(client):
    store = reviews.ReviewStore()
    item, _ = store.upsert_item("request:job:9", section="gated", source="request:job", title="Ask", basis={})
    store.add_version(item["short_id"], question="Go?", options=OPTS, recommend="A", context="ctx")
    sid = item["short_id"]
    body = client.post(f"/api/review/items/{sid}/auto", json={"note": "Go if CI is green."}).json()
    assert body["result"]["state"] == "auto" and body["item"]["status"] == "auto"
    data = client.get("/api/review").json()
    assert data["queue"] == [] and [(i["short_id"], i["auto"]["note"]) for i in data["auto"]] == [(sid, "Go if CI is green.")]
    again = client.post(f"/api/review/items/{sid}/auto", json={}).json()
    assert again["result"]["state"] == "refused"
    back = client.post(f"/api/review/items/{sid}/manual").json()
    assert back["result"]["state"] == "open"
    assert [i["short_id"] for i in client.get("/api/review").json()["queue"]] == [sid]
