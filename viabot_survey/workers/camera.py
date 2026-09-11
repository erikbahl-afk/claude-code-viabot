"""Continuous video recording for after-the-fact review.

The point of the camera is correlation: when the link dies at 14:32:07 you want
to see what the rig was looking at at 14:32:07, so you can tell an entrance
ramp from an irrelevant corner.

Two things make that reliable:

* ffmpeg writes **segments named in UTC** (``-strftime 1`` with ``TZ=UTC`` in
  its environment), so :meth:`locate` can turn any timestamp into a
  (file, offset) pair without parsing the video — and without depending on what
  the Pi's timezone happens to be set to. The Pi has no real-time clock and its
  timezone was never configured during imaging; naming segments in local time
  meant that setting the timezone later silently shifted every previously
  recorded run, and that one hour each DST fallback was ambiguous. UTC has
  neither problem.
* in ``overlay`` mode the clock is also **burned into the picture**, derived
  from frame PTS rather than render time so it cannot drift. That one is
  rendered in *local* time with its UTC offset printed alongside, because the
  person reviewing footage is matching it against when they were walking.

Output is Matroska in both modes because it survives an abrupt power loss —
an MP4 killed mid-segment loses its index and will not play at all.

Video is written to local disk only and deliberately not served over the AP.
"""

from __future__ import annotations

import calendar
import collections
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import STATE_DEGRADED, STATE_FAILED, Worker

# The trailing Z is load-bearing documentation: these names are UTC, and anyone
# reading the directory should be able to tell at a glance.
SEGMENT_PATTERN = "%Y%m%dT%H%M%SZ.mkv"
SEGMENT_RE = re.compile(r"^(\d{8})T(\d{6})Z\.mkv$")

# Checked in order; the first that exists is used for the burned-in clock.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
)


def find_font() -> str | None:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def segment_start_epoch(name: str) -> float | None:
    """Recover a segment's start time from its ``YYYYmmddTHHMMSSZ.mkv`` name.

    Deliberately uses :func:`calendar.timegm` rather than :func:`time.mktime`:
    the name is UTC, so the result must not depend on the host's timezone at
    the moment we happen to read it.
    """
    match = SEGMENT_RE.match(name)
    if not match:
        return None
    try:
        parsed = time.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return float(calendar.timegm(parsed))


def utc_offset_s(at: float | None = None) -> int:
    """Seconds east of UTC in force locally, DST included."""
    at = time.time() if at is None else at
    return -(time.altzone if time.localtime(at).tm_isdst else time.timezone)


def format_utc_offset(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(int(seconds))
    return f"{sign}{seconds // 3600:02d}{(seconds % 3600) // 60:02d}"


def free_disk_mb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / 1e6


class CameraWorker(Worker):
    name = "camera"

    def __init__(self, device: str = "/dev/video0", width: int = 1280, height: int = 720,
                 fps: int = 10, mode: str = "overlay", segment_s: int = 300,
                 min_free_disk_mb: float = 2000, output_dir: Path | None = None,
                 enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled, **kwargs)
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.mode = mode if mode in ("overlay", "copy") else "overlay"
        self.segment_s = max(10, int(segment_s))
        self.min_free_disk_mb = float(min_free_disk_mb)
        self.output_dir = Path(output_dir) if output_dir else None
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        # Keep the tail of ffmpeg's stderr: on a bad capture format it tells you
        # exactly which modes the camera does support.
        self._stderr: collections.deque[str] = collections.deque(maxlen=25)
        self._low_disk = False

    # -- configuration -------------------------------------------------------

    def set_output_dir(self, path: Path) -> None:
        self.output_dir = Path(path)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build_command(self, output_dir: Path, start_epoch: float) -> list[str]:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", str(self.fps),
            "-video_size", f"{self.width}x{self.height}",
            "-i", self.device,
        ]

        font = find_font() if self.mode == "overlay" else None
        if self.mode == "overlay" and font:
            # Derive the printed time from the frame's own PTS plus the capture
            # start, so the label matches when the frame was taken even if
            # encoding lags behind.
            #
            # ffmpeg runs with TZ=UTC so that segment filenames are UTC, which
            # also makes drawtext's "localtime" render UTC. Adding the local
            # offset to the base gets a local-time clock back in the picture,
            # and the offset itself is printed after it so the footage says
            # which zone it is in rather than leaving the reviewer to guess.
            offset = utc_offset_s(start_epoch)
            base = int(start_epoch) + offset
            label = f"%{{pts\\:localtime\\:{base}\\:%F %T}} {format_utc_offset(offset)}"
            drawtext = (
                f"drawtext=fontfile={font}"
                f":text={label}"
                ":fontsize=22:fontcolor=white"
                ":box=1:boxcolor=black@0.6:boxborderw=6"
                ":x=10:y=10"
            )
            cmd += [
                "-vf", drawtext,
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", "28", "-pix_fmt", "yuv420p",
                "-g", str(max(1, self.fps * 2)),
            ]
        else:
            if self.mode == "overlay" and not font:
                self.emit("warning",
                          "no usable font found; recording without a burned-in clock "
                          "(apt install fonts-dejavu-core)")
            # Store the camera's native MJPEG untouched: no decode, no encode.
            cmd += ["-c:v", "copy"]

        cmd += [
            "-f", "segment",
            "-segment_time", str(self.segment_s),
            "-segment_format", "matroska",
            "-reset_timestamps", "1",
            "-strftime", "1",
            str(output_dir / SEGMENT_PATTERN),
        ]
        return cmd

    # -- worker loop ---------------------------------------------------------

    def run_once(self) -> None:
        if shutil.which("ffmpeg") is None:
            self._set_state(STATE_FAILED, "ffmpeg not installed (apt install ffmpeg)")
            self.emit("error", "ffmpeg binary not found; install ffmpeg")
            self.wait(30)
            return
        if not Path(self.device).exists():
            self._set_state(STATE_FAILED, f"{self.device} not present")
            self.emit("error", f"camera {self.device} not found")
            self.wait(10)
            return
        if self.output_dir is None:
            # Idle until a run starts and gives us somewhere to write.
            self.wait(1.0)
            return
        if not self._check_disk():
            self.wait(10)
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._started_at = time.time()
        # TZ=UTC makes -strftime name segments in UTC; see the module docstring.
        environment = {**os.environ, "TZ": "UTC"}
        self._proc = subprocess.Popen(
            self.build_command(self.output_dir, self._started_at),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=environment)
        self._write_manifest()
        self.emit("info", f"recording to {self.output_dir}")

        assert self._proc.stderr is not None
        try:
            for line in self._proc.stderr:
                line = line.rstrip()
                if line:
                    self._stderr.append(line)
                if self.stopping:
                    break
                if not self._check_disk():
                    break
        finally:
            code = self._terminate()
            self._started_at = None
            if code not in (0, None) and not self.stopping:
                detail = "; ".join(list(self._stderr)[-3:]) or f"exit {code}"
                self._set_state(STATE_FAILED, detail)
                self.emit("error", f"ffmpeg stopped: {detail}")

    def _check_disk(self) -> bool:
        if self.output_dir is None:
            return False
        free = free_disk_mb(self.output_dir)
        if free >= self.min_free_disk_mb:
            self._low_disk = False
            return True
        if not self._low_disk:
            self._low_disk = True
            self._set_state(STATE_DEGRADED, f"only {free:.0f} MB free")
            self.emit("error",
                      f"stopping recording: {free:.0f} MB free, "
                      f"below the {self.min_free_disk_mb:.0f} MB floor")
        return False

    def on_stop(self) -> None:
        self._terminate()

    def _terminate(self) -> int | None:
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        if proc.poll() is None:
            # 'q' lets ffmpeg close the current segment cleanly; SIGTERM is the
            # fallback if it is wedged.
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        return proc.returncode

    def _write_manifest(self) -> None:
        """Record how to read this directory, for whoever opens it later.

        Segment names are UTC and the burned-in clock is local; six months from
        now, on a different machine, that distinction is not guessable from the
        files alone.
        """
        if self.output_dir is None or self._started_at is None:
            return
        offset = utc_offset_s(self._started_at)
        manifest = {
            "capture_started_epoch": self._started_at,
            "capture_started_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._started_at)),
            "segment_filenames_are": "UTC",
            "burned_in_clock_is": "local time",
            "local_utc_offset_s": offset,
            "local_utc_offset": format_utc_offset(offset),
            "timezone": time.strftime("%Z", time.localtime(self._started_at)),
            "device": self.device,
            "mode": self.mode,
            "resolution": f"{self.width}x{self.height}@{self.fps}",
        }
        try:
            (self.output_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n")
        except OSError as exc:
            self.emit("warning", f"could not write video manifest: {exc}")

    # -- correlation ---------------------------------------------------------

    def segments(self) -> list[tuple[str, float]]:
        """(filename, start epoch) for every segment in the current run, sorted."""
        if self.output_dir is None or not self.output_dir.exists():
            return []
        found = []
        for path in self.output_dir.iterdir():
            start = segment_start_epoch(path.name)
            if start is not None:
                found.append((path.name, start))
        found.sort(key=lambda item: item[1])
        return found

    def locate(self, ts: float) -> tuple[str | None, float | None]:
        """Map a wall-clock timestamp to (segment filename, offset in seconds)."""
        best: tuple[str, float] | None = None
        for name, start in self.segments():
            if start <= ts:
                best = (name, start)
            else:
                break
        if best is None:
            return None, None
        return best[0], round(ts - best[1], 2)

    def snapshot(self) -> dict[str, Any]:
        segments = self.segments()
        directory = self.output_dir
        return {
            "device": self.device,
            "mode": self.mode,
            "resolution": f"{self.width}x{self.height}@{self.fps}",
            "recording": self._proc is not None and self._proc.poll() is None,
            "output_dir": str(directory) if directory else None,
            "segments": len(segments),
            "current_segment": segments[-1][0] if segments else None,
            "free_disk_mb": round(free_disk_mb(directory), 1) if directory and directory.exists() else None,
            "stderr_tail": list(self._stderr)[-5:],
        }
