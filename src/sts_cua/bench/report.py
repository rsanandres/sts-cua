"""The newest bench results per model, a recommended ranking, and the docs/hosting.md writer.

Ranking: measured grounding hit rate first, then median latency (lower is better). A model
fits a mode when its measured VRAM (used minus the pre-server baseline) is within the mode's
budget in models/registry.py; without a measurement the registry's estimate is used and
marked "est.". The recommendation is the best-ranked local model that fits each mode.

--write-docs touches two places in docs/hosting.md and nothing else:
- everything between `<!-- bench:start -->` and `<!-- bench:end -->`; when the markers are
  missing, a "## Measured on the PC" section with them is appended at the end;
- the rows in MEASURE_ROWS, inside the "## VRAM budget" and "## RAM budget" sections, and
  only when the row still says **measure** (or carries the row's `<!-- bench:<id> -->` tag
  from an earlier write) and a measured value exists for it. Other rows keep their text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from ..models import registry
from . import results as R

START = "<!-- bench:start -->"
END = "<!-- bench:end -->"
SECTION = "## Measured on the PC"
_MEASURE = re.compile(r"\*\*measure\*\*", re.IGNORECASE)


def latest(results: list[dict]) -> dict:
    """{"grounding": {model: entry}, "clicks": {model: entry}, "resources": {(model, mode): entry}}, newest wins."""
    out: dict = {"grounding": {}, "clicks": {}, "resources": {}}
    for r in results:  # oldest first
        kind = r.get("kind")
        if kind == "grounding":
            for m in r.get("models") or []:
                if (m.get("summary") or {}).get("answered"):
                    out["grounding"][m["model"]] = {**m, "created_at": r.get("created_at"),
                                                    "game_build": r.get("game_build"), "targets": r.get("targets"),
                                                    "runs": r.get("runs"), "dir": r.get("dir")}
        elif kind == "clicks":
            out["clicks"][r["model"]] = r
        elif kind == "resources":
            out["resources"][(r["model"], r["mode"])] = r
    return out


def fit(model: str, mode: str, resources: dict) -> tuple[bool | None, float | None, str]:
    """(fits, GiB needed, "measured" / "est." / "api") for one model and mode."""
    r = resources.get((model, mode))
    if r and r.get("model_vram_gib") is not None:
        return r["model_vram_gib"] <= registry.budget(mode), r["model_vram_gib"], "measured"
    local = registry.MODELS.get(model)
    if local:
        return local.fits(registry.budget(mode)), round(local.need_gib, 1), "est."
    return None, None, "api"


def ranking(data: dict) -> list[dict]:
    rows = []
    for model, entry in data["grounding"].items():
        s = entry["summary"]
        if s.get("hit_rate") is None:
            continue
        rows.append({"model": model, "hit_rate": s["hit_rate"], "latency_p50_s": s.get("latency_p50_s"),
                     "local": model in registry.MODELS,
                     "fits": {mode: fit(model, mode, data["resources"]) for mode in ("coop", "solo")}})
    rows.sort(key=lambda r: (-r["hit_rate"], r["latency_p50_s"] if r["latency_p50_s"] is not None else float("inf")))
    return rows


def recommended(ranked: list[dict]) -> dict[str, str | None]:
    return {mode: next((r["model"] for r in ranked if r["local"] and r["fits"][mode][0]), None)
            for mode in ("coop", "solo")}


def _fit_text(f: tuple[bool | None, float | None, str]) -> str:
    fits, need, how = f
    if how == "api":
        return "API"
    return f"{'yes' if fits else 'no'} ({how} {need:.1f} GiB)"


def ranking_markdown(ranked: list[dict]) -> str:
    if not ranked:
        return "No grounding results yet."
    lines = ["Ranking (hit rate first, then median latency):", ""]
    for n, r in enumerate(ranked, 1):
        lines.append(f"{n}. **{r['model']}**: {R.pct(r['hit_rate'])} hits, {R.fmt(r['latency_p50_s'], 2)} s median; "
                     f"co-op {_fit_text(r['fits']['coop'])}, solo {_fit_text(r['fits']['solo'])}")
    rec = recommended(ranked)
    def name(model: str | None) -> str:
        return f"**{model}**" if model else "none that fits"

    lines += ["", f"Recommended: co-op {name(rec['coop'])}, solo {name(rec['solo'])}."]
    return "\n".join(lines)


def block(data: dict, today: str | None = None) -> str:
    """The markdown between the bench markers."""
    today = today or date.today().isoformat()
    g = data["grounding"]
    lines = [f"Written by `python -m sts_cua.bench report --write-docs` on {today}; the next report rewrites it. "
             "Raw results are in `runs/bench/` on the PC.", ""]
    if g:
        builds = sorted({e.get("game_build") or "unknown" for e in g.values()})
        sets = sorted({(e.get("targets"), e.get("runs")) for e in g.values()}, key=str)
        set_text = "; ".join(f"{t} targets from {n} Claude runs" for t, n in sets)
        lines += [f"Grounding (`bench grounding`): {set_text}, game build {', '.join(builds)}. "
                  "A hit is a click within the target's radius (40 px at 1280x720).", "",
                  "| Model | Measured | Hit rate | Median error (px) | Latency p50 / p95 (s) | Tokens/s | Malformed |",
                  "|---|---|---|---|---|---|---|"]
        for model, e in sorted(g.items()):
            s = e["summary"]
            lines.append(f"| {model} | {(e.get('created_at') or '')[:10]} | {R.pct(s['hit_rate'])} "
                         f"({s['hits']}/{s['answered']}) | {R.fmt(s['median_error_px'])} | "
                         f"{R.fmt(s['latency_p50_s'], 2)} / {R.fmt(s['latency_p95_s'], 2)} | "
                         f"{R.fmt(s.get('tokens_per_s'))} | {s['malformed']} |")
        lines.append("")
    else:
        lines += ["No grounding results yet.", ""]
    if data["clicks"]:
        lines += ["Live clicks on `tools/click_target.py` (`bench clicks`):", "",
                  "| Model | Measured | Hits | Latency p50 / p95 (s) | Malformed |", "|---|---|---|---|---|"]
        for model, c in sorted(data["clicks"].items()):
            s = c["summary"]
            lines.append(f"| {model} | {(c.get('created_at') or '')[:10]} | {s['hits']}/{s['answered']} | "
                         f"{R.fmt(s['latency_p50_s'], 2)} / {R.fmt(s['latency_p95_s'], 2)} | {s['malformed']} |")
        lines.append("")
    if data["resources"]:
        lines += ["Memory (`bench resources`; model VRAM is GPU used minus the pre-server baseline):", "",
                  "| Model | Mode | Measured | GPU used / total (GiB) | Model VRAM (GiB) | Budget (GiB) | Server RSS (GiB) "
                  "| WSL RAM used (GiB) | Game + Steam + Xvfb RSS (GiB) |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for (model, mode), r in sorted(data["resources"].items()):
            game = r.get("game") or {}
            lines.append(f"| {model} | {mode} | {(r.get('created_at') or '')[:10]} | "
                         f"{R.fmt(r.get('vram_used_gib'))} / {R.fmt(r.get('vram_total_gib'))} | "
                         f"{R.fmt(r.get('model_vram_gib'))} | {registry.budget(mode):g} | "
                         f"{R.fmt((r.get('server') or {}).get('rss_gib'))} | {R.fmt(r.get('ram_used_gib'))} | "
                         f"{R.fmt(game.get('rss_gib')) if game.get('running') else '-'} |")
        lines.append("")
    if g:
        lines.append(ranking_markdown(ranking(data)))
    return "\n".join(lines).strip() + "\n"


# ---- the **measure** rows


@dataclass(frozen=True)
class MeasureRow:
    id: str
    section: str  # heading the row must be under
    pattern: str  # regex the stripped line must match at its start
    table: bool  # a table row (value and source cells) or a bullet (text after the pattern)
    value: Callable[[dict], tuple[str, str] | None]  # data -> (value, source) or None when not measured


def _newest(rows: list[dict]) -> dict | None:
    return max(rows, key=lambda r: r.get("created_at") or "", default=None)


def _activations(data: dict):
    r = _newest([r for r in data["resources"].values() if r.get("activations_gib") is not None])
    if not r:
        return None
    return (f"{r['activations_gib']:.1f}",
            f"measured {r['created_at'][:10]} ({r['model']}): model VRAM minus vLLM's \"Model loading took\" "
            "and \"Available KV cache memory\"")


def _games(data: dict):
    r = _newest([r for (_, mode), r in data["resources"].items()
                 if mode == "coop" and r.get("games_vram_gib") is not None])
    if not r:
        return None
    return (f"{r['games_vram_gib']:.1f}",
            f"measured {r['created_at'][:10]}: GPU used with both copies running minus the desktop baseline")


def _server_rss(data: dict):
    rows = [r for r in data["resources"].values() if (r.get("server") or {}).get("rss_gib")]
    r = _newest([x for x in rows if "vllm" in ((x["server"].get("cmd") or "").lower())]) or _newest(rows)
    if not r:
        return None
    kind = "vLLM" if "vllm" in (r["server"].get("cmd") or "").lower() else "llama-server"
    return f"{r['server']['rss_gib']:.1f} GiB RSS ({r['model']} on {kind}, measured {r['created_at'][:10]}).", ""


def _game_rss(data: dict):
    r = _newest([r for r in data["resources"].values() if (r.get("game") or {}).get("running")])
    if not r:
        return None
    g = r["game"]
    parts = ", ".join(f"{name} {R.fmt((g.get(key) or {}).get('rss_gib'))}"
                      for name, key in (("game", "game"), ("Steam", "steam"), ("Xvfb", "xvfb")))
    return f"{g['rss_gib']:.1f} GiB RSS ({parts}; measured {r['created_at'][:10]}).", ""


MEASURE_ROWS = (
    MeasureRow("vram_activations", "## VRAM budget", r"\|\s*\.\.\.activations, CUDA graphs, vision encoder", True,
               _activations),
    MeasureRow("vram_games", "## VRAM budget", r"\|\s*Two Spire 2 copies\s*\|", True, _games),
    MeasureRow("ram_server", "## RAM budget", r"-\s+The [\w .-]*process itself:", False, _server_rss),
    MeasureRow("ram_game", "## RAM budget", r"-\s+The AI's copy, Steam for Linux and Xvfb:", False, _game_rss),
)


def _tag(row: MeasureRow) -> str:
    return f"<!-- bench:{row.id} -->"


def _rewrite(line: str, row: MeasureRow, value: str, source: str) -> str:
    indent = line[:len(line) - len(line.lstrip())]
    if row.table:
        cells = line.strip().split("|")  # ['', label, value, source, '']
        if len(cells) < 5:
            return line
        cells[2], cells[3] = f" {value} ", f" {source} {_tag(row)} "
        return indent + "|".join(cells)
    match = re.match(row.pattern, line.strip())
    return f"{indent}{match.group(0)} {value} {_tag(row)}"


def _sections(lines: list[str]) -> dict[int, str]:
    """Line index -> the "## " heading it falls under."""
    current, out = "", {}
    for i, line in enumerate(lines):
        if line.startswith("## "):
            current = line.strip()
        out[i] = current
    return out


def replace_measure_rows(text: str, data: dict) -> tuple[str, list[str]]:
    lines = text.split("\n")
    under = _sections(lines)
    end = next((i for i, line in enumerate(lines) if line.strip() == START), len(lines))
    touched = []
    for row in MEASURE_ROWS:
        measured = row.value(data)
        if measured is None:
            continue
        for i, line in enumerate(lines[:end]):
            if not under[i].startswith(row.section) or not re.match(row.pattern, line.strip()):
                continue
            if not (_MEASURE.search(line) or _tag(row) in line):
                continue
            lines[i] = _rewrite(line, row, *measured)
            touched.append(row.id)
    return "\n".join(lines), touched


def write_block(text: str, body: str) -> str:
    start, end = text.find(START), text.find(END)
    if start != -1 and end > start:
        return text[:start + len(START)] + "\n" + body + text[end:]
    return text.rstrip("\n") + f"\n\n{SECTION}\n\n{START}\n{body}{END}\n"


def write_docs(path: str | Path, data: dict, today: str | None = None) -> list[str]:
    """Update docs/hosting.md in place. Returns the measure rows it touched."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    text, touched = replace_measure_rows(text, data)
    path.write_text(write_block(text, block(data, today)), encoding="utf-8")
    return touched
