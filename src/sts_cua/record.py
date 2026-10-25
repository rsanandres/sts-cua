"""Real-time screen recording of the agent's display with ffmpeg, for video footage.

    with ScreenRecorder(":99", (1280, 720), run_dir):
        ...  # the run

Writes <run_dir>/screen.mp4 and screen.json (when recording started, so highlights.md can
give screen.mp4 timestamps). Needs ffmpeg with x11grab: `sudo apt install ffmpeg`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


class ScreenRecorder:
    def __init__(self, display: str, size: tuple[int, int], run_dir: Path, fps: int = 15):
        self.display, self.size, self.run_dir, self.fps = display, size, Path(run_dir), fps
        self._proc: subprocess.Popen | None = None

    def command(self, ffmpeg: str = "ffmpeg") -> list[str]:
        w, h = self.size
        return [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "x11grab", "-video_size", f"{w}x{h}", "-framerate", str(self.fps), "-i", self.display,
            # ultrafast keeps CPU use low next to the game; crf 26 keeps files small.
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
            str(self.run_dir / "screen.mp4"),
        ]

    def start(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg not found: install it with `sudo apt install ffmpeg`, or run without --record")
        log = (self.run_dir / "screen.log").open("w", encoding="utf-8")
        self._proc = subprocess.Popen(self.command(ffmpeg), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
        started = datetime.now(timezone.utc).isoformat()
        (self.run_dir / "screen.json").write_text(json.dumps({"started_at": started, "fps": self.fps}) + "\n",
                                                  encoding="utf-8")

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            self._proc.communicate(b"q", timeout=15)  # "q" lets ffmpeg finish the MP4 properly
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=5)

    def __enter__(self) -> "ScreenRecorder":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
