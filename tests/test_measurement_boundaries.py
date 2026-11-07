"""Regression tests for measurement transitions, incremental repairs and fact retention."""

import json

import pytest
from conftest import DECIDED, MEASUREMENT, cycle_md, write

from hermes_initiatives_core import lifecycle, review_exec, writers
from hermes_initiatives_core.settings import ProjectSettings
from hermes_initiatives_core.validate import cycle_edit_errors, cycle_errors

VERIFIED = {"at": "2026-10-11T00:00:00Z", "by": "a", "kind": "release_verified", "evidence": "r.md"}


def cycle(**overrides):
    return {
        "id": "in-demo-c01",
        "title": "Demo",
        "stage": "measuring",
        "decisions": DECIDED,
        "measurement": dict(MEASUREMENT),
        **overrides,
    }


@pytest.mark.parametrize("window", [False, 0, [], {}, "seven days", {"starts": "bad", "length_days": 7}])
def test_malformed_windows_are_not_spot_checks(window):
    fm = cycle(measurement={**MEASUREMENT, "observation_window": window})
    assert any("observation_window" in error for error in cycle_errors(fm, [VERIFIED]))
    assert not lifecycle.spot_check_verdict_due(fm, "measuring", {"state": "verified"})


def test_invalid_replacement_window_is_refused_even_when_error_unchanged():
    before = cycle(measurement={**MEASUREMENT, "observation_window": {"starts": "bad", "length_days": 7}})
    after = cycle(measurement={**MEASUREMENT, "observation_window": "seven days"})
    assert any("observation_window" in error for error in cycle_edit_errors(before, after, [VERIFIED]))


def test_incremental_measurement_repairs_are_allowed():
    before = cycle(measurement={})
    after = cycle(measurement={"primary_metric": "m"})
    assert cycle_edit_errors(before, after, [VERIFIED]) == []


@pytest.mark.parametrize("text", ["no frontmatter", "---\nid: [\n---\n", "---\nmeasurement: []\n---\n"])
def test_bad_cycle_diagnostics_do_not_lose_an_observation(root, text):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", text)
    row = writers.record_observation(root, "in-demo-c01", {"kind": "release_verified", "evidence": "r.md"})
    log = root / "in-demo" / "observations" / "in-demo-c01.jsonl"
    assert json.loads(log.read_text().splitlines()[-1])["kind"] == "release_verified"
    assert row["cycle_warnings"]


def test_preview_includes_the_user_decision_that_the_answer_will_record(root):
    write(root / "in-demo" / "cycles" / "in-demo-c01.md", cycle_md("in-demo-c01"))
    project = ProjectSettings(id="demo", label="Demo", root=root)
    assert review_exec.cycle_fields_problems(project, "in-demo-c01", {"stage": "shipping"}) == []


def test_already_invalid_cycle_cannot_advance_without_destination_fields():
    before = cycle(stage="shipping", measurement={})
    after = cycle(stage="measuring", measurement={})
    assert any("measurement fields" in error for error in cycle_edit_errors(before, after, [VERIFIED]))
