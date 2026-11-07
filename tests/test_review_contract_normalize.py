"""Exact typed approval restoration at the locked source/Review boundary, not prose inference."""

import datetime as dt
import hashlib
import json

import pytest
from conftest import cycle_md, write

from hermes_initiatives_core import outcome_loop, review_collect, reviews, writers
from hermes_initiatives_core.docs import dump_markdown, parse_markdown
from hermes_initiatives_core.review_contract_normalize import normalize
from hermes_initiatives_core.review_settle import Settler, SettleRefused
from hermes_initiatives_core.settings import ProjectSettings

kb = pytest.importorskip("hermes_cli.kanban_db_connect")


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def normalizer_case(root, isolated_hermes, tmp_path):
    kb.init_db(board="default")
    initiative = root / "in-demo/initiative.md"
    writers.edit_frontmatter(
        initiative,
        root,
        lambda fm: fm.update(outcome_policy={"enabled": True, "owner": "planner", "session": "approved-session"}),
    )
    authority = {
        "date": "2026-09-02",
        "by": "user",
        "decision": "Use this measurement source",
        "fields": {"measurement": {"source": "The approved receipt", "primary_metric": "passing checks"}},
    }
    path = write(
        root / "in-demo/cycles/in-demo-c01.md",
        cycle_md(
            "in-demo-c01",
            stage="shipping",
            decisions=[authority],
            measurement={"source": None, "success_criteria": "Every check passes"},
            outcome_review={"enabled": True, "owner": "planner", "release_surface": "main_ci", "collector": "Read receipt"},
        ),
    )
    epoch = outcome_loop.input_epoch(parse_markdown(path.read_text(), path).frontmatter, [])
    evidence = write(tmp_path / "report.md", f"Assessment epoch: {epoch}\nSource field needs restoration.")
    proposal = write(tmp_path / "proposal.md", f"Assessment epoch: {epoch}\nKeep all approved criteria.")
    writers.record_observation(
        root,
        "in-demo-c01",
        {
            "kind": "assessment",
            "epoch": epoch,
            "status": "insufficient_data",
            "detail": "Source field needs restoration",
            "evidence": str(evidence),
            "proposal": str(proposal),
        },
    )
    project = ProjectSettings(id="demo", label="Demo", root=root, kanban_board="default")
    store = reviews.ReviewStore(tmp_path / "reviews.db")
    review_collect.sync(store, [project], gates={"default": {}})
    item = store.get_by_key("cycle:demo:in-demo-c01:gate")
    sid = item["short_id"]
    store.add_version(
        sid,
        question="Restore the contract?",
        options=[
            {
                "id": "A",
                "label": "Restore source",
                "action": "set_cycle_fields",
                "args": {"cycle": "in-demo-c01", "fields": {"measurement": {"source": "The approved receipt"}}},
            },
            {"id": "B", "label": "Wait", "action": "defer", "args": {}},
        ],
        recommend="A",
        context="Recorded authority",
    )
    item = store.get(sid)
    args = {
        "authority_decision": 0,
        "authority_sha256": digest(
            json.dumps(authority, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
        ),
        "source_sha256": digest(path.read_bytes()),
        "fields": ["source"],
    }
    settler = Settler(store, project)
    return settler, item, args, path, root


def test_restore_only_exact_approved_empty_fields_preserving_user_decisions(normalizer_case):
    settler, item, args, path, root = normalizer_case
    store, project, sid = settler.store, settler.project, item["short_id"]
    epoch = item["basis"]["epoch"]
    before = parse_markdown(path.read_text(), path)
    bad = dict(args, fields=["guardrails"])
    with pytest.raises(SettleRefused, match="typed approval"):
        settler.settle(
            sid, "normalize_outcome_contract", bad, "Restore the previously approved source without adopting any new criterion."
        )
    assert parse_markdown(path.read_text(), path).frontmatter == before.frontmatter
    receipt = settler.settle(
        sid, "normalize_outcome_contract", args, "Restore the previously approved source without adopting any new criterion."
    )
    assert receipt["rule"] == "normalize_outcome_contract"
    after = parse_markdown(path.read_text(), path)
    expected = dict(before.frontmatter["measurement"], source="The approved receipt")
    assert after.frontmatter["measurement"] == expected
    assert after.frontmatter["stage"] == "shipping" and after.frontmatter["result"]["verdict"] == "unset"
    assert after.frontmatter["decisions"][:-1] == before.frontmatter["decisions"]
    assert after.frontmatter["decisions"][-1]["by"] == "agent" and after.body == before.body
    assert store.answers(sid) == [] and store.get(sid)["status"] == "settled"
    from hermes_initiatives_core.store import load
    from hermes_initiatives_core.validate import validate

    assert validate(load(root), check_sources=False).ok
    assert outcome_loop.input_epoch(after.frontmatter, []) != epoch
    review_collect.sync(store, [project], gates={"default": {}})
    assert store.get_by_key(item["key"]) is None, "old receipt cannot answer the changed epoch"
    with pytest.raises(SettleRefused, match="open"):
        settler.settle(sid, "normalize_outcome_contract", args, "Do not apply a completed restoration twice.")


@pytest.mark.parametrize("case", ["stale_hash", "prose", "criteria", "suspended", "answered", "symlink", "conflict"])
def test_unsafe_normalization_refuses_without_source_or_answer_writes(normalizer_case, case, tmp_path):
    settler, item, args, path, root = normalizer_case
    if case == "stale_hash":
        args["source_sha256"] = "0" * 64
    elif case == "prose":
        writers.edit_frontmatter(path, root, lambda fm: fm["decisions"][-1].pop("fields"))
        fm = parse_markdown(path.read_text(), path).frontmatter
        args["authority_sha256"] = digest(
            json.dumps(fm["decisions"][-1], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        args["source_sha256"] = digest(path.read_bytes())
    elif case == "criteria":
        args["fields"] = ["success_criteria"]
    elif case == "suspended":
        settler.store.set_rule_state("normalize_outcome_contract", "suspended", "User overruled this rule")
    elif case == "answered":
        settler.store.answer(item["short_id"], "B", note="Keep it parked", surface="tab")
    elif case == "symlink":
        target = tmp_path / "outside.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif case == "conflict":
        writers.edit_frontmatter(path, root, lambda fm: fm["measurement"].update(source="Another source"))
        args["source_sha256"] = digest(path.read_bytes())
    before = path.read_bytes()
    answers = settler.store.answers(item["short_id"])
    with pytest.raises(SettleRefused):
        normalize(settler, item, args, "Restore a field only when the exact authority remains provable.")
    assert path.read_bytes() == before
    assert settler.store.answers(item["short_id"]) == answers


@pytest.mark.parametrize("basis", [["outcome_review_ready"], "outcome_review_ready", 3])
def test_malformed_basis_is_refused_without_writes(normalizer_case, basis):
    """A basis that is not a mapping cannot prove the ready review; it refuses instead of raising AttributeError."""
    settler, item, args, path, root = normalizer_case
    before = path.read_bytes()
    with pytest.raises(SettleRefused, match="basis"):
        normalize(settler, dict(item, basis=basis), args, "Restore a field only when the recorded basis is a real mapping.")
    assert path.read_bytes() == before
    assert settler.store.answers(item["short_id"]) == []


@pytest.mark.parametrize("why", [None, 5, ["a sentence long enough to pass"], {"why": "sentence long enough"}])
def test_non_text_why_is_refused_without_writes(normalizer_case, why):
    """A non-text reason must refuse at both entry points, never abort the CLI with AttributeError."""
    settler, item, args, path, root = normalizer_case
    before = path.read_bytes()
    with pytest.raises(SettleRefused, match="why"):
        settler.settle(item["short_id"], "normalize_outcome_contract", args, why)
    with pytest.raises(SettleRefused, match="why"):
        normalize(settler, item, args, why)
    assert path.read_bytes() == before
    assert settler.store.answers(item["short_id"]) == []


def test_yaml_date_authority_is_canonicalized_not_crashed(normalizer_case):
    """An unquoted YAML date parses as datetime.date; the canonical hash must stringify it, not raise TypeError."""
    settler, item, args, path, root = normalizer_case
    writers.edit_frontmatter(path, root, lambda fm: fm["decisions"][-1].update(date=dt.date(2026, 9, 2)))
    row = parse_markdown(path.read_text(), path).frontmatter["decisions"][-1]
    assert isinstance(row["date"], dt.date) and not isinstance(row["date"], str)
    args["authority_sha256"] = digest(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    )
    args["source_sha256"] = digest(path.read_bytes())
    receipt = settler.settle(
        item["short_id"],
        "normalize_outcome_contract",
        args,
        "Restore the approved source though its date parses as a YAML date.",
    )
    assert receipt["rule"] == "normalize_outcome_contract"
    from hermes_initiatives_core.store import load
    from hermes_initiatives_core.validate import validate

    assert validate(load(root), check_sources=False).ok


def test_initiative_authority_changed_during_claim_refuses_cycle_write(normalizer_case, monkeypatch):
    settler, item, args, path, root = normalizer_case
    original = settler.store.claim_under_rule
    initiative = root / "in-demo/initiative.md"
    before = path.read_bytes()

    def claim(*a, **kw):
        result = original(*a, **kw)
        writers.edit_frontmatter(
            initiative,
            root,
            lambda fm: fm["decisions"].append({"by": "user", "date": "2026-10-24", "decision": "Stop restoring the contract"}),
        )
        return result

    monkeypatch.setattr(settler.store, "claim_under_rule", claim)
    from hermes_initiatives_core.review_exec import ActionFailed

    with pytest.raises(ActionFailed, match="initiative.*changed"):
        settler.settle(
            item["short_id"],
            "normalize_outcome_contract",
            args,
            "Restore the source only when both authority files stay unchanged.",
        )
    assert path.read_bytes() == before
    assert settler.store.answers(item["short_id"]) == []


@pytest.mark.parametrize("case", ["authority", "opened"])
def test_missing_authority_dates_refuse(normalizer_case, case):
    settler, item, args, path, root = normalizer_case
    if case == "authority":
        # Write-time validation refuses an edit that drops a decision date, so plant the malformed
        # authority by hand: the normalizer must refuse it on its own hash/chronology checks.
        doc = parse_markdown(path.read_text(encoding="utf-8"), path)
        doc.frontmatter["decisions"][-1].pop("date")
        path.write_text(dump_markdown(doc.frontmatter, doc.body), encoding="utf-8")
        row = parse_markdown(path.read_text(), path).frontmatter["decisions"][-1]
        args["authority_sha256"] = digest(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
        )
        args["source_sha256"] = digest(path.read_bytes())
    else:
        item = dict(item, opened_at=None)
    before = path.read_bytes()
    with pytest.raises(SettleRefused, match="dates"):
        normalize(settler, item, args, "Restore only when the authority chronology can be established.")
    assert path.read_bytes() == before
