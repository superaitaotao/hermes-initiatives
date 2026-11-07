#!/usr/bin/env python3
"""Time list and detail builds on a synthetic large root (30 initiatives x 20 cycles x 50 observations).

bench.py [scratch-dir]      # builds the data once, then prints cold and warm timings
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_initiatives_core import store, views  # noqa: E402
from hermes_initiatives_core.docs import dump_markdown  # noqa: E402
from hermes_initiatives_core.settings import ProjectSettings  # noqa: E402

MEASUREMENT = {
    "primary_metric": "Neutral AI citations on tracker IDs 5, 7, 8, 16",
    "source": "GEO citation tracker",
    "baseline": {"value": "0/8 usable", "period": "2026-10-02"},
    "observation_window": {"starts": "verified_release", "length_days": 42},
    "success_criteria": ">=3/8 neutral citations",
    "guardrails": ["No regression on guide clicks"],
    "insufficient_data": "Extend once by 28 days",
}


def build(root: Path) -> None:
    if (root / ".built").exists():
        return
    for i in range(30):
        iid = f"in-bench-{i:02d}"
        decision = [{"date": "2026-09-01", "by": "user", "decision": "Start"}]
        initiative = {"id": iid, "title": f"Bench {i}", "status": "active", "rank": i, "decisions": decision}
        (root / iid / "cycles").mkdir(parents=True, exist_ok=True)
        (root / iid / "observations").mkdir(parents=True, exist_ok=True)
        (root / iid / "initiative.md").write_text(dump_markdown(initiative, "Brief.\n"))
        for c in range(20):
            cid = f"{iid}-c{c:02d}"
            fm = {
                "id": cid,
                "title": f"Cycle {c}",
                "stage": "measuring",
                "decisions": decision,
                "kanban_cards": [],
                "measurement": MEASUREMENT,
                "result": {"verdict": "unset", "evidence": []},
                "disposition": {"action": "unset", "follow_ups": []},
            }
            (root / iid / "cycles" / f"{cid}.md").write_text(dump_markdown(fm, "Body.\n" * 20))
            rows = [{"at": "2026-10-01T00:00:00Z", "by": "b", "kind": "release_verified", "evidence": "r.md"}]
            rows += [
                {
                    "at": f"2026-10-{(n % 28) + 2:02d}T{n % 24:02d}:00:{n:02d}Z",
                    "by": "b",
                    "kind": "metric",
                    "metric": "m",
                    "value": n,
                    "evidence": "r.md",
                }
                for n in range(49)
            ]
            (root / iid / "observations" / f"{cid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (root / ".built").write_text("ok")


def timed(fn, runs: int) -> float:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main() -> None:
    scratch = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ini-bench-large")
    root = scratch / "initiatives"
    root.mkdir(parents=True, exist_ok=True)
    build(root)
    project = ProjectSettings(id="bench", label="Bench", root=root)
    files = sum(1 for _ in root.rglob("*.md")) + sum(1 for _ in root.rglob("*.jsonl"))
    start = time.perf_counter()
    views.build_list(project)
    cold = (time.perf_counter() - start) * 1000
    parses = store.cache_parse_count()
    warm_list = timed(lambda: views.build_list(project), 5)
    warm_detail = timed(lambda: views.build_detail(project, "in-bench-07"), 5)
    print(
        json.dumps(
            {
                "files": files,
                "cold_list_ms": round(cold, 1),
                "parses_cold": parses,
                "warm_list_ms": round(warm_list, 1),
                "warm_detail_ms": round(warm_detail, 1),
                "reparses_when_warm": store.cache_parse_count() - parses,
            }
        )
    )


if __name__ == "__main__":
    main()
