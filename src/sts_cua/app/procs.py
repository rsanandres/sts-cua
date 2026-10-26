"""Processes the control app starts, stops and reports on.

Children the app starts get a pid file in <state_dir>/run/<name>.pid (JSON: pid, start
time, argv, log), so an app restarted later still knows them. A pid file only counts
while /proc shows the same process (same start time), so a reused pid isn't mistaken
for it. Processes started outside the app are found with pgrep patterns (WATCHES).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Agent runs: the harness (`python -m sts_cua ...`), the supervisor, the memory CLI and
# the memory A/B evaluation, but not this app (`python -m sts_cua.app`).
# tools/stack_down.sh uses the same pattern.
AGENT_PATTERN = r"python[0-9.]* -m sts_cua(\.supervisor|\.memory|\.eval)?( |$)"
# A game-playing run, for /api/live: the harness, the supervisor or an evaluation (which drives the supervisor).
RUN_PATTERN = r"python[0-9.]* -m sts_cua(\.supervisor|\.eval run)?( |$)"
SUPERVISOR_PATTERN = r"python[0-9.]* -m sts_cua\.(supervisor|eval run)( |$)"
# A co-op session: the harness with --coop (python -m sts_cua ... --coop).
COOP_PATTERN = r"python[0-9.]* -m sts_cua .*--coop( |$)"
# A local model server: vLLM, or llama.cpp's llama-server as the program itself (argv[0]),
# so `python -m sts_cua.serve start x --llama-server ...` isn't mistaken for one.
# tools/stack_down.sh uses the same pattern.
# The [ ] and [.] keep a pgrep running this pattern from matching its own command line.
MODEL_PATTERN = r"vllm[ ]serve|vllm[.]entrypoints|^[^ ]*llama-server( |$)"
VLLM_PATTERN = MODEL_PATTERN  # the old name


def watches(display: str = ":99") -> dict[str, list[str]]:
    """pgrep arguments for each process /api/status reports."""
    return {
        "steam": ["-x", "steam"],
        "xvfb": ["-f", f"Xvfb {display}( |$)"],
        "game": ["-x", "SlayTheSpire2"],
        "model": ["-f", MODEL_PATTERN],
        "supervisor": ["-f", SUPERVISOR_PATTERN],
        "coop": ["-f", COOP_PATTERN],
    }


def pgrep(args: list[str]) -> list[int]:
    try:
        out = subprocess.run(["pgrep", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def _stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name, or None if there is no such process."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw[raw.rfind(")") + 2:].split()


def start_ticks(pid: int) -> int | None:
    fields = _stat(pid)
    return int(fields[19]) if fields and len(fields) > 19 else None


def alive(pid: int, ticks: int | None = None) -> bool:
    """The process exists, isn't a zombie and, when ticks is given, started at that time."""
    fields = _stat(pid)
    if fields is None or fields[0] in ("Z", "X"):
        return False
    return ticks is None or int(fields[19]) == ticks


def started_at(pid: int) -> str | None:
    """When a process started, from /proc (boot time + start ticks)."""
    ticks = start_ticks(pid)
    if ticks is None:
        return None
    try:
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime "))
    except (OSError, StopIteration, ValueError):
        return None
    seconds = btime + ticks / os.sysconf("SC_CLK_TCK")
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def popen(argv: list[str], cwd: Path, env: dict[str, str], log) -> subprocess.Popen:
    """Start a child in its own session (so signals can go to its whole group), output to log."""
    return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)


@dataclass
class ProcessManager:
    state_dir: Path
    display: str = ":99"
    launcher: Callable[..., subprocess.Popen] = popen
    find: Callable[[list[str]], list[int]] = pgrep
    _handles: dict[str, subprocess.Popen] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    stopping: set[str] = field(default_factory=set)

    @property
    def run_dir(self) -> Path:
        return self.state_dir / "run"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def log_path(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def _pid_file(self, name: str) -> Path:
        return self.run_dir / f"{name}.pid"

    def start(self, name: str, argv: list[str], cwd: Path, env: dict[str, str] | None = None,
              log_name: str | None = None, extra: dict | None = None) -> int:
        """Start a child, append its output to <logs>/<log_name>.log and write its pid file."""
        with self._lock:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_path(log_name or name)
            with log_path.open("ab") as log:
                offset = log.tell()
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                log.write(f"--- {stamp} sts_cua.app started: {' '.join(argv)}\n".encode())
                log.flush()
                proc = self.launcher(argv, cwd=cwd, env={**os.environ, **(env or {})}, log=log)
            self._handles[name] = proc
            record = {"pid": proc.pid, "start_ticks": start_ticks(proc.pid), "started_at": stamp,
                      "argv": argv, "log": str(log_path), "log_offset": offset, **(extra or {})}
            self._pid_file(name).write_text(json.dumps(record) + "\n", encoding="utf-8")
            return proc.pid

    def managed(self, name: str) -> dict | None:
        """The pid file record of a child the app started, while it is still running."""
        with self._lock:
            path = self._pid_file(name)
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            handle = self._handles.get(name)
            if handle is not None and handle.pid == record.get("pid"):
                running = handle.poll() is None  # also reaps an exited child
            else:
                running = alive(record["pid"], record.get("start_ticks"))
            if not running:
                self._handles.pop(name, None)
                path.unlink(missing_ok=True)
                return None
            return record

    def update(self, name: str, **fields) -> None:
        """Add fields to a running child's pid file record."""
        with self._lock:
            record = self.managed(name)
            if record:
                self._pid_file(name).write_text(json.dumps({**record, **fields}) + "\n", encoding="utf-8")

    def pids(self, name: str) -> list[int]:
        """Running pids for a watched process: the app's own child first, then pgrep matches."""
        record = self.managed(name)
        found = [record["pid"]] if record else []
        args = watches(self.display).get(name)
        if args:
            found += [p for p in self.find(args) if p not in found]
        return found

    def info(self, name: str) -> dict:
        found = self.pids(name)
        if not found:
            return {"running": False, "pid": None, "since": None}
        record = self.managed(name)
        since = started_at(found[0]) or (record or {}).get("started_at")
        return {"running": True, "pid": found[0], "since": since}

    def matching(self, pattern: str) -> list[int]:
        return self.find(["-f", pattern])

    def send(self, name: str, sig: int) -> list[int]:
        """Send sig to the app's child (its whole process group) and to pgrep matches."""
        record = self.managed(name)
        sent = []
        for pid in self.pids(name):
            try:
                if record and pid == record["pid"]:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
                sent.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        return sent

    def stop(self, name: str, grace: float = 10.0, first: int = signal.SIGINT,
             then: int = signal.SIGTERM) -> list[int]:
        """Send `first` now and `then` after grace seconds to whatever is still running."""
        sent = self.send(name, first)
        if not sent:
            return []
        with self._lock:
            self.stopping.add(name)

        def escalate():
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and self.pids(name):
                time.sleep(0.2)
            if self.pids(name):
                self.send(name, then)
            with self._lock:
                self.stopping.discard(name)

        threading.Thread(target=escalate, daemon=True, name=f"stop-{name}").start()
        return sent

    def log_since_start(self, name: str, limit: int = 65536) -> str:
        """Output a running child wrote since the app started it (up to limit bytes)."""
        record = self.managed(name)
        if not record:
            return ""
        try:
            with open(record["log"], "rb") as f:
                f.seek(record.get("log_offset", 0))
                return f.read(limit).decode("utf-8", "replace")
        except OSError:
            return ""
