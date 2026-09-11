"""Find dead zones in a finished run, and cut the footage that shows them.

This is the rig's actual output. A survey walk produces three things: what
fraction of the walk had a usable link, a list of the places where it did not,
and a short video clip of each so you can see whether the spot matters — a ramp
the robot has to drive, or a corner nobody goes near.

Detection deliberately works off the recorded samples rather than live state,
so the rules can change and old runs can be re-analysed without re-walking.

Clip geometry follows the zone rather than a fixed window: ``pre_roll`` seconds
of approach, then the whole dead zone however long it ran, then ``post_roll``
seconds of recovery. A ninety-second dead stretch produces a clip you can watch
end to end, not a thirty-second excerpt of the middle.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

#: Sample interval, seconds. Samples are written on a 1 Hz grid.
SAMPLE_S = 1.0

#: How far apart two samples can be before we treat the gap as a break in the
#: walk (a pause, or the service restarting) rather than continuous data.
CONTINUITY_GAP_S = 3.0


@dataclass
class DeadZone:
    """One stretch of walk where the link was unusable."""

    index: int
    start_ts: float
    end_ts: float
    worst_loss_pct: float | None = None
    worst_rtt_ms: float | None = None
    sample_count: int = 0
    clip_path: str | None = None
    clip_error: str | None = None
    video_file: str | None = None
    video_offset_s: float | None = None

    @property
    def duration_s(self) -> float:
        # Each sample covers one second, so a zone spanning a single sample is
        # one second long, not zero.
        return round(self.end_ts - self.start_ts + SAMPLE_S, 1)

    def clip_window(self, pre_roll_s: float, post_roll_s: float) -> tuple[float, float]:
        return self.start_ts - pre_roll_s, self.end_ts + SAMPLE_S + post_roll_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": self.duration_s,
            "start_local": _local(self.start_ts),
            "start_utc": _utc(self.start_ts),
            "worst_loss_pct": self.worst_loss_pct,
            "worst_rtt_ms": self.worst_rtt_ms,
            "sample_count": self.sample_count,
            "clip_path": self.clip_path,
            "clip_error": self.clip_error,
            "video_file": self.video_file,
            "video_offset_s": self.video_offset_s,
        }


def _local(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def is_unusable(sample: dict, config: dict) -> bool:
    """Whether one sample is bad enough to count toward a dead zone.

    Loss or latency alone is enough — a link that answers every packet two
    seconds late is no more usable than one that answers none.
    """
    loss = sample.get("loss_pct")
    rtt = sample.get("rtt_ms")
    if loss is not None and loss >= float(config.get("loss_pct_at_least", 80)):
        return True
    if rtt is not None and rtt >= float(config.get("or_rtt_ms_at_least", 1500)):
        return True
    # No reply at all: ping records loss, but a sample taken before any reading
    # existed has neither field. Treat "we know nothing" as not-a-dead-zone
    # rather than inventing one.
    return False


def find_dead_zones(samples: Sequence[dict], config: dict) -> list[DeadZone]:
    """Group unusable samples into merged, long-enough dead zones."""
    min_duration = float(config.get("min_duration_s", 5))
    merge_gap = float(config.get("merge_gap_s", 10))

    runs: list[list[dict]] = []
    current: list[dict] = []
    previous_ts: float | None = None

    for sample in samples:
        ts = sample["ts"]
        # A jump in timestamps means the walk was paused or the service
        # restarted. Do not stitch across it — the operator was not walking.
        broken = previous_ts is not None and (ts - previous_ts) > CONTINUITY_GAP_S
        if is_unusable(sample, config) and not broken:
            current.append(sample)
        elif is_unusable(sample, config):
            if current:
                runs.append(current)
            current = [sample]
        elif current:
            runs.append(current)
            current = []
        previous_ts = ts
    if current:
        runs.append(current)

    merged = _merge_adjacent(runs, merge_gap)

    zones: list[DeadZone] = []
    for group in merged:
        start_ts = group[0]["ts"]
        end_ts = group[-1]["ts"]
        if (end_ts - start_ts + SAMPLE_S) < min_duration:
            continue
        losses = [s["loss_pct"] for s in group if s.get("loss_pct") is not None]
        rtts = [s["rtt_ms"] for s in group if s.get("rtt_ms") is not None]
        zones.append(DeadZone(
            index=len(zones) + 1,
            start_ts=start_ts,
            end_ts=end_ts,
            worst_loss_pct=max(losses) if losses else None,
            worst_rtt_ms=max(rtts) if rtts else None,
            sample_count=len(group),
            video_file=group[0].get("video_file"),
            video_offset_s=group[0].get("video_offset_s"),
        ))
    return zones


def _merge_adjacent(runs: list[list[dict]], merge_gap: float) -> list[list[dict]]:
    """Fold runs separated by less than ``merge_gap`` into one.

    A single bad ramp tends to flicker in and out; without this it lands in the
    report as six entries with six nearly identical clips.
    """
    if not runs:
        return []
    merged = [runs[0]]
    for group in runs[1:]:
        gap = group[0]["ts"] - merged[-1][-1]["ts"]
        if gap <= merge_gap:
            merged[-1] = merged[-1] + group
        else:
            merged.append(group)
    return merged


def summarise(samples: Sequence[dict], zones: Sequence[DeadZone]) -> dict[str, Any]:
    """Headline numbers for a finished run.

    The percentage is of *time walked*, not distance — there is no positioning
    indoors. Pausing while stationary is what keeps it meaningful, which is why
    paused time produces no samples at all.
    """
    total = len(samples)
    dead_samples = sum(zone.sample_count for zone in zones)
    rtts = [s["rtt_ms"] for s in samples if s.get("rtt_ms") is not None]
    losses = [s["loss_pct"] for s in samples if s.get("loss_pct") is not None]

    return {
        "walked_s": round(total * SAMPLE_S, 1),
        "sample_count": total,
        "dead_zone_count": len(zones),
        "dead_s": round(dead_samples * SAMPLE_S, 1),
        "runnable_pct": round(100.0 * (total - dead_samples) / total, 1) if total else None,
        "longest_dead_zone_s": max((z.duration_s for z in zones), default=0.0),
        "rtt_ms": {
            "min": round(min(rtts), 1) if rtts else None,
            "median": round(_median(rtts), 1) if rtts else None,
            "max": round(max(rtts), 1) if rtts else None,
        },
        "mean_loss_pct": round(sum(losses) / len(losses), 1) if losses else None,
        "measured_in": "time, not distance — walk at a steady pace and pause when stationary",
    }


def _median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ---------------------------------------------------------------------------
# Clip extraction
# ---------------------------------------------------------------------------

@dataclass
class SegmentSpan:
    """A piece of one video segment that overlaps the requested window."""

    path: Path
    start_epoch: float
    #: Seconds into this file where the wanted footage begins.
    offset_s: float


def plan_clip(segments: Sequence[tuple[str, float]], video_dir: Path,
              window_start: float, window_end: float,
              segment_length_s: float) -> list[SegmentSpan]:
    """Work out which segment files cover a window, and where in the first one.

    A dead zone near a segment boundary needs footage from two files, so the
    result is a list to be concatenated rather than a single (file, offset).
    """
    spans: list[SegmentSpan] = []
    for name, start_epoch in segments:
        end_epoch = start_epoch + segment_length_s
        if end_epoch <= window_start or start_epoch >= window_end:
            continue
        path = video_dir / name
        if not path.exists():
            continue
        spans.append(SegmentSpan(
            path=path,
            start_epoch=start_epoch,
            offset_s=max(0.0, window_start - start_epoch),
        ))
    return spans


def extract_clip(zone: DeadZone, segments: Sequence[tuple[str, float]],
                 video_dir: Path, output_dir: Path, config: dict,
                 segment_length_s: float = 300.0,
                 container: str = "mp4") -> DeadZone:
    """Cut the footage for one dead zone. Mutates and returns the zone."""
    if shutil.which("ffmpeg") is None:
        zone.clip_error = "ffmpeg not installed"
        return zone

    pre_roll = float(config.get("pre_roll_s", 10))
    post_roll = float(config.get("post_roll_s", 5))
    window_start, window_end = zone.clip_window(pre_roll, post_roll)

    spans = plan_clip(segments, video_dir, window_start, window_end, segment_length_s)
    if not spans:
        zone.clip_error = "no video covers this dead zone"
        return zone

    output_dir.mkdir(parents=True, exist_ok=True)
    name = (f"deadzone-{zone.index:02d}-"
            f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(zone.start_ts))}-"
            f"{int(zone.duration_s)}s.{container}")
    output = output_dir / name
    duration = window_end - window_start

    try:
        if len(spans) == 1:
            _cut_one(spans[0], duration, output)
        else:
            _cut_joined(spans, duration, output)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        zone.clip_error = str(exc)[:300]
        # Do not leave a truncated file behind: a zero-byte .mp4 in the clips
        # directory looks like a clip until someone tries to play it.
        output.unlink(missing_ok=True)
        return zone

    if not output.exists() or output.stat().st_size == 0:
        zone.clip_error = "ffmpeg produced no output"
        output.unlink(missing_ok=True)
        return zone

    zone.clip_path = str(output)
    return zone


def _ffmpeg(args: list[str], timeout: float = 180) -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                             "-nostdin", "-y", *args],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"ffmpeg exited {result.returncode}")


def _cut_one(span: SegmentSpan, duration: float, output: Path) -> None:
    # -ss before -i seeks by keyframe, which is why this is fast and why the
    # clip can begin a second or two early. Re-encoding would be frame-exact
    # and far slower on a Pi; the pre-roll exists to absorb exactly this.
    _ffmpeg(["-ss", f"{span.offset_s:.3f}", "-i", str(span.path),
             "-t", f"{duration:.3f}", "-c", "copy", "-avoid_negative_ts",
             "make_zero", str(output)])


def _cut_joined(spans: Sequence[SegmentSpan], duration: float, output: Path) -> None:
    """Join consecutive segments, then cut — for a zone crossing a boundary."""
    with tempfile.TemporaryDirectory() as work:
        listing = Path(work) / "segments.txt"
        listing.write_text("".join(f"file '{span.path.resolve()}'\n" for span in spans))
        joined = Path(work) / f"joined{output.suffix}"
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", str(joined)])
        # The offset is measured from the start of the first span, which is
        # where the concatenated file now begins.
        _ffmpeg(["-ss", f"{spans[0].offset_s:.3f}", "-i", str(joined),
                 "-t", f"{duration:.3f}", "-c", "copy", "-avoid_negative_ts",
                 "make_zero", str(output)])
