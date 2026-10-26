"""Result directories (runs/bench/<timestamp>/results.json + results.md) and the small stats the bake-off reports."""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

RESULTS_FILE = "results.json"
MARKDOWN_FILE = "results.md"


def bench_runs(runs_dir: str | Path = "runs") -> Path:
    return Path(runs_dir) / "bench"


def new_dir(runs_dir: str | Path = "runs", now: datetime | None = None) -> Path:
    base = bench_runs(runs_dir)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    out = base / stamp
    suffix = 1
    while out.exists():
        out = base / f"{stamp}-{suffix}"
        suffix += 1
    out.mkdir(parents=True)
    return out


def write(out_dir: Path, result: dict, markdown: str) -> Path:
    (out_dir / RESULTS_FILE).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out_dir / MARKDOWN_FILE).write_text(markdown, encoding="utf-8")
    return out_dir / RESULTS_FILE


def load_all(runs_dir: str | Path = "runs") -> list[dict]:
    """Every bench result, oldest first, each with `dir` set to its directory."""
    out = []
    for path in sorted(bench_runs(runs_dir).glob(f"*/{RESULTS_FILE}")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result["dir"] = str(path.parent)
        out.append(result)
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, q in 0-100."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    rank = max(1, math.ceil(q / 100 * len(values)))
    return values[rank - 1]


def fmt(value, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"
