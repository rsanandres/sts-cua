"""Reading run logs and other files for the control app, without reading big files whole."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[\w.-]+$")


def safe_name(name: str) -> bool:
    """A single path component of word characters, dots and dashes (not '.' or '..')."""
    return bool(NAME_RE.match(name)) and name not in (".", "..") and len(name) <= 128


def inside(base: Path, *parts: str) -> Path | None:
    """base/parts resolved, or None if any part is unsafe or the result leaves base."""
    if not all(safe_name(p) for p in parts):
        return None
    root = base.resolve()
    path = root.joinpath(*parts).resolve()
    return path if path.is_relative_to(root) and path != root else None


def tail_lines(path: Path, n: int, block: int = 65536) -> list[str]:
    """The last n lines of a file, read backwards from the end in blocks."""
    if n <= 0:
        return []
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        end = f.seek(0, os.SEEK_END)
        data = b""
        pos = end
        while pos > 0 and data.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.decode("utf-8", "replace").splitlines()
    if pos > 0:
        lines = lines[1:]  # the first line may be cut off
    return lines[-n:]


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def last_record(path: Path) -> dict | None:
    """The last complete JSON line of a .jsonl file."""
    for line in reversed(tail_lines(path, 2)):
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if isinstance(record, dict):
            return record
    return None


def run_dirs(runs: Path) -> list[Path]:
    """Run directories (with a meta.json), newest first. RunLog names them by UTC start time."""
    if not runs.is_dir():
        return []
    dirs = [p for p in runs.iterdir() if p.is_dir() and safe_name(p.name) and (p / "meta.json").exists()]
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def without_raw(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "raw"}


def read_steps(path: Path, offset: int, limit: int) -> tuple[int, list[dict]]:
    """(total lines, records offset..offset+limit without 'raw'), parsing only the lines returned."""
    total, out = 0, []
    try:
        f = open(path, "rb")
    except OSError:
        return 0, []
    with f:
        for line in f:
            if not line.strip():
                continue
            if offset <= total < offset + limit:
                try:
                    out.append(without_raw(json.loads(line)))
                except ValueError:
                    pass
            total += 1
    return total, out


@dataclass
class CostTracker:
    """Running cost of a live steps.jsonl, reading only the bytes added since the last call."""

    _seen: dict[str, tuple[int, float]] = field(default_factory=dict)  # path -> (offset, cost)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cost(self, path: Path) -> float:
        key = str(path)
        with self._lock:
            offset, total = self._seen.get(key, (0, 0.0))
            try:
                with open(path, "rb") as f:
                    if f.seek(0, os.SEEK_END) < offset:
                        offset, total = 0, 0.0  # file replaced
                    f.seek(offset)
                    for line in f:
                        if not line.endswith(b"\n"):
                            break  # partly written; read it next time
                        offset += len(line)
                        try:
                            usage = json.loads(line).get("usage") or {}
                            total += float(usage.get("cost_usd") or 0.0)
                        except (ValueError, AttributeError, TypeError):
                            pass
            except OSError:
                return total
            self._seen[key] = (offset, total)
            return total
