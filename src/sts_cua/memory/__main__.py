"""Inspect and edit the note store, and score or reflect on a finished run.

    python -m sts_cua.memory list [--all] [--store memory/notes.jsonl]
    python -m sts_cua.memory show n0003
    python -m sts_cua.memory retire n0003
    python -m sts_cua.memory score runs/20261025T164617Z
    python -m sts_cua.memory reflect runs/20261025T164617Z [--model claude-sonnet-5-5] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .reflect import DEFAULT_MODEL, build_request, reflect
from .runs import load_run
from .score import score_run
from .store import DEFAULT_PATH, NoteStore, now


def _store_path(args, run=None) -> Path:
    if args.store:
        return Path(args.store)
    if run is not None and (run.meta.get("memory") or {}).get("path"):
        return Path(run.meta["memory"]["path"])
    return DEFAULT_PATH


def _stats(note) -> str:
    u = note.utility
    if u.mean is None:
        return f"{u.shown.n}/{u.hidden.n} -"
    return f"{u.shown.n}/{u.hidden.n} {u.mean:+.3f} [{u.lo:+.3f},{u.hi:+.3f}]"


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help=f"notes file (default: the run's memory path, else {DEFAULT_PATH})")
    parser = argparse.ArgumentParser(prog="sts_cua.memory", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", parents=[common], help="list notes")
    p.add_argument("--all", action="store_true", help="include retired notes")
    sub.add_parser("show", parents=[common], help="print one note as JSON").add_argument("id")
    sub.add_parser("retire", parents=[common], help="retire a note").add_argument("id")
    p = sub.add_parser("score", parents=[common], help="update utilities from a run's outcomes")
    p.add_argument("run_dir")
    p.add_argument("--force", action="store_true", help="score again even if already scored (counts the run twice)")
    p = sub.add_parser("reflect", parents=[common], help="score the run, then one Claude call to edit the store")
    p.add_argument("run_dir")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true", help="print the request instead of calling Claude")
    args = parser.parse_args(argv)

    if args.command in ("score", "reflect"):
        run = load_run(args.run_dir)
        store = NoteStore.load(_store_path(args, run))
        if args.command == "score":
            report = score_run(store, run, force=args.force)
            store.save()
            print(json.dumps(report, indent=2))
        elif args.dry_run:
            print(json.dumps(build_request(store, run, args.model), indent=2))
        else:
            record = reflect(store, run, model=args.model)
            print(json.dumps({k: record[k] for k in ("cost_usd", "results") if k in record}, indent=2))
            if "error" in record:
                print(record["error"], file=sys.stderr)
        return

    store = NoteStore.load(_store_path(args))
    if args.command == "list":
        for note in store.notes:
            if note.status == "retired" and not args.all:
                continue
            keys = ",".join(f"{k}={v}" for k, v in note.keys.items()) or "-"
            print(f"{note.id}  {note.status:<9} {note.kind:<8} {keys:<32} shown {note.shown_n:<4} {_stats(note)}  {note.text[:70]}")
        return
    note = store.get(args.id)
    if note is None:
        sys.exit(f"no note {args.id} in {store.path}")
    if args.command == "show":
        print(json.dumps(note.to_dict(), indent=2))
    elif args.command == "retire":
        note.status, note.updated = "retired", now()
        store.save()
        print(f"retired {note.id}")


if __name__ == "__main__":
    main()
