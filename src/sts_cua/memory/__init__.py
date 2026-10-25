"""Memory-based learning across runs, with no weight updates.

After a run, `reflect` writes a few short lessons into a note store; during the next
runs, `Retriever` shows the ones that match the current screen, and `score` keeps
the ones whose showing measurably helped.

- store.py: the notes file (memory/notes.jsonl) and its records
- retrieve.py: which notes to show each step (and the rule that keys only use what is on screen)
- runs.py: fights (via sts_cua.score) and wasted steps read back from a run directory
- score.py: utility from shown vs hidden outcomes; probation -> active -> retired
- reflect.py: one Claude call per run that returns delta operations on the store

CLI: python -m sts_cua.memory list|show|retire|score|reflect
"""

from .retrieve import Retriever
from .store import Note, NoteStore

__all__ = ["Note", "NoteStore", "Retriever"]
