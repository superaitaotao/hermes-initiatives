#!/usr/bin/env python3
"""File a decision for the user's daily review, instead of writing the ask in a message.

  request_decision.py --source calendar-sync --key req:calendar-sync:evt-123 \\
      --title "Library replacement charge $25.00" --question "Pay the charge now, or check the library app first?" \\
      --option 'A|Pay it now|{"action": "handoff", "args": {}}' \\
      --option 'B|Check the app first|{"action": "defer", "args": {"kind": "state_change"}}' \\
      --recommend B --basis-json '{"email": "example-email"}' \\
      --owner-profile default --owner-job calendar-job [--project p] [--initiative in-x] \\
      [--deadline 2026-10-13T09:00+08:00] [--urgent-rule deadline] [--evidence <path or url>] [--blocks t_1,t_2]

A second request with the same open key updates the basis and the options; it does not create a new item.
Prints {"ok": true, "short_id": "D17", "created": bool}.

The owner applies handed-off answers itself, then acknowledges:

  request_decision.py --answers --source calendar-sync      answers waiting for this source (JSON list)
  request_decision.py --ack D17 --receipt "Paid $25.00"    record that the answer was applied
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import fail

from hermes_initiatives_core.reviews import ReviewError, ReviewStore


def _option(raw: str) -> dict:
    parts = raw.split("|", 2)
    if len(parts) != 3:
        fail(f"--option must be 'ID|label|action-json', got {raw!r}")
    option_id, label, spec = parts
    try:
        action = json.loads(spec)
    except json.JSONDecodeError as exc:
        fail(f"--option {option_id}: action JSON is invalid: {exc}")
    if not isinstance(action, dict):
        fail(f"--option {option_id}: action JSON must be an object")
    return {"id": option_id.strip(), "label": label.strip(), "action": action.get("action"), "args": action.get("args") or {}}


def _owner_mode(argv: list[str]) -> bool:
    if "--answers" not in argv and "--ack" not in argv:
        return False
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", action="store_true")
    parser.add_argument("--ack")
    parser.add_argument("--receipt", default="")
    parser.add_argument("--source")
    args = parser.parse_args(argv)
    store = ReviewStore()
    if args.answers:
        if not args.source:
            fail("--answers needs --source")
        print(json.dumps({"ok": True, "answers": store.for_owner(f"request:{args.source}")}, ensure_ascii=False))
        return True
    try:
        acked = store.acknowledge(args.ack, args.receipt)
    except ReviewError as exc:
        fail(str(exc))
    if not acked:
        fail(f"{args.ack} is not waiting for its owner")
    print(json.dumps({"ok": True, "short_id": args.ack.upper(), "status": "acked"}))
    return True


def main() -> None:
    if _owner_mode(sys.argv[1:]):
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="the asking job or profile, e.g. calendar-sync")
    parser.add_argument("--key", required=True, help="stable key, e.g. req:calendar-sync:<id>")
    parser.add_argument("--title", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--option", action="append", required=True, help='\'ID|label|{"action": ..., "args": {...}}\'')
    parser.add_argument("--recommend", required=True)
    parser.add_argument("--basis-json", required=True, help="the state this ask was raised against")
    parser.add_argument("--context", default="", help="2-4 lines the user needs to decide")
    parser.add_argument("--project")
    parser.add_argument("--initiative")
    parser.add_argument("--owner-profile")
    parser.add_argument("--owner-job")
    parser.add_argument("--deadline")
    parser.add_argument("--urgent-rule", choices=("security_consent", "protected_policy_consent", "deadline"))
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--blocks", default="", help="comma-separated card ids this decision blocks")
    args = parser.parse_args()
    try:
        basis = json.loads(args.basis_json)
    except json.JSONDecodeError as exc:
        fail(f"--basis-json is invalid: {exc}")
    key = args.key.strip()
    if not key.startswith("req:"):
        fail("--key must start with 'req:' (gate keys belong to the collector)")
    options = [_option(raw) for raw in args.option]
    store = ReviewStore()
    try:
        store.log_request(args.source, key, {k: v for k, v in vars(args).items()})
        from hermes_initiatives_core.reviews import validate_options

        validate_options(options, args.recommend)
        item, created = store.upsert_item(
            key,
            section="gated",
            source=f"request:{args.source}",
            title=args.title,
            basis=basis,
            blocks=[b.strip() for b in args.blocks.split(",") if b.strip()],
            links=[{"kind": "evidence", "ref": e} for e in args.evidence],
            project=args.project,
            initiative=args.initiative,
            owner_profile=args.owner_profile,
            owner_job=args.owner_job,
            deadline=args.deadline,
            urgent_rule=args.urgent_rule,
        )
        current = item["version"]
        same = current and current["question"] == args.question and current["basis_hash"] == item["basis_hash"]
        if not same and item["status"] in ("open", "watching"):
            store.add_version(
                item["short_id"], question=args.question, options=options, recommend=args.recommend, context=args.context
            )
    except ReviewError as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "short_id": item["short_id"], "created": created, "status": item["status"]}))


if __name__ == "__main__":
    main()
