"""Render a stored review as Telegram-sized plain-text messages, one section per initiative."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .reviews import ReviewStore, current
from .settings import ProjectSettings
from .store import load

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3800  # room for the adapter's "(n/m)" marker and HTML escaping
REPLY_HELP = (
    "Reply here, one line per answer:\n"
    "  D3 A    or    D3 B: free text\n"
    "  confirm D3    (irreversible options)\n"
    "defer D3 until 2026-10-20  |  defer D3 until change  |  defer D3: reason\n"
    "auto D3    or    auto D3: what the agents should do   (agents handle it)  |  manual D3   (take it back)\n"
    "Send only answer lines in one message; any other text goes to the agent. Answers apply at once; first answer wins."
)


def _age_days(item: dict[str, Any], now: dt.datetime) -> int:
    try:
        opened = dt.datetime.fromisoformat(str(item["opened_at"]).replace("Z", "+00:00"))
    except ValueError:
        return 0
    opened = opened if opened.tzinfo else opened.replace(tzinfo=dt.UTC)
    return max(0, (now - opened).days)


def _deferral_text(item: dict[str, Any]) -> str:
    deferral = item.get("deferral") or {}
    return f"{deferral.get('kind') or ''} {deferral.get('until') or ''}".strip()


def _item_block(item: dict[str, Any], now: dt.datetime) -> str:
    version = item["version"] or {}
    meta = [f"{_age_days(item, now)}d open"]
    if item.get("refreshed"):
        meta.insert(0, "refreshed: rewritten after 3 idle days")
    if item["blocks"]:
        meta.append(f"blocks {len(item['blocks'])}")
    if item["urgent_rule"]:
        meta.insert(0, f"URGENT {item['urgent_rule']}")
    lines = [f"{item['short_id']} — {version.get('question') or item['title']}", f"  ({', '.join(meta)}; {item['title']})"]
    summary = version.get("summary") or {}
    if summary:
        lines += [f"  What: {summary['what']}", f"  Why: {summary['why']}"]
    if version.get("context"):
        lines.append("  " + version["context"].strip().replace("\n", "\n  "))
    for option in version.get("options") or []:
        tags = []
        if option["id"] == version.get("recommend"):
            tags.append("recommended")
        if option.get("confirm"):
            tags.append("needs confirm")
        suffix = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"  {option['id']}{suffix}: {option['label']}")
    return "\n".join(lines)


def push_message(item: dict[str, Any], titles: dict[str, str]) -> str:
    """One new decision as its own Telegram message: initiative, question, why, options, how to answer."""
    version = item["version"] or {}
    initiative = item.get("initiative") or ""
    where = titles.get(initiative) or initiative or "No initiative"
    lines = [f"Decision {item['short_id']} — {where}", version.get("question") or item["title"], f"({item['title']})"]
    summary = version.get("summary") or {}
    if summary:
        lines += [f"What: {summary['what']}", f"Why: {summary['why']}"]
    if version.get("context"):
        lines.append(version["context"].strip())
    for option in version.get("options") or []:
        tags = [
            t
            for t, on in (("recommended", option["id"] == version.get("recommend")), ("needs confirm", option.get("confirm")))
            if on
        ]
        lines.append(f"{option['id']}{' (' + ', '.join(tags) + ')' if tags else ''}: {option['label']}")
    sid = item["short_id"]
    lines.append(
        f"Reply: {sid} {version.get('recommend') or 'A'}   or   {sid} B: note   |   defer {sid} until change   |   auto {sid}"
    )
    return "\n".join(lines)


def _button_label(option: dict[str, Any], recommended: Any) -> str:
    """One button: the option id a typed reply uses, the start of its label, and its tags."""
    text = " ".join(str(option.get("label") or "").split())
    if len(text) > 34:
        text = text[:33].rstrip() + "…"
    tags = (" ★" if option["id"] == recommended else "") + (" ⚠" if option.get("confirm") else "")
    return f"{option['id']} · {text}{tags}"[:60]


def push_keyboard(item: dict[str, Any]) -> dict[str, Any] | None:
    """Inline keyboard for one pushed decision: a button per option, then Defer and Agents handle it.

    Each button carries the same option id a typed reply accepts (``D3 A``), so a tap runs no new answer path.
    ``None`` when the item has no options yet: that push goes out as plain text.
    """
    from .review_buttons import AUTO, DEFER, callback_data

    version = item.get("version") or {}
    options = version.get("options") or []
    if not options:
        return None
    short_id = item["short_id"]
    rows = [
        [{"text": _button_label(o, version.get("recommend")), "callback_data": callback_data(short_id, o["id"])}] for o in options
    ]
    rows.append(
        [
            {"text": "Defer", "callback_data": callback_data(short_id, DEFER)},
            {"text": "Agents handle it", "callback_data": callback_data(short_id, AUTO)},
        ]
    )
    return {"inline_keyboard": rows}


NO_INITIATIVE = ""


def initiative_titles(projects: list[ProjectSettings]) -> dict[str, str]:
    """``{initiative id: title}`` from the initiative files of every configured project (unreadable roots skipped)."""
    titles: dict[str, str] = {}
    for project in projects:
        try:
            snapshot = load(project.root)
        except OSError:
            continue
        for initiative_id, doc in snapshot.initiatives.items():
            title = str(doc.frontmatter.get("title") or "").strip()
            if title:
                titles.setdefault(initiative_id, title)
    return titles


UNCHANGED = "No change since the last review."


def groups(store: ReviewStore, review_id: str, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """The review split by initiative: each group has its decisions (ranked), summary, update, watching, deferred.

    Groups with decisions come first, ordered by their highest-ranked decision; then groups with only an
    update; quiet initiatives (no decision, no change) last. Items with no initiative form one group, ``""``.
    """
    now = now or dt.datetime.now(dt.UTC)
    review = store.review(review_id) or {}
    text = review.get("summaries") or {}
    summaries, updates = text.get("summaries") or {}, text.get("updates") or {}
    unchanged = set((review.get("facts") or {}).get("unchanged") or [])
    by: dict[str, dict[str, Any]] = {}

    def group(initiative: str | None) -> dict[str, Any]:
        key = initiative or NO_INITIATIVE
        return by.setdefault(
            key,
            {
                "initiative": key,
                "decisions": [],
                "watching": [],
                "deferred": [],
                "auto": [],
                "summary": "",
                "update": "",
                "rank": None,
            },
        )

    for rank, item in enumerate(i for i in store.queue(now=now) if current(i)):
        entry = group(item["initiative"])
        entry["decisions"].append(item)
        entry["rank"] = rank if entry["rank"] is None else entry["rank"]
    for item in store.items(statuses=("watching",)):
        group(item["initiative"])["watching"].append(item)
    for item in store.items(statuses=("deferred",)):
        group(item["initiative"])["deferred"].append(item)
    for item in store.items(statuses=("auto",)):
        group(item["initiative"])["auto"].append(item)
    for initiative in set(summaries) | set(updates) | unchanged:
        entry = group(initiative)
        entry["summary"] = str(summaries.get(initiative) or "")
        entry["update"] = str(updates.get(initiative) or (UNCHANGED if initiative in unchanged else ""))

    def order(entry: dict[str, Any]) -> tuple:
        quiet = not entry["decisions"] and entry["update"] in ("", UNCHANGED)
        return (entry["rank"] is None, quiet, entry["rank"] if entry["rank"] is not None else 0, entry["initiative"] or "~")

    return sorted(by.values(), key=order)


def _group_title(entry: dict[str, Any]) -> str:
    count = len(entry["decisions"])
    name = entry["initiative"] or "No initiative"
    return f"{name} — {count} decision(s)" if count else name


def _group_blocks(entry: dict[str, Any], now: dt.datetime) -> list[str]:
    blocks: list[str] = []
    head = [entry["summary"]] if entry["summary"] else []
    if entry["update"]:
        head.append(f"Since last review: {entry['update']}")
    if head:
        blocks.append("\n".join(head))
    blocks.extend(_item_block(i, now) for i in entry["decisions"])
    quiet = [f"  Watching {i['short_id']} — {i['title']}: {i.get('watch_reason') or ''}" for i in entry["watching"]]
    quiet += [f"  Deferred {i['short_id']} — {i['title']} ({_deferral_text(i)})" for i in entry["deferred"]]
    quiet += [f"  Auto {i['short_id']} — {i['title']}: agents handle it at the next tick" for i in entry.get("auto") or []]
    if quiet:
        blocks.append("\n".join(quiet))
    return blocks or [UNCHANGED]


def _waiting(store: ReviewStore) -> list[dict[str, Any]]:
    from .review_attention import collect
    from .settings import load_settings

    return collect(store, load_settings()[0].projects)["items"]  # an unreadable board is skipped, not raised


def _waiting_block(entry: dict[str, Any]) -> str:
    item = entry.get("item") or {}
    answer = item.get("answer") or {}
    lines = [f"{entry['card']} {entry['title']}", f"You: {entry['you']}"]
    meta = [entry["status"]]
    if item:
        meta.append(f"{item['short_id']} {item['status']}")
    if answer:
        meta.append(f"you chose {answer['option']} on {str(answer.get('answered_at') or '')[:10]}")
    lines.append(" · ".join(meta) + " · start a session from the Review tab")
    return "\n".join(lines)


def sections(store: ReviewStore, review_id: str, now: dt.datetime | None = None) -> list[tuple[str, list[str]]]:
    now = now or dt.datetime.now(dt.UTC)
    review = store.review(review_id) or {}
    out: list[tuple[str, list[str]]] = []
    queue = [i for i in store.queue(now=now) if current(i)]
    pending = [i for i in store.queue(now=now) if not i["version"]]
    entries = groups(store, review_id, now=now)
    header = f"Daily review {review.get('generated_at', '')[:10]} — {len(queue)} decision(s)"
    if review.get("window_start"):
        header += f", updates since {review['window_start'][:16].replace('T', ' ')} UTC"
    busy = [e for e in entries if e["decisions"]]
    index = [f"{e['initiative'] or 'No initiative'}: {len(e['decisions'])}" for e in busy]
    waiting = _waiting(store)
    nothing = "No decision needs you today." if not waiting else "No decision needs you today; see Waiting on you."
    out.append((header, ["\n".join(index) if index else nothing]))
    if waiting:
        out.append((f"Waiting on you — {len(waiting)} card(s) need a step from you", [_waiting_block(w) for w in waiting]))
    for entry in entries:
        out.append((_group_title(entry), _group_blocks(entry, now)))
    if pending:
        out.append(("Not yet written up (generator gap)", [f"{i['short_id']} — {i['title']}" for i in pending]))
    suggested = [i for i in store.queue(now=now, section="suggested") if i["version"]]
    if suggested:
        out.append(("Suggested (optional)", [_item_block(i, now) for i in suggested]))
    facts = review.get("facts") or {}
    results = facts.get("results") or []
    if results or facts.get("urgent_pushes"):
        lines = [f"Urgent pushes since the last review: {facts.get('urgent_pushes', 0)}"] + [
            f"{r['short_id']}: {r['status']} — {r['note']}" for r in results
        ]
        out.append(("Results since the last review", lines))
    gaps = review.get("gaps") or []
    if gaps:
        out.append(
            (
                "Coverage gaps (asks with no review item; shadow week)",
                [f"{g.get('source')}: {g.get('ref') or '-'} — {g.get('line') or ''}" for g in gaps],
            )
        )
    out.append(("", [REPLY_HELP]))
    return out


def _split_long(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    parts, current = [], ""
    for line in block.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def render(store: ReviewStore, review_id: str, *, limit: int = SAFE_LIMIT, now: dt.datetime | None = None) -> list[str]:
    """Split on block boundaries so one decision never spans two messages (unless it alone exceeds *limit*)."""
    messages: list[str] = []
    current = ""
    for title, blocks in sections(store, review_id, now=now):
        heading = f"== {title} ==" if title else ""
        for index, block in enumerate(blocks):
            piece = f"{heading}\n{block}" if heading and index == 0 else block
            for chunk in _split_long(piece, limit):
                candidate = f"{current}\n\n{chunk}" if current else chunk
                if len(candidate) > limit:
                    messages.append(current)
                    current = chunk
                else:
                    current = candidate
    if current:
        messages.append(current)
    return messages
