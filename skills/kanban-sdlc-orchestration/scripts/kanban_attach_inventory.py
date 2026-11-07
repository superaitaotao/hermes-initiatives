#!/usr/bin/env python3
"""Attach an inventory of evidence files to a Kanban card, byte-verified.

Operator fallback transport for the case where a worker's own attachment
surface is refused (no ``path=`` parameter, or the CLI refuses a delegated
child). The operator stores the frozen originals instead of rebuilding them.

Identity is stored size + SHA-256, never the filename: the CLI suffixes a name
that already exists on the card (``<name> (1).json``), so a lookup by source
basename misses an attachment that is already stored and a naive retry
re-uploads the same bytes under another suffixed name. A fresh upload takes
its id from the id-set difference around the ``attach`` call.

Usage:
  kanban_attach_inventory.py --board project-board --task t_abc123 \
      --inventory .verification/evidence-manifest.json --author operator

Options:
  --receipt PATH     additive receipt (default: <inventory>-attach-receipt.json)
  --expect-status S  refuse unless the card is in status S (e.g. blocked)
  --no-lock          do not hold the board dispatch lock while attaching
  --dry-run          verify sources and report the plan; attach nothing

Inventory shape (either key holds the rows):
  {"items" | "files": [{"path": "<file>",
                        "filename": "<optional stored name>",
                        "bytes" | "size": 1234,
                        "sha256": "<64 hex>"}]}

Sources are verified against the inventory before upload and each stored
attachment is verified against its source afterwards. Exits 0 only when every
row is stored and verified; on failure it stops, leaving earlier attachments
and the card untouched. Re-running is cheap and idempotent: already-stored
rows are matched by bytes and reused.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HERMES = "hermes"
CLI_TIMEOUT = 120


def cli(*args: str) -> str:
    result = subprocess.run([HERMES, *args], capture_output=True, text=True,
                            timeout=CLI_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(
            f"hermes {' '.join(args[:4])} failed: {(result.stderr or result.stdout)[:400]}")
    return result.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_rows(inventory: Path) -> list[dict]:
    data = json.loads(inventory.read_text(encoding="utf-8"))
    rows = data.get("items") or data.get("files")
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{inventory}: no non-empty 'items' or 'files' rows")
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("path"):
            raise SystemExit(f"{inventory}: row without a 'path': {row!r}")
        source = Path(row["path"])
        if not source.is_file():
            raise SystemExit(f"missing source file: {source}")
        actual_size = source.stat().st_size
        actual_digest = sha256_file(source)
        declared_digest = row.get("sha256") or row.get("sha")
        declared_size = row.get("bytes") or row.get("size")
        if declared_digest and declared_digest != actual_digest:
            raise SystemExit(
                f"source digest mismatch for {source}: {actual_digest} != {declared_digest}")
        if declared_size and int(declared_size) != actual_size:
            raise SystemExit(
                f"source size mismatch for {source}: {actual_size} != {declared_size}")
        normalized.append({
            "source": source,
            "source_path": str(source),
            "name": row.get("filename") or source.name,
            "bytes": actual_size,
            "sha256": actual_digest,
        })
    return normalized


def attachments(board: str, task: str) -> list[dict]:
    return json.loads(cli("kanban", "--board", board, "attachments", task, "--json"),
                     strict=False)


def card_status(board: str, task: str) -> str:
    card = json.loads(cli("kanban", "--board", board, "show", task, "--json"),
                      strict=False)
    return card["task"]["status"]


def stored_match(rows: list[dict], row: dict) -> list[dict]:
    """Existing attachments whose stored bytes equal this source, by size + SHA-256."""
    matched = []
    for candidate in rows:
        stored = Path(candidate.get("stored_path", ""))
        if not stored.is_file():
            continue
        if stored.stat().st_size != row["bytes"]:
            continue
        if sha256_file(stored) == row["sha256"]:
            matched.append(candidate)
    return matched


@contextlib.contextmanager
def board_lock(board: str):
    """Best-effort reuse of the route script's board dispatch lock.

    Falls open: a missing sibling script must not block authorized transport.
    """
    try:
        sibling = Path(__file__).with_name("kanban_triage_route.py")
        spec = importlib.util.spec_from_file_location("kanban_triage_route", sibling)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with module.dispatch_lock(module.board_db(board)):
            yield
    except Exception as exc:  # noqa: BLE001 - transport must not depend on the lock
        print(f"note: dispatch lock unavailable ({exc}); continuing unlocked",
              file=sys.stderr)
        yield


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--author", default="operator")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--expect-status")
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.inventory)
    receipt_path = args.receipt or args.inventory.with_name(
        f"{args.inventory.stem}-attach-receipt.json")

    if args.expect_status:
        status = card_status(args.board, args.task)
        if status != args.expect_status:
            print(f"REFUSED {args.task}: status {status!r}, expected "
                  f"{args.expect_status!r}", file=sys.stderr)
            return 1

    receipt = {
        "schema_version": 1,
        "task_id": args.task,
        "inventory": str(args.inventory),
        "inventory_sha256": sha256_file(args.inventory),
        "actor": args.author,
        "phase": "native_transport",
        "status": "partial",
        "files": [],
    }
    if receipt_path.is_file():
        try:
            previous = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["files"] = [r for r in previous.get("files", [])
                                if isinstance(r, dict) and r.get("source_path")]
        except (OSError, ValueError):
            pass

    lock = contextlib.nullcontext() if args.no_lock else board_lock(args.board)
    with lock:
        current = attachments(args.board, args.task)
        for row in rows:
            matched = stored_match(current, row)
            if matched:
                stored = matched[0]
                print(f"reuse  {stored['id']} {stored['filename']} "
                      f"{row['bytes']} (source {row['name']})")
            elif args.dry_run:
                print(f"would attach {row['source']} as {row['name']} "
                      f"{row['bytes']} {row['sha256']}")
                continue
            else:
                before = {item["id"] for item in current}
                cli("kanban", "--board", args.board, "attach", args.task,
                    str(row["source"]), "--name", row["name"], "--author", args.author)
                current = attachments(args.board, args.task)
                fresh = [item for item in current if item["id"] not in before]
                if len(fresh) != 1:
                    raise SystemExit(
                        f"attach of {row['source']} produced {len(fresh)} new attachments")
                stored = fresh[0]
                stored_path = Path(stored.get("stored_path", ""))
                if not stored_path.is_file():
                    raise SystemExit(f"stored file missing for {row['name']}")
                if stored_path.stat().st_size != row["bytes"] or \
                        sha256_file(stored_path) != row["sha256"]:
                    raise SystemExit(
                        f"stored bytes differ from source for {row['name']}: "
                        f"{stored_path.stat().st_size} {sha256_file(stored_path)}")
                print(f"attach {stored['id']} {stored['filename']} "
                      f"{row['bytes']} (source {row['name']})")

            receipt["files"] = [r for r in receipt["files"]
                                if r.get("source_path") != row["source_path"]]
            receipt["files"].append({
                "source_path": row["source_path"],
                "filename": row["name"],
                "stored_filename": stored.get("filename"),
                "attachment_id": stored["id"],
                "bytes": row["bytes"],
                "sha256": row["sha256"],
            })
            receipt["count"] = len(receipt["files"])
            receipt["updated_at"] = datetime.now(UTC).isoformat()
            if not args.dry_run:
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n",
                                        encoding="utf-8")

        if args.dry_run:
            print(f"dry run: {len(rows)} rows verified, nothing attached")
            return 0

        receipt["status"] = "verified" if len(receipt["files"]) == len(rows) else "partial"
        receipt["count"] = len(receipt["files"])
        receipt["updated_at"] = datetime.now(UTC).isoformat()
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

        current = attachments(args.board, args.task)
        if not stored_match(current, {"bytes": receipt_path.stat().st_size,
                                      "sha256": sha256_file(receipt_path)}):
            cli("kanban", "--board", args.board, "attach", args.task, str(receipt_path),
                "--name", receipt_path.name, "--author", args.author)

    print(f"{receipt['status']}: {receipt['count']}/{len(rows)} rows stored and verified")
    print(f"receipt: {receipt_path}")
    return 0 if receipt["count"] == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
