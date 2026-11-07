"""Markdown-with-frontmatter and JSONL reading, a stat-keyed parse cache, and atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    _LOADER = yaml.CSafeLoader
    _DUMPER = yaml.CSafeDumper
except AttributeError:  # pragma: no cover - libyaml missing; correct, only slower
    _LOADER = yaml.SafeLoader
    _DUMPER = yaml.SafeDumper


class DocumentError(ValueError):
    """A file that cannot be parsed into frontmatter."""


@dataclass(frozen=True)
class Document:
    path: Path
    frontmatter: dict[str, Any]
    body: str


def parse_markdown(text: str, path: Path) -> Document:
    if not text.startswith("---\n"):
        raise DocumentError("missing YAML frontmatter (the file must start with '---')")
    end = text.find("\n---", 4)
    if end < 0:
        raise DocumentError("unterminated YAML frontmatter")
    try:
        data = yaml.load(text[4:end], Loader=_LOADER)  # noqa: S506 - safe loader
    except yaml.YAMLError as exc:
        raise DocumentError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DocumentError("frontmatter must be a mapping")
    body_start = text.find("\n", end + 4)
    return Document(path=path, frontmatter=data, body=text[body_start + 1 :] if body_start >= 0 else "")


def parse_jsonl(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {number}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {number}: not a JSON object")
            continue
        rows.append(row)
    return rows, errors


def dump_markdown(frontmatter: dict[str, Any], body: str) -> str:
    head = yaml.dump(frontmatter, Dumper=_DUMPER, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{head}---\n{body}"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ParseCache:
    """Re-parse a file only when its size or mtime changes; the change check is one ``stat``."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[tuple[int, int], Any]] = {}
        self._lock = threading.Lock()
        self.parses = 0

    def get(self, path: Path, parse):
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
        name = str(path)
        with self._lock:
            hit = self._entries.get(name)
            if hit and hit[0] == key:
                return hit[1]
        try:
            value: Any = parse(path.read_text(encoding="utf-8"), path)
        except (DocumentError, UnicodeDecodeError) as exc:
            value = exc
        with self._lock:
            self.parses += 1
            self._entries[name] = (key, value)
        return value

    def prune(self, live: set[str]) -> None:
        with self._lock:
            for name in [n for n in self._entries if n not in live]:
                del self._entries[name]
