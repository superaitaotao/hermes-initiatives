#!/usr/bin/env python3
"""Daily review: one place for every decision that needs the user.

The 30-minute review tick (its own cron, after the gate list is written) runs:
  daily_review.py tick --gates <gates.json>        sync every source (Kanban = the gate list), print only the
                                                    items that need options; no review row. Also files one card
                                                    per handed-off answer whose owner is a profile with no job
                                                    (review_handoff)
  daily_review.py write --file out.json             store those options (no review id needed)
  daily_review.py settle --file settle.json         close items under a rule the user delegated (checked in code)
  daily_review.py reopen D19 --reason "..."         return a deferred item whose named condition is met
  daily_review.py auto --file auto.json             handle items the user marked auto: apply, resolve, or return;
                                                    an apply or resolve appends a learned rule (review_learned)
  daily_review.py rules [--matches]                 all delegated rules (coded and learned) with their state;
                                                    --matches: open items a learned rule covers now
  daily_review.py notify --target <t>               push each new decision once, inside the configured push window
  daily_review.py daily --due                       exit 0 when today's digest is due (configured digest time, not yet sent)
  daily_review.py daily --gates <gates.json>        start today's review: print summaries/updates input as JSON
  daily_review.py send --target <t>                 send today's digest (retried by later ticks until it lands)

Older steps (a standalone daily run) still work:
  daily_review.py instructions --target <t>         print the generator prompt with this host's paths
  daily_review.py collect [--trigger cron|manual]   sync live gates into reviews.db, start a review, print the
                                                    generator's input as JSON
  daily_review.py write --file out.json             validate and store the generator's output (all or nothing)
  daily_review.py digest [--review R…]              print the Telegram digest; messages are separated by a line
                                                    containing only "\\f"
  daily_review.py send [--review R…] --target telegram:<chat>:<topic>
                                                    send the digest, one Telegram message per part
  daily_review.py status                            counts by status, the latest review, and the queue

  daily_review.py answer "D3 A: note"                apply review commands (same syntax as the Telegram topic);
                                                    "auto D3: note" hands an item to the agents, "manual D3" takes it back

Only `answer` writes to sources, through the review executor (review_exec): Kanban via the hermes CLI, cycle and
idea files via locked, read-back edits of user-owned fields. Merges are not executed from the review.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from _common import fail

from hermes_initiatives_core import review_collect, review_digest, review_gaps, review_write, writers
from hermes_initiatives_core.reviews import ReviewError, ReviewStore
from hermes_initiatives_core.settings import Settings, data_dir, hermes_root, load_settings

GENERATOR_MODEL = os.environ.get("HERMES_INITIATIVES_GENERATOR_LABEL", "review agent")


class SendFailed(RuntimeError):
    """`hermes send` did not deliver a message."""


def _instructions(args: argparse.Namespace) -> None:
    """Print the generator instructions with this host's paths (``--tick``: the review tick's step)."""
    name = "review_tick.md" if args.tick else "daily_review_generator.md"
    text = (Path(__file__).resolve().parents[1] / "references" / name).read_text(encoding="utf-8")
    workdir = data_dir() / "review-work"
    workdir.mkdir(parents=True, exist_ok=True)
    values = {
        "<scripts>": str(Path(__file__).resolve().parent),
        "<target>": args.target,
        "<trigger>": args.trigger,
        "<workdir>": str(workdir),
        "<gates>": str(args.gates or workdir / "gates.json"),
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    print(text)


def _sweep_links(projects) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """Link cards whose body names a cycle before the gates are read, so the review sees the full card set."""
    linked: dict[str, dict[str, list[str]]] = {}
    errors: list[str] = []
    for project in projects:
        if not project.kanban_board:
            continue
        try:
            done = writers.sweep_links(project.root, project.kanban_board)
        except writers.WriteRefused as exc:
            errors.append(f"{project.id}: card link sweep: {exc}")
            continue
        if done:
            linked[project.id] = done
    return linked, errors


def _gates(args: argparse.Namespace) -> review_collect.GateList | None:
    """The human gate list, or None (Kanban then stays unread; the error is reported, nothing resolves)."""
    path = getattr(args, "gates", None)
    if not path:
        return None
    try:
        return review_collect.load_gate_list(Path(path))
    except review_collect.GateListError as exc:
        sys.stderr.write(f"warning: {exc}\n")
        return None


def _sync(store: ReviewStore, config, args: argparse.Namespace) -> tuple[dict, dict]:
    linked, link_errors = _sweep_links(config.projects)
    sync = review_collect.sync(store, config.projects, today=dt.date.today(), gates=_gates(args))
    sync["errors"] = [*link_errors, *sync["errors"]]
    return sync, linked


def _item_input(store: ReviewStore, items: list[dict]) -> list[dict]:
    needs = {i["short_id"] for i in store.needs_options()}
    return [
        {
            "short_id": i["short_id"],
            "key": i["key"],
            "source": i["source"],
            "status": i["status"],
            "title": i["title"],
            "project": i["project"],
            "initiative": i["initiative"],
            "cycle": i["cycle"],
            "blocks": i["blocks"],
            "links": i["links"],
            "opened_at": i["opened_at"],
            "context": i["context"],
            "needs_options": i["short_id"] in needs,
            "previous": i["version"],
            "watch_reason": i.get("watch_reason"),
        }
        for i in items
        if i["section"] == "gated"
    ]


def _tick(args: argparse.Namespace) -> None:
    """Per-tick step: sync, then print only the items that need options. No review row, no summaries."""
    config, _ = load_settings()
    store = ReviewStore()
    from hermes_initiatives_core import outcome_loop, outcome_migration

    maintenance = {}
    maintenance_errors = []
    for project in config.projects:
        try:
            report = outcome_migration.maintain(project.root, today=dt.date.today())
            maintenance[project.id] = report["counts"]
            maintenance_errors.extend(f"{project.id}: {error}" for error in report["errors"])
        except (outcome_migration.MigrationRefused, writers.WriteRefused, OSError) as exc:
            maintenance_errors.append(f"{project.id}: outcome onboarding: {exc}")
    assessments = outcome_loop.dispatch(config)
    sync, _ = _sync(store, config, args)
    needs = [i for i in _item_input(store, store.items(statuses=("open",))) if i["needs_options"]]
    deferred = [
        {"short_id": i["short_id"], "title": i["title"], "deferral": i["deferral"], "context": i["context"][:500]}
        for i in store.items(statuses=("deferred",))
    ]
    from hermes_initiatives_core import review_auto, review_handoff, review_learned, review_routine, review_sweep

    # Handed-off answers whose owner is a profile with no job (a request from an interactive session) get a card.
    handoffs = review_handoff.dispatch(store, config)
    print(
        json.dumps(
            {
                "items": needs,
                "handoff_cards": handoffs["created"],
                "outcome_assessments": assessments,
                "outcome_onboarding": maintenance,
                "auto": review_auto.pending(store),
                # Items nobody looked at for 3 days: keep, refresh, resolve, or apply (review_tick.md "Stale sweep").
                "stale": review_sweep.listing(store),
                # Open decisions shown without a what/why summary: write one with `summarize` (review_tick.md).
                "unsummarised": [
                    {"short_id": i["short_id"], "title": i["title"], "question": i["version"]["question"]}
                    for i in store.queue()
                    if i["version"] and not i["version"].get("summary") and i["source"] != "learn"
                ],
                "learned_rules": review_learned.matches(store),
                # Reconsider already-written routine recommendations too, not just items needing options.
                "routine_candidates": review_routine.candidates(store),
                "deferred": deferred,
                "errors": [*maintenance_errors, *sync["errors"], *handoffs["errors"], *assessments["errors"]],
                "sync": {k: sync[k] for k in ("opened", "resolved", "reopened")},
                "actions": sorted(review_write.ACTIONS_HELP.items()),
            },
            indent=1,
            default=str,
        )
    )


def _hermes_send(target: str, message: str) -> str | None:
    proc = subprocess.run(
        [os.environ.get("HERMES_BIN") or "hermes", "send", "--to", target, "--json"],
        input=message,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return _send_error(proc)


def _hermes_send_with_buttons(target: str, message: str, keyboard: dict) -> tuple[str | None, str | None]:
    """One push with its inline keyboard, and the message id to record. Falls back to plain text inside
    ``review_buttons.send`` whenever the buttons cannot go out, so a push is never lost to the new path."""
    from hermes_initiatives_core import review_buttons

    return review_buttons.send(target, message, keyboard)


def notify(
    store: ReviewStore,
    target: str,
    send: Callable[[str, str], str | None],
    *,
    now: dt.datetime | None = None,
    titles: dict[str, str],
    baseline: bool = False,
    settings: Settings | None = None,
    send_with_buttons: Callable[[str, str, dict], tuple[str | None, str | None]] | None = None,
) -> list[str]:
    """Push each new decision once (per item and options version), inside the configured push window.
    Decisions outside the window wait for the next one.

    A failed send raises SendFailed and records nothing for that item, so the next tick retries it.
    ``baseline`` records the current decisions as pushed without sending (once, at cutover).
    With ``send_with_buttons`` a decision that has options carries its inline keyboard and its message id is
    recorded; every other push, and any button failure, goes out as plain text."""
    now = now or dt.datetime.now(dt.UTC)
    config = settings if settings is not None else load_settings()[0]
    if not baseline and now.astimezone(config.zone()).hour not in config.push_hours():
        return []
    pushed = []
    for item in store.to_push():
        message = review_digest.push_message(item, titles)
        keyboard = review_digest.push_keyboard(item)
        error, message_id = None, None
        if baseline:
            pass
        elif send_with_buttons is not None and keyboard is not None:
            error, message_id = send_with_buttons(target, message, keyboard)
        else:
            error = send(target, message)
        if error:
            raise SendFailed(f"{item['short_id']} was not delivered: {error}")
        store.record_push(item, message_id)
        pushed.append(item["short_id"])
    return pushed


def _notify(args: argparse.Namespace) -> None:
    config, _ = load_settings()
    try:
        pushed = notify(
            ReviewStore(),
            args.target,
            _hermes_send,
            titles=review_digest.initiative_titles(config.projects),
            baseline=args.baseline,
            settings=config,
            send_with_buttons=None if args.baseline else _hermes_send_with_buttons,
        )
    except SendFailed as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, "pushed": pushed}))


def daily_due(
    store: ReviewStore, *, now: dt.datetime | None = None, settings: Settings | None = None
) -> bool:
    """Due from the configured digest time until a review generated on that local day is delivered."""
    config = settings if settings is not None else load_settings()[0]
    zone = config.zone()
    local = (now or dt.datetime.now(dt.UTC)).astimezone(zone)
    return local.time() >= config.digest_time() and store.last_digest_day(zone) != local.date()


def _daily(args: argparse.Namespace) -> None:
    store = ReviewStore()
    config, _ = load_settings()
    zone = config.zone()
    if args.due:
        sys.exit(0 if daily_due(store, settings=config) else 1)
    latest = store.latest_review()
    today = dt.datetime.now(dt.UTC).astimezone(zone).date()
    if latest and dt.datetime.fromisoformat(latest["generated_at"]).astimezone(zone).date() == today and not args.new:
        # Written but not delivered (a failed send): reuse it, do not start a second review today.
        print(json.dumps({"review_id": latest["id"], "reuse": True}))
        return
    args.trigger = "cron"
    _collect(args)


def _collect(args: argparse.Namespace) -> None:
    config, _ = load_settings()
    store = ReviewStore()
    today = dt.date.today()
    window_start = store.last_completed_at()
    sync, linked = _sync(store, config, args)
    review = store.create_review(trigger=args.trigger, model=GENERATOR_MODEL, window_start=window_start)
    open_items = store.items(statuses=("open", "watching"))
    store.link_items(review["id"], [i["short_id"] for i in open_items if i["section"] == "gated"])
    since = dt.datetime.fromisoformat(window_start) if window_start else dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    gaps = review_gaps.scan(store, hermes_root(), since=since, alert_jobs=config.alert_jobs)
    previous = store.latest_review_before(review["id"])
    results = [
        {"short_id": i["short_id"], "status": i["status"], "note": i.get("closed_note") or ""}
        for i in store.items(statuses=("resolved_elsewhere", "applied", "settled", "failed", "stale"))
        if previous and i["updated_at"] >= previous["generated_at"]
    ]
    projects = []
    unchanged: list[str] = []
    for project in config.projects:
        updates = review_collect.updates_since(project, window_start or since.isoformat())
        summaries = review_collect.summaries(project, today=today)
        unchanged.extend(s["id"] for s in summaries if not updates["by_initiative"].get(s["id"]))
        projects.append({"project": project.id, "label": project.label, "initiatives": summaries, "updates": updates})
    facts = {
        "results": results,
        "linked_cards": linked,
        "urgent_pushes": len(store.urgent_pushes_since(window_start)),
        "unchanged": unchanged,
        "sync": {k: v for k, v in sync.items() if k != "updated"},
    }
    store.update_review(review["id"], gaps_json=json.dumps(gaps), facts_json=json.dumps(facts, default=str))
    items = _item_input(store, open_items)
    print(
        json.dumps(
            {
                "review_id": review["id"],
                "window_start": window_start,
                "items": items,
                "projects": projects,
                "facts": facts,
                "gaps": gaps,
                "errors": sync["errors"],
                "actions": sorted(review_write.ACTIONS_HELP.items()),
            },
            indent=1,
            default=str,
        )
    )


def _write(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read())
    store = ReviewStore()
    result = review_write.apply(store, payload)
    print(json.dumps({"ok": not result["errors"], **result}, indent=1))
    if result["errors"]:
        sys.exit(2)


def _review_id(store: ReviewStore, value: str | None) -> str:
    review = store.review(value) if value else store.latest_review()
    if review is None:
        fail(f"no review {value or '(none yet)'}")
    return review["id"]


def _digest(args: argparse.Namespace) -> None:
    store = ReviewStore()
    print("\n\f\n".join(review_digest.render(store, _review_id(store, args.review))))


def _send_error(proc: subprocess.CompletedProcess) -> str | None:
    """Why `hermes send` did not deliver, or None. Exit 0 is not enough: a cron session that auto-delivers to
    the same target gets ``{"success": true, "skipped": true}`` and nothing is sent."""
    if proc.returncode != 0:
        return proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if isinstance(result, dict) and (result.get("skipped") or result.get("success") is False):
        return str(result.get("reason") or result.get("error") or "skipped")
    return None


def _send(args: argparse.Namespace) -> None:
    store = ReviewStore()
    review_id = _review_id(store, args.review)
    messages = review_digest.render(store, review_id)
    sent = []
    for index, message in enumerate(messages):
        error = _hermes_send(args.target, message)
        if error:
            # Nothing is recorded as delivered, so `daily --due` stays true and the next tick sends it again.
            fail(f"part {index + 1}/{len(messages)} was not delivered: {error}")
        sent.append({"part": index + 1})
    store.update_review(review_id, telegram_json=json.dumps(sent))
    print(json.dumps({"ok": True, "review": review_id, "parts": len(sent)}))


def _status(_: argparse.Namespace) -> None:
    store = ReviewStore()
    counts: dict[str, int] = {}
    for item in store.items(limit=5000):
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    latest = store.latest_review()
    print(
        json.dumps(
            {
                "counts": counts,
                "latest_review": latest and {k: latest[k] for k in ("id", "generated_at", "completed_at", "trigger")},
                "last_completed_at": store.last_completed_at(),
                "queue": [f"{i['short_id']} {i['title'][:80]}" for i in store.queue()],
            },
            indent=1,
        )
    )


def _answer(args: argparse.Namespace) -> None:
    from hermes_initiatives_core.review_exec import Executor, format_results, parse_message

    text = sys.stdin.read() if args.text == "-" else args.text
    commands = parse_message(text)
    if commands is None:
        fail("every line must be a review command: 'D3 A', 'D3 B: note', 'confirm D3', 'defer D3 until 2026-10-20'")
    results = Executor(ReviewStore()).run_commands(commands, surface="tab")
    print(format_results(results))
    print(json.dumps({"ok": all(r["state"] not in ("refused", "failed") for r in results), "results": results}))


def _settle(args: argparse.Namespace) -> None:
    """Close items under a delegated rule (review_settle). All-or-nothing per item; prints one result per item."""
    from hermes_initiatives_core.review_exec import ActionFailed
    from hermes_initiatives_core.review_settle import Settler, SettleRefused

    payload = json.loads(Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read())
    entries = payload.get("settle") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        fail('settle file must be {"settle": [{"short_id", "rule", "args", "why"}]}')
    config, _ = load_settings()
    store = ReviewStore()
    results = []
    for entry in entries:
        if not isinstance(entry, dict):
            results.append({"ok": False, "status": "refused", "error": "settlement entry must be an object"})
            continue
        sid = str(entry.get("short_id") or "").upper()
        item = store.get(sid)
        project = config.project(item["project"]) if item and item.get("project") else None
        project = project or (config.projects[0] if len(config.projects) == 1 else None)
        try:
            if project is None:
                raise SettleRefused(f"{sid}: no project in settings")
            done = Settler(store, project).settle(sid, str(entry.get("rule") or ""), entry.get("args", {}), entry.get("why", ""))
            results.append({"ok": True, **done})
        except SettleRefused as exc:
            results.append({"ok": False, "short_id": sid, "status": "refused", "error": str(exc)})
        except (ActionFailed, ReviewError, OSError, sqlite3.Error) as exc:
            results.append({"ok": False, "short_id": sid, "status": "failed", "error": str(exc)})
    print(json.dumps({"ok": all(r["ok"] for r in results), "results": results}, indent=1))
    if not all(r["ok"] for r in results):
        sys.exit(2)


def _auto(args: argparse.Namespace) -> None:
    """Handle items the user marked auto (review_auto). One result per entry; a refusal writes nothing."""
    from hermes_initiatives_core.review_auto import AutoRefused, handle
    from hermes_initiatives_core.review_exec import ActionFailed

    payload = json.loads(Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read())
    entries = payload.get("auto") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        fail('auto file must be {"auto": [{"short_id", "do": "apply|resolve|return", "option"?, "why", "rule"?}]}')
    store = ReviewStore()
    results = []
    for entry in entries:
        sid = str((entry or {}).get("short_id") or "").upper() if isinstance(entry, dict) else ""
        try:
            if not isinstance(entry, dict):
                raise AutoRefused("each entry must be an object")
            done = handle(store, entry)
            results.append({"ok": done["state"] not in ("failed", "refused", "stale"), **done})
        except AutoRefused as exc:
            results.append({"ok": False, "short_id": sid, "state": "refused", "message": str(exc)})
        except (ActionFailed, ReviewError, OSError, sqlite3.Error) as exc:
            results.append({"ok": False, "short_id": sid, "state": "failed", "message": str(exc)})
    print(json.dumps({"ok": all(r["ok"] for r in results), "results": results}, indent=1))
    if not all(r["ok"] for r in results):
        sys.exit(2)


def _sweep(args: argparse.Namespace) -> None:
    """Keep, refresh, resolve, or apply items idle for 3 days (review_sweep). One result per entry."""
    from hermes_initiatives_core.review_exec import ActionFailed
    from hermes_initiatives_core.review_sweep import SweepRefused, handle

    payload = json.loads(Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read())
    entries = payload.get("sweep") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        fail('sweep file must be {"sweep": [{"short_id", "do": "keep|refresh|resolve|apply", "why", "option"?, "risk"?}]}')
    config, _ = load_settings()
    store = ReviewStore()
    results = []
    for entry in entries:
        sid = str((entry or {}).get("short_id") or "").upper() if isinstance(entry, dict) else ""
        try:
            if not isinstance(entry, dict):
                raise SweepRefused("each entry must be an object")
            item = store.get(sid)
            project = config.project(item["project"]) if item and item.get("project") else None
            done = handle(store, entry, project=project)
            results.append({"ok": True, **done})
        except SweepRefused as exc:
            results.append({"ok": False, "short_id": sid, "state": "refused", "message": str(exc)})
        except (ActionFailed, ReviewError, OSError, sqlite3.Error) as exc:
            results.append({"ok": False, "short_id": sid, "state": "failed", "message": str(exc)})
    print(json.dumps({"ok": all(r["ok"] for r in results), "results": results}, indent=1))
    if not all(r["ok"] for r in results):
        sys.exit(2)


def _rules(args: argparse.Namespace) -> None:
    """Every delegated rule: the coded ones and those appended from auto marks, with their state."""
    from hermes_initiatives_core import review_learned
    from hermes_initiatives_core.model import AGENT_RULES

    store = ReviewStore()
    if args.matches:
        print(json.dumps({"ok": True, "matches": review_learned.matches(store)}, indent=1, default=str))
        return
    states = store.rule_states()
    coded = [{"rule": r, "state": (states.get(r) or {"state": "active"})["state"], "kind": "coded"} for r in AGENT_RULES]
    learned = [{**r, "kind": "learned"} for r in review_learned.all_rules(store)]
    print(json.dumps({"ok": True, "rules": coded + learned}, indent=1, default=str))


def _reopen(args: argparse.Namespace) -> None:
    """Return a deferred item to the user when its deferral's named condition is met (no source change needed)."""
    from hermes_initiatives_core.review_settle import reopen_deferral

    store = ReviewStore()
    config, _ = load_settings()
    item = store.get(args.short_id)
    project = config.project(item["project"]) if item and item.get("project") else None
    print(json.dumps({"ok": True, **reopen_deferral(store, args.short_id, args.reason, project)}))


def _summarize(args: argparse.Namespace) -> None:
    """Add a what/why blocker summary to open decisions whose current options have none (no new version)."""
    from hermes_initiatives_core.reviews import ReviewError

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    store = ReviewStore()
    results, errors = {}, []
    for short_id, summary in (payload.get("summaries") or {}).items():
        try:
            results[str(short_id).upper()] = store.set_summary(short_id, summary)
        except ReviewError as exc:
            errors.append(f"{short_id}: {exc}")
    print(json.dumps({"ok": not errors, "summarised": sorted(results), "errors": errors}))
    if errors:
        sys.exit(1)


def _learn(args: argparse.Namespace) -> None:
    """Mine the user's answers for rule candidates; with --propose, also file one review item per new candidate."""
    from hermes_initiatives_core import review_learn

    store = ReviewStore()
    candidates = review_learn.mine(store)
    config, _ = load_settings()
    filed = review_learn.propose(store, config.projects) if args.propose else []
    rules = {rule: state for rule, state in store.rule_states().items()}
    print(
        json.dumps(
            {
                "ok": True,
                "candidates": [
                    {"signature": c["signature"], "evidence": [r["short_id"] for r in c["evidence"]]} for c in candidates
                ],
                "filed": filed,
                "rule_states": rules,
            },
            indent=1,
        )
    )


def _overrule(args: argparse.Namespace) -> None:
    """The user says an agent settlement was wrong: suspend its rule and file the follow-up decision."""
    from hermes_initiatives_core import review_learn

    config, _ = load_settings()
    try:
        done = review_learn.overrule(ReviewStore(), config.projects, args.short_id, args.reason)
    except ReviewError as exc:
        fail(str(exc))
    print(json.dumps({"ok": True, **done}))


def _complete_promotions(args: argparse.Namespace) -> None:
    """Finish idea promotions applied without a destination, from the user's recorded answers."""
    from hermes_initiatives_core.review_exec import ActionFailed, Executor

    config, _ = load_settings()
    executor = Executor(ReviewStore())
    out = []
    for project in config.projects:
        try:
            out += executor.complete_promotions(project, assignee=args.assignee, dry_run=not args.apply)
        except ActionFailed as exc:
            fail(f"{project.id}: {exc}")
    print(json.dumps({"ok": True, "applied": args.apply, "promotions": out}, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--trigger", choices=("cron", "manual"), default="cron")
    collect.add_argument("--gates", help="the human gate list (gates.json); without it Kanban is not read")
    collect.set_defaults(run=_collect)
    tick = sub.add_parser("tick", help="per-tick sync; print the items that need options")
    tick.add_argument("--gates", required=True, help="the human gate list (gates.json)")
    tick.set_defaults(run=_tick)
    notify_cmd = sub.add_parser("notify", help="push each new decision once (inside the configured push window)")
    notify_cmd.add_argument("--target", required=True)
    notify_cmd.add_argument("--baseline", action="store_true", help="mark current decisions pushed; send nothing")
    notify_cmd.set_defaults(run=_notify)
    daily = sub.add_parser("daily", help="today's digest: --due checks, otherwise start today's review")
    daily.add_argument("--due", action="store_true", help="exit 0 when the digest is due, 1 when not")
    daily.add_argument("--gates", help="the human gate list (gates.json)")
    daily.add_argument("--new", action="store_true", help="start a new review even if one exists today")
    daily.set_defaults(run=_daily)
    instructions = sub.add_parser("instructions", help="print the generator prompt (cron pre-run script)")
    instructions.add_argument("--target", required=True, help="digest target, e.g. telegram:<chat>:<topic>")
    instructions.add_argument("--trigger", choices=("cron", "manual"), default="cron")
    instructions.add_argument("--tick", action="store_true", help="print the review tick's step instead")
    instructions.add_argument("--gates", help="gate list path for --tick (default: <data>/review-work/gates.json)")
    instructions.set_defaults(run=_instructions)
    write = sub.add_parser("write")
    write.add_argument("--file", required=True, help="generator output JSON, or - for stdin")
    write.set_defaults(run=_write)
    settle = sub.add_parser("settle", help="close items under a rule the user delegated (see review_tick.md)")
    settle.add_argument("--file", required=True, help='{"settle": [{"short_id", "rule", "args", "why"}]}, or - for stdin')
    settle.set_defaults(run=_settle)
    auto = sub.add_parser("auto", help="handle items the user marked auto (see review_tick.md)")
    auto.add_argument("--file", required=True, help='{"auto": [{"short_id", "do", "option"?, "why"}]}, or - for stdin')
    auto.set_defaults(run=_auto)
    sweep = sub.add_parser("sweep", help="keep, refresh, resolve, or apply items idle for 3 days (see review_tick.md)")
    sweep.add_argument("--file", required=True, help='{"sweep": [{"short_id", "do", "why", "option"?, "risk"?}]}, or -')
    sweep.set_defaults(run=_sweep)
    rules = sub.add_parser("rules", help="all delegated rules with their state, or open items learned rules cover")
    rules.add_argument("--matches", action="store_true", help="open items a learned rule covers now")
    rules.set_defaults(run=_rules)
    reopen = sub.add_parser("reopen", help="return a deferred item whose named condition is met")
    reopen.add_argument("short_id")
    reopen.add_argument("--reason", required=True, help="the deferral's condition and the evidence that it is met")
    reopen.set_defaults(run=_reopen)
    summarize = sub.add_parser("summarize", help="add what/why blocker summaries to open decisions that lack one")
    summarize.add_argument("--file", required=True, help='{"summaries": {"D7": {"what": "...", "why": "..."}}}')
    summarize.set_defaults(run=_summarize)
    learn = sub.add_parser("learn", help="mine answers for delegated-rule candidates (--propose files them)")
    learn.add_argument("--propose", action="store_true", help="file one review item per new candidate")
    learn.set_defaults(run=_learn)
    overrule = sub.add_parser("overrule", help="the user overrules an agent settlement: suspend its rule")
    overrule.add_argument("short_id")
    overrule.add_argument("--reason", required=True)
    overrule.set_defaults(run=_overrule)
    digest = sub.add_parser("digest")
    digest.add_argument("--review")
    digest.set_defaults(run=_digest)
    send = sub.add_parser("send")
    send.add_argument("--review")
    send.add_argument("--target", required=True)
    send.set_defaults(run=_send)
    sub.add_parser("status").set_defaults(run=_status)
    answer = sub.add_parser("answer", help="apply review commands, e.g. 'D3 A: note' or 'confirm D3'")
    answer.add_argument("text", nargs="?", default="-", help="commands, or - for stdin")
    answer.set_defaults(run=_answer)
    complete = sub.add_parser("complete-promotions", help="finish idea promotions applied without promoted_to")
    complete.add_argument("--assignee", help="profile for the delivery cards (default: the project's intake_assignee)")
    complete.add_argument("--apply", action="store_true", help="create the cards and write promoted_to (default: dry run)")
    complete.set_defaults(run=_complete_promotions)
    args = parser.parse_args()
    try:
        args.run(args)
    except ReviewError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
