"""Shadow-week coverage gaps: asks that alert jobs delivered, with no matching review item.

A gap is a line in a delivered cron response (the text after ``## Response``) that asks for the user. A line
that names a card id is an exact gap when no active or recent item key contains that id. A line without an id
is a heuristic gap; the digest says so. Prompt text is ignored: only what was delivered counts.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from .reviews import ReviewStore
from .settings import AlertJob

ASK = re.compile(
    r"NEEDS YOU|\bYou:\*{0,2}\s*(?!nothing|no\b|none)|for (?:your )?review|your (?:decision|approval|input|consent)"
    r"|\bplease (?:approve|confirm|decide|choose|reply)|\bawaiting (?:your|user|human)|\bA or [BC]\b",
    re.IGNORECASE,
)
CARD = re.compile(r"\bt_[0-9a-f]{8}\b")
PR = re.compile(r"(?:PR\s*#|pull/)(\d+)")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})$")


def _response(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = text.find("\n## Response")
    return text[marker + len("\n## Response") :] if marker >= 0 else ""


def _when(path: Path) -> dt.datetime | None:
    match = STAMP.match(path.stem)
    if not match:
        return None
    day, hh, mm, ss = match.groups()
    # Cron output names use the host's local time.
    return dt.datetime.fromisoformat(f"{day}T{hh}:{mm}:{ss}").astimezone(dt.UTC)


def scan(
    store: ReviewStore, hermes_root: Path, *, since: dt.datetime, alert_jobs: list[AlertJob]
) -> list[dict[str, Any]]:
    keys = " ".join(i["key"] for i in store.items(limit=5000))
    gaps: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for configured in alert_jobs:
        profile, job, label = configured.profile, configured.job, configured.label
        home = hermes_root / "profiles" / profile if profile else hermes_root
        folder = home / "cron" / "output" / job
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            when = _when(path)
            if when is None or when < since:
                continue
            body = _response(path)
            if body.strip().startswith("[SILENT]"):
                continue
            for raw in body.splitlines():
                line = raw.strip()
                if not line or not ASK.search(line):
                    continue
                refs = CARD.findall(line) + [f"#{n}" for n in PR.findall(line)]
                if refs:
                    for ref in refs:
                        if ref.lstrip("#") in keys or (label, ref) in seen:
                            continue
                        seen.add((label, ref))
                        gaps.append(
                            {
                                "source": label,
                                "ref": ref,
                                "line": line[:240],
                                "match": "exact",
                                "at": when.isoformat(),
                                "file": str(path),
                            }
                        )
                elif (label, line[:120]) not in seen:
                    seen.add((label, line[:120]))
                    gaps.append(
                        {
                            "source": label,
                            "ref": None,
                            "line": line[:240],
                            "match": "heuristic",
                            "at": when.isoformat(),
                            "file": str(path),
                        }
                    )
    return gaps
