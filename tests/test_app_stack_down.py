"""tools/stack_down.sh must stop agent runs but not the control app (python -m sts_cua.app)."""

import re
import subprocess
import time
from pathlib import Path

import pytest

from sts_cua.app.procs import AGENT_PATTERN

SCRIPT = Path(__file__).parent.parent / "tools" / "stack_down.sh"
AGENT_RUNS = [
    "python -m sts_cua --display :99 --model claude-haiku-5-5",
    "/home/raph/sts-cua/.venv/bin/python -m sts_cua --display :99",
    ".venv/bin/python3 -m sts_cua.supervisor --display :99 --model scripted",
    "python3.12 -m sts_cua.supervisor",
    "python -m sts_cua.memory reflect runs/20261025T164617Z",
    "python -m sts_cua",
    "python -m sts_cua.eval run memory-ab --display :99",
    "/home/raph/sts-cua/.venv/bin/python -m sts_cua.bench grounding holo4-27b --serve --mode solo",
]
NOT_AGENT = [
    "python -m sts_cua.app",
    "/home/raph/sts-cua/.venv/bin/python -m sts_cua.app --port 8765",
    "python -m sts_cua.apps",
    "python -m sts_cua_other",
    "python -m sts_cua.evaluate",
    "python -m sts_cua.benchmark",
    "vim src/sts_cua/supervisor.py",
    "bash tools/stack_down.sh",
]


def script_pattern() -> str:
    line = next(line for line in SCRIPT.read_text().splitlines() if line.startswith('stop "agent runs"'))
    return re.search(r"-f '([^']+)'", line).group(1)


def test_script_uses_the_shared_pattern():
    assert script_pattern() == AGENT_PATTERN


@pytest.mark.parametrize("cmdline", AGENT_RUNS + NOT_AGENT)
def test_pattern_with_grep_ere(cmdline):
    """pgrep/pkill use POSIX extended regexes, like grep -E."""
    found = subprocess.run(["grep", "-qE", script_pattern()], input=cmdline, text=True).returncode == 0
    assert found == (cmdline in AGENT_RUNS)
    assert bool(re.search(AGENT_PATTERN, cmdline)) == found


def test_pattern_with_real_pgrep():
    procs = {c: subprocess.Popen(["bash", "-c", f'exec -a "{c}" sleep 30']) for c in AGENT_RUNS[:3] + NOT_AGENT[:2]}
    try:
        time.sleep(0.3)
        out = subprocess.run(["pgrep", "-f", script_pattern()], capture_output=True, text=True).stdout
        matched = {int(p) for p in out.split()}
        for cmdline, proc in procs.items():
            assert (proc.pid in matched) == (cmdline in AGENT_RUNS), cmdline
    finally:
        for proc in procs.values():
            proc.kill()
            proc.wait()
