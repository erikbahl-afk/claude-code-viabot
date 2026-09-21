"""Turn a finished run into something a person can read.

A walk produces a database full of one-second samples, which is the right shape
for analysis and the wrong shape for answering the only question anyone asks:
*can the robot work here, and if not, where does it fail?*

So the report has two audiences on two tabs. The first is the answer — the
headline percentage, and every dead zone with a clip you can watch. The second
is the evidence, for when the first is surprising and someone wants to know
whether to believe it.

The rendered page is one self-contained HTML file: no external stylesheets, no
fonts, no scripts fetched from anywhere. It has to open from a laptop with no
network, from a cloud server, and off a USB stick, and it has to keep working
years from now when whatever CDN was fashionable this month has gone.
"""

from __future__ import annotations

import bisect
import html
import json
import math
import time
from typing import Any, Iterable, Sequence

from . import chart, radio
from .workers.udpload import LOAD_SENDING, parse_bitrate_mbps

#: Files are laid out beside the report when it is published, so every link in
#: the page is relative and the whole bundle can be moved or copied intact.
CLIP_DIR = "clips"
FULL_VIDEO = "video/full.mp4"


def iso_local(ts: float | None) -> str:
    """Local time with the UTC offset spelled out.

    A bare local timestamp in an exported file is ambiguous, and this rig's
    entire output is timestamps.
    """
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def iso_utc(ts: float | None) -> str:
    """UTC, matching how video segment files are named."""
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def clock_time(ts: float | None) -> str:
    """Just the wall clock, which is what the burned-in video overlay shows."""
    if ts is None:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def duration(seconds: float | None) -> str:
    """Human-length durations. '2m 14s' reads faster than '134.0'."""
    if seconds is None:
        return "—"
    seconds = int(round(float(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _numbers(samples: Iterable[dict], key: str) -> list[float]:
    return [s[key] for s in samples if s.get(key) is not None]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _spread(values: Sequence[float]) -> dict[str, float] | None:
    """Min / median / 95th / max, rounded. None when there is nothing to say."""
    if not values:
        return None
    return {
        "min": round(min(values), 1),
        "median": round(_median(values), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def signal_stats(samples: Sequence[dict]) -> dict[str, Any]:
    """What the modem saw, and how often it changed its mind about which cell.

    Handovers matter more than they look. A few dead seconds at the same ramp
    on every pass, with a different cell ID either side of it, is a handover
    rather than a coverage hole — and no amount of antenna work fixes it.
    """
    cells = [s["cell_id"] for s in samples if s.get("cell_id")]
    handovers = sum(1 for a, b in zip(cells, cells[1:]) if a != b)
    bands = sorted({str(s["band"]) for s in samples if s.get("band")})

    return {
        "readings": sum(1 for s in samples if s.get("rsrp") is not None),
        "rsrp": _spread(_numbers(samples, "rsrp")),
        "rsrq": _spread(_numbers(samples, "rsrq")),
        "sinr": _spread(_numbers(samples, "sinr")),
        "rssi": _spread(_numbers(samples, "rssi")),
        "bands": bands,
        "distinct_cells": len(set(cells)),
        "handovers": handovers,
        "tech": sorted({str(s["tech"]) for s in samples if s.get("tech")}),
    }


def quality_stats(samples: Sequence[dict]) -> dict[str, Any]:
    """Latency and loss beyond the headline, plus the two rig faults that
    quietly invalidate a walk: an unsynchronised clock and undervoltage.

    The latency and loss figures cover the seconds the rig was *not* loading
    the uplink itself, so this table describes the link a robot would find.
    What the link did with a stream on it is the section below it, which is a
    different question and deserves its own numbers — averaging the two gives a
    median round trip nobody ever experienced. The fault counts are over the
    whole walk: a clock that was wrong was wrong the entire time.
    """
    judged = [s for s in samples if not s.get("uplink_loaded")]
    rtts = _numbers(judged, "rtt_ms")
    losses = _numbers(judged, "loss_pct")
    total = len(samples)

    return {
        "rtt_ms": _spread(rtts),
        "jitter_ms": _spread(_numbers(judged, "jitter_ms")),
        "dns_ms": _spread(_numbers(judged, "dns_ms")),
        "mean_loss_pct": round(sum(losses) / len(losses), 1) if losses else None,
        "seconds_with_any_loss": sum(1 for v in losses if v > 0),
        "replies": len(rtts),
        "judged_count": len(judged),
        # A walk taken on an unsynchronised clock cannot be lined up with the
        # video, which is the entire point of recording it.
        "unsynced_clock_samples": sum(
            1 for s in samples if s.get("clock_synced") == 0),
        "undervoltage_samples": sum(
            1 for s in samples if s.get("undervoltage") == 1),
        "sample_count": total,
    }


def _absent_reason(direction: str, walked_s: int, block_s: float,
                   idle_s: float = 0.0) -> str:
    """Why a direction has nothing, in terms of what the operator did.

    Uplink loss can only be counted at the far end, so the rig runs a fixed
    block and then asks the server what arrived. A block that does not finish
    reports nothing at all — which means a walk shorter than one block produces
    no uplink data whatsoever, and the old report simply left the section out.
    Silently dropping the half that decides whether a spot is workable is worse
    than saying "not measured".

    The block is followed by a quiet gap, so the walk has to outlast a whole
    cycle rather than just the block.
    """
    cycle_s = block_s + idle_s
    if direction == "uplink" and walked_s < cycle_s * 1.5:
        gap = (f", then stays quiet for {idle_s:g}s so the modem's buffer can "
               "drain" if idle_s else "")
        return (
            f"The walk lasted {walked_s}s. Uplink is measured in bursts of "
            f"{block_s:g}s{gap}, and a burst only reports once it finishes, so "
            f"this walk was cut off before one could. Walk for at least "
            f"{cycle_s * 1.5:.0f}s to get uplink readings."
        )
    if direction == "uplink":
        return ("No readings came back from the server. The test runs in "
                f"{block_s:g}s bursts and asks the far end what arrived; check "
                "the event log for what the load test said.")
    return ("The stream never produced a reading. Check the event log for what "
            "the load test said.")


def _silent_seconds(direction: str, samples: Sequence[dict],
                    readings: Sequence[dict]) -> int:
    """Seconds the test was running and nothing came back.

    That is the severe case — iperf3's control channel is TCP, so a link bad
    enough takes the test down and it goes quiet rather than reporting 100%
    loss. It is only severe when the rig was actually sending, though: uplink
    runs in bursts, so most of a walk is deliberate silence and counting that
    would report two thirds of every walk as a catastrophic failure.

    Falls back to the whole walk when nothing marked the samples, which is
    every run recorded before the bursts existed.
    """
    if direction != "uplink":
        return len(samples) - len(readings)
    sending = [s for s in samples if s.get("uplink_loaded") == LOAD_SENDING]
    if not sending:
        return len(samples) - len(readings)
    return sum(1 for s in sending if s.get("udp_up_loss_pct") is None)


def saturation(readings: Sequence[dict], prefix: str, offered_mbps: float | None,
               below: float) -> dict[str, Any]:
    """How much of a direction could not carry the rate it was offered.

    This is the difference between "the garage is bad" and "we broke it
    ourselves". A link that delivers everything it was offered and still shows
    jitter is telling you about the place. A link delivering half of it is
    *full* — and a full link loses packets and delays them because it is full,
    so its loss and jitter figures are a floor, not a measurement. Without this
    the two are indistinguishable in the report, because bufferbloat saturates:
    once the buffer is full the latency stops rising, so a link that was
    slightly short and one that was hopelessly short look identical.

    Reported rather than corrected. There is no honest way to recover what the
    numbers would have been; the answer is to offer less next time.
    """
    if not offered_mbps:
        return {"offered_mbps": None}
    floor = offered_mbps * float(below)
    delivered = [s.get(prefix + "mbps") for s in readings]
    short = sum(1 for v in delivered if v is not None and v < floor)
    got = [v for v in delivered if v is not None]
    return {
        "offered_mbps": round(offered_mbps, 3),
        "saturated_seconds": short,
        "saturated_pct": round(100.0 * short / len(readings), 1) if readings else None,
        "delivered_median_mbps": round(_median(got), 3) if got else None,
        "below": float(below),
    }


def load_stats(samples: Sequence[dict],
               load_config: dict | None = None) -> dict[str, Any]:
    """What the link did with a real teleop load on it, each way separately.

    A teleop session is asymmetric and the two halves fail differently. Uplink
    carries the robot's video and is usually the weaker direction, so it is
    normally what decides whether a spot is workable. Downlink carries the
    operator's commands: small, but if it collapses the robot stops taking
    orders.

    The count of seconds with no reading matters as much as the numbers.
    iperf3's control channel is TCP, so when the link fails badly the test
    stops rather than reporting 100% loss — silence is the severe case, not a
    gap in the data.
    """
    config = load_config or {}
    block_s = float(config.get("uplink_block_s") or 30)
    idle_s = float(config.get("uplink_idle_s") or 0)
    below = float(config.get("uplink_saturated_below") or 0.85)
    out: dict[str, Any] = {}
    for name, prefix, rate_key in (("uplink", "udp_up_", "uplink_bitrate"),
                                   ("downlink", "udp_down_", "downlink_bitrate")):
        readings = [s for s in samples if s.get(prefix + "loss_pct") is not None]
        if not readings:
            out[name] = {
                "seconds_measured": 0,
                "absent_reason": _absent_reason(name, len(samples), block_s, idle_s),
            }
            continue
        losses = _numbers(readings, prefix + "loss_pct")
        offered = parse_bitrate_mbps(config.get(rate_key))
        out[name] = {
            "seconds_measured": len(readings),
            "seconds_without_stream": _silent_seconds(name, samples, readings),
            "jitter_ms": _spread(_numbers(readings, prefix + "jitter_ms")),
            "loss_pct": _spread(losses),
            "mbps": _spread(_numbers(readings, prefix + "mbps")),
            "mean_loss_pct": round(sum(losses) / len(losses), 1) if losses else None,
            "clean_seconds": sum(1 for v in losses if v == 0),
            **saturation(readings, prefix, offered, below),
        }
    out["seconds_measured"] = max(out["uplink"]["seconds_measured"],
                                  out["downlink"]["seconds_measured"])
    out["uplink_block_s"] = block_s
    out["uplink_idle_s"] = idle_s
    out["enabled"] = bool(config.get("enabled"))
    # What the duty cycle should have delivered. Uplink sends for block_s out
    # of every block_s + idle_s, so anything far below that means the test was
    # failing rather than resting, and the page has to be able to say which.
    cycle_s = block_s + idle_s
    expected = int(len(samples) * block_s / cycle_s) if cycle_s else 0
    got = out["uplink"]["seconds_measured"]
    out["uplink_expected_seconds"] = expected
    out["uplink_coverage_pct"] = (round(100.0 * got / expected, 1)
                                  if expected else None)
    return out


#: Columns the throughput plot is bucketed into. A walk is sampled at 1 Hz, so
#: an hour underground is 3,600 readings per direction — more than a 900-pixel
#: plot can show and more than belongs in a page that has to be uploaded over
#: the link it is describing. Each column then covers several seconds, and
#: keeps that span's worst, median and best rather than an average, because an
#: average hides exactly the second the video would have dropped.
TIMELINE_COLUMNS = 600

#: Consecutive samples further apart than this mean the run was paused. Pause
#: leaves no samples at all, so wall-clock time would draw a ten-minute coffee
#: break as a ten-minute outage. The plot runs on *walked* time instead, one
#: second per sample, and marks where the pauses were.
PAUSE_GAP_S = 3.0


def _bucket(values: Sequence[float | None], per: int) -> dict[str, list]:
    """Group a per-second series into columns of ``per`` seconds each."""
    lo: list[float | None] = []
    mid: list[float | None] = []
    hi: list[float | None] = []
    seconds: list[int] = []
    for start in range(0, len(values), per):
        chunk = [v for v in values[start:start + per] if v is not None]
        seconds.append(len(chunk))
        if not chunk:
            lo.append(None); mid.append(None); hi.append(None)
            continue
        lo.append(round(min(chunk), 3))
        mid.append(round(_median(chunk), 3))
        hi.append(round(max(chunk), 3))
    return {"lo": lo, "mid": mid, "hi": hi, "seconds": seconds}


def radio_timeline(samples: Sequence[dict], zones: Sequence[dict] = (),
                   columns: int = TIMELINE_COLUMNS) -> dict[str, Any]:
    """The radio score along the walk, on the same axis as the throughput plot.

    Deliberately the same shape and the same bucketing, so the two charts line
    up column for column and a reader can ask the question that actually
    matters: when the link failed, had the radio failed with it?
    """
    total = len(samples)
    if not total:
        return {}
    scored = [radio.rate(s) for s in samples]
    if not any(r["score"] is not None for r in scored):
        return {}

    per = max(1, math.ceil(total / columns))
    out: dict[str, Any] = {
        "columns": math.ceil(total / per),
        "seconds_per_column": per,
        "walked_s": total,
    }
    for name in ("strength", "quality"):
        values = [r[name] for r in scored]
        if any(v is not None for v in values):
            out[name] = _bucket(values, per)

    out["score"] = _bucket([r["score"] for r in scored], per)
    # The raw readings as well as the scores: the plot puts dBm on the axis,
    # because a number an engineer can check against a modem beats a derived
    # one they have to take on trust.
    out["rsrp"] = _bucket([s.get("rsrp") for s in samples], per)
    out["sinr"] = _bucket([s.get("sinr") for s in samples], per)
    stamps = [s["ts"] for s in samples]
    spans: list[list[int]] = []
    for zone in zones:
        start = min(bisect.bisect_left(stamps, zone["start_ts"]), total - 1)
        end = max(start, bisect.bisect_right(stamps, zone["end_ts"]) - 1)
        span = [start // per, min(end, total - 1) // per]
        if spans and span[0] <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], span[1])
        else:
            spans.append(span)
    out["dead_zones"] = spans
    out["pauses"] = sorted({i // per for i in range(1, total)
                            if stamps[i] - stamps[i - 1] > PAUSE_GAP_S})
    return out


def radio_summary(samples: Sequence[dict]) -> dict[str, Any]:
    """The score's headline figures, and which half of it was the problem.

    "How much of the walk was strength-limited" is the actionable number here:
    a garage that is mostly strength-limited may be fixable with antennas, and
    one that is mostly quality-limited will not be.
    """
    scored = [radio.rate(s) for s in samples if radio.rate(s)["score"] is not None]
    if not scored:
        return {"readings": 0}
    scores = [r["score"] for r in scored]
    bands: dict[str, int] = {}
    for entry in scored:
        bands[entry["band"]] = bands.get(entry["band"], 0) + 1
    limited: dict[str, int] = {}
    for entry in scored:
        if entry["limited_by"]:
            limited[entry["limited_by"]] = limited.get(entry["limited_by"], 0) + 1
    worst = min(scored, key=lambda r: r["score"])
    return {
        "readings": len(scored),
        "median": round(_median(scores), 1),
        "worst": round(min(scores), 1),
        "best": round(max(scores), 1),
        "band": radio.band(_median(scores)),
        "bands": bands,
        "limited_by": limited,
        "mostly_limited_by": max(limited, key=limited.get) if limited else None,
        "worst_limited_by": worst["limited_by"],
    }


def throughput_timeline(samples: Sequence[dict], zones: Sequence[dict] = (),
                        load_config: dict | None = None,
                        columns: int = TIMELINE_COLUMNS) -> dict[str, Any]:
    """What each direction actually delivered, second by second along the walk.

    This is not a speed test and must not be read as one. The rig sends a fixed
    teleop-sized stream and records what arrived, so the ceiling of this plot is
    the rate that was *offered* — the shape worth looking at is where it falls
    short of it, and where it stops altogether.

    Returns {} when the run has no load readings at all, which is every run
    taken before ``udp_load`` was switched on.
    """
    total = len(samples)
    if not total:
        return {}
    config = load_config or {}
    series: dict[str, dict[str, Any]] = {}
    for name, prefix, key in (("uplink", "udp_up_mbps", "uplink_bitrate"),
                              ("downlink", "udp_down_mbps", "downlink_bitrate")):
        values = [s.get(prefix) for s in samples]
        if not any(v is not None for v in values):
            continue
        series[name] = {"values": values,
                        "offered_mbps": parse_bitrate_mbps(config.get(key))}
    if not series:
        return {}

    per = max(1, math.ceil(total / columns))
    out: dict[str, Any] = {
        "columns": math.ceil(total / per),
        "seconds_per_column": per,
        "walked_s": total,
    }
    for name, built in series.items():
        out[name] = dict(_bucket(built["values"], per),
                         offered_mbps=built["offered_mbps"])

    # Dead zones and pauses are recorded against the wall clock; the plot runs
    # on walked time, so both have to be found by where they land in the
    # sample sequence rather than by when they happened.
    stamps = [s["ts"] for s in samples]
    spans: list[list[int]] = []
    for zone in zones:
        start = min(bisect.bisect_left(stamps, zone["start_ts"]), total - 1)
        end = max(start, bisect.bisect_right(stamps, zone["end_ts"]) - 1)
        span = [start // per, min(end, total - 1) // per]
        if spans and span[0] <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], span[1])
        else:
            spans.append(span)
    out["dead_zones"] = spans
    out["pauses"] = sorted({i // per for i in range(1, total)
                            if stamps[i] - stamps[i - 1] > PAUSE_GAP_S})
    # Which columns the rig was actually sending uplink in, so silence it chose
    # is never drawn as silence the garage caused.
    out["uplink_sending"] = _bucket(
        [1.0 if s.get("uplink_loaded") == LOAD_SENDING else None for s in samples],
        per)
    out["no_stream"] = _no_stream_spans(out, list(series), out["columns"])
    return out


def _no_stream_spans(timeline: dict, measured: Sequence[str],
                     columns: int) -> list[list[int]]:
    """Stretches where no direction reported anything at all.

    That is the severe failure, not a hole in the data: iperf3's control
    channel is TCP, so a link bad enough takes the test down with it and stays
    silent until it recovers. It is worth drawing, because a stream can
    collapse without the walk crossing the dead-zone thresholds.

    Silence the rig chose is not that. Uplink runs in bursts, so a column with
    no burst in it has nothing to report and never counts — otherwise a walk
    with downlink switched off would come back shaded grey almost end to end.

    Stretches touching either end are dropped. The test takes a few seconds to
    come up at the start of a walk and is stopped at the end of one, and
    neither is the garage's fault.
    """
    sending = (timeline.get("uplink_sending") or {}).get("seconds") or []
    tried = any(sending)
    down = [all(timeline[name]["seconds"][c] == 0 for name in measured)
            and (not tried or bool(sending[c]))
            for c in range(columns)]
    spans: list[list[int]] = []
    for column, missing in enumerate(down):
        if not missing:
            continue
        if spans and spans[-1][1] == column - 1:
            spans[-1][1] = column
        else:
            spans.append([column, column])
    return [s for s in spans if s[0] > 0 and s[1] < columns - 1]


def build_report(storage: Any, run: dict,
                 load_config: dict | None = None) -> dict[str, Any]:
    """A finished run's result: how much of the walk was usable, and where not.

    Read on a laptop after the walk, not on the phone during it.
    """
    samples = list(storage.iter_samples(run["id"]))
    zones = storage.list_dead_zones(run["id"])
    total = len(samples)

    status_counts: dict[str, int] = {}
    for sample in samples:
        key = sample["status"] or "unknown"
        status_counts[key] = status_counts.get(key, 0) + 1

    stored = json.loads(run["summary_json"]) if run.get("summary_json") else {}
    for zone in zones:
        zone["start_local"] = iso_local(zone["start_ts"])
        zone["start_utc"] = iso_utc(zone["start_ts"])

    return {
        "run": run,
        "summary": stored,
        "sample_count": total,
        "walked_s": stored.get("walked_s", round(total * 1.0, 1)),
        "runnable_pct": run.get("runnable_pct"),
        "elapsed_s": round((run.get("ended_at") or time.time()) - run["started_at"], 1),
        "status_counts": status_counts,
        "status_pct": {k: round(100.0 * v / total, 1)
                       for k, v in status_counts.items()} if total else {},
        "dead_zones": zones,
        "throughput": storage.list_throughput(run["id"]),
        "signal": signal_stats(samples),
        "quality": quality_stats(samples),
        "under_load": load_stats(samples, load_config),
        "timeline": throughput_timeline(samples, zones, load_config),
        "radio_timeline": radio_timeline(samples, zones),
        "radio_score": radio_summary(samples),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

STYLE = """
:root {
  --ink: #16181d; --muted: #646b7a; --line: #e3e6ec; --bg: #fbfcfd;
  --card: #ffffff; --good: #1f7a4d; --bad: #b3261e; --warn: #8a5a00;
  --accent: #1b4d8f;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
}
.wrap { max-width: 940px; margin: 0 auto; padding: 32px 20px 64px; }
header { border-bottom: 1px solid var(--line); padding-bottom: 18px; }
h1 { font-size: 27px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 14px; margin: 0; }
h2 { font-size: 17px; margin: 34px 0 12px; }
.tabs { display: flex; gap: 4px; margin: 20px 0 0; border-bottom: 1px solid var(--line); }
.tab {
  appearance: none; border: 0; background: none; cursor: pointer;
  font: inherit; font-weight: 600; color: var(--muted);
  padding: 10px 16px; border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab[aria-selected="true"] { color: var(--accent); border-bottom-color: var(--accent); }
.headline { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin: 28px 0 6px; }
.pct { font-size: 58px; font-weight: 650; letter-spacing: -0.03em; line-height: 1; }
.pct.good { color: var(--good); } .pct.bad { color: var(--bad); }
.pct.mid { color: var(--warn); }
.caption { color: var(--muted); max-width: 46ch; }
.stats { display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 0; }
.stat {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 16px; min-width: 132px; flex: 1 1 132px;
}
.stat dt { color: var(--muted); font-size: 12px; text-transform: uppercase;
           letter-spacing: 0.04em; margin: 0 0 3px; }
.stat dd { margin: 0; font-size: 21px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; background: var(--card);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); }
th { font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em;
     color: var(--muted); font-weight: 600; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
a { color: var(--accent); }
.note { background: #fff8e8; border: 1px solid #f0dfb4; color: var(--warn);
        border-radius: 10px; padding: 12px 16px; margin: 18px 0; }
.empty { color: var(--muted); background: var(--card); border: 1px solid var(--line);
         border-radius: 10px; padding: 26px; text-align: center; }
video { width: 100%; border-radius: 10px; background: #000; }
.btn {
  appearance: none; font: inherit; font-weight: 600; cursor: pointer;
  background: var(--card); color: var(--accent);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 18px;
}
.btn:hover { border-color: var(--accent); }
.chartbox { background: var(--card); border: 1px solid var(--line);
            border-radius: 10px; padding: 10px 12px 4px; margin: 6px 0 4px;
            overflow-x: auto; }
/* Below this the axis labels stop being readable, so the box scrolls rather
   than shrinking the plot into decoration. */
.chart { display: block; width: 100%; min-width: 620px; height: auto; }
.mono { font-variant-numeric: tabular-nums; }
footer { color: var(--muted); font-size: 13px; margin-top: 48px;
         border-top: 1px solid var(--line); padding-top: 14px; }
[hidden] { display: none !important; }
@media print {
  .tabs, .tab { display: none; }
  [role="tabpanel"] { display: block !important; }
  body { background: #fff; }
}
"""

SCRIPT = """
(function () {
  // Scoped to the tab bar. A button elsewhere on the page that merely looks
  // like a tab is not one, and picking it up here threw on every report —
  // aborting show() and everything after it in this script.
  var tabs = document.querySelectorAll('.tabs [data-tab]');
  function show(name) {
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      var panel = document.getElementById('panel-' + t.dataset.tab);
      if (panel) panel.hidden = !on;
    });
    if (history.replaceState) history.replaceState(null, '', '#' + name);
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { show(t.dataset.tab); });
  });
  show(location.hash === '#detail' ? 'detail' : 'result');

  // The full recording is uploaded after this page was written, so the only
  // way to know whether it is there is to ask for it. A plain same-origin
  // HEAD: no library, nothing fetched from anywhere else, and a failure just
  // leaves the request button showing.
  var offer = document.getElementById('videoOffer');
  var player = document.getElementById('videoPlayer');
  if (offer && player && window.fetch) {
    fetch('video/full.mp4', { method: 'HEAD' }).then(function (response) {
      if (!response.ok) return;
      offer.hidden = true;
      player.hidden = false;
    }).catch(function () {});
  }
})();
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _stat(label: str, value: str) -> str:
    return f'<div class="stat"><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>'


def _radio_row(label: str, spread: dict | None, unit: str) -> str:
    """Radio rows read weakest-to-strongest, not best-to-worst.

    Every number here is negative or small-positive and *higher is better*:
    -100 dBm is a stronger signal than -104. Labelling the minimum "best", the
    way latency tables do, states the exact opposite of the truth to anyone
    skimming, so these get their own header and their own order.
    """
    if not spread:
        return (f'<tr><td>{_e(label)}</td>'
                f'<td colspan="3" class="muted">not recorded</td></tr>')
    return (f"<tr><td>{_e(label)}</td>"
            f'<td class="num">{spread["min"]}{unit}</td>'
            f'<td class="num">{spread["median"]}{unit}</td>'
            f'<td class="num">{spread["max"]}{unit}</td></tr>')


def _spread_row(label: str, spread: dict | None, unit: str) -> str:
    if not spread:
        return (f'<tr><td>{_e(label)}</td>'
                f'<td colspan="4" class="muted">not recorded</td></tr>')
    return (f"<tr><td>{_e(label)}</td>"
            f'<td class="num">{spread["min"]}{unit}</td>'
            f'<td class="num">{spread["median"]}{unit}</td>'
            f'<td class="num">{spread["p95"]}{unit}</td>'
            f'<td class="num">{spread["max"]}{unit}</td></tr>')


def _grade(pct: float | None) -> str:
    if pct is None:
        return "mid"
    return "good" if pct >= 95 else "bad" if pct < 80 else "mid"


def _dead_zone_rows(zones: Sequence[dict], pre_roll: float, post_roll: float) -> str:
    rows = []
    for zone in zones:
        clip = zone.get("clip_path")
        if clip:
            name = str(clip).rsplit("/", 1)[-1]
            link = (f'<a href="{CLIP_DIR}/{_e(name)}">watch '
                    f'{int(pre_roll)}s before &rarr; {int(post_roll)}s after</a>')
        else:
            link = f'<span class="muted">{_e(zone.get("clip_error") or "no clip")}</span>'
        loss = zone.get("worst_loss_pct")
        rtt = zone.get("worst_rtt_ms")
        rows.append(
            f'<tr><td class="num">{_e(zone.get("idx") or zone.get("index"))}</td>'
            f'<td class="mono">{_e(clock_time(zone.get("start_ts")))}</td>'
            f'<td class="num">{_e(duration(zone.get("duration_s")))}</td>'
            f'<td class="num">{"—" if loss is None else f"{loss:.0f}%"}</td>'
            f'<td class="num">{"—" if rtt is None else f"{rtt:.0f} ms"}</td>'
            f"<td>{link}</td></tr>")
    return "".join(rows)


def render_html(report: dict, *, deadzone_config: dict | None = None,
                full_video: bool = False, version: str = "") -> str:
    """Render the report as one self-contained page.

    ``full_video`` says whether the whole walk has been uploaded beside this
    file. It usually has not: the clips are a few tens of megabytes and the
    full recording is a few hundred, and the rig sends them over the same
    cellular link it is there to measure. So the second tab offers to fetch it
    rather than assuming somebody wanted to pay for it.
    """
    config = deadzone_config or {}
    pre_roll = float(config.get("pre_roll_s", 10))
    post_roll = float(config.get("post_roll_s", 5))

    run = report["run"]
    summary = report.get("summary") or {}
    signal = report.get("signal") or {}
    quality = report.get("quality") or {}
    zones = report.get("dead_zones") or []
    pct = report.get("runnable_pct")

    label = run.get("label") or "Unnamed run"
    started = run.get("started_at")
    when = time.strftime("%A %d %B %Y, %H:%M", time.localtime(started)) if started else ""

    # -- headline ------------------------------------------------------------
    if pct is None:
        headline = ('<div class="headline"><div class="pct mid">—</div>'
                    '<p class="caption">No samples were recorded for this run.</p></div>')
    else:
        excluded = summary.get("load_excluded_s") or 0
        judged = (
            f' Measured over the {duration(summary.get("judged_s"))} of the walk '
            "when the rig was not loading the uplink itself: the load test "
            "shares the modem's buffer with ping, so those seconds describe the "
            "test rather than the garage. They are spread evenly across the "
            "walk, so this is a fair sample of it."
            if excluded else "")
        headline = (
            f'<div class="headline"><div class="pct {_grade(pct)}">{pct:g}%</div>'
            '<p class="caption">of the time walked had a link the robot could '
            'use. This is a share of <strong>time</strong>, not of floor area — '
            'there is no positioning underground, so it holds only if the walk '
            'was at a steady pace and paused whenever standing still.'
            f'{judged}</p></div>')

    warnings = []
    if config.get("provisional", False) or summary.get("thresholds_provisional"):
        warnings.append(
            "The thresholds that decide what counts as a dead zone "
            f"({config.get('loss_pct_at_least', 80):g}% packet loss or "
            f"{config.get('or_rtt_ms_at_least', 1500):g} ms latency, sustained for "
            f"{config.get('min_duration_s', 5):g}s) are provisional. They were "
            "chosen before any real survey and have not yet been checked against "
            "a garage anyone knows. Treat the count as indicative.")
    up = (report.get("under_load") or {}).get("uplink") or {}
    if (up.get("saturated_pct") or 0) >= 25 and up.get("offered_mbps"):
        warnings.append(
            f"The uplink could not carry the rate it was offered for "
            f"{up['saturated_pct']:g}% of the seconds it was measured "
            f"({up['saturated_seconds']}s of {up['seconds_measured']}s; offered "
            f"{up['offered_mbps']:g} Mbit/s, median delivered "
            f"{up.get('delivered_median_mbps')} Mbit/s). A link that is full "
            "loses and delays packets because it is full, so the uplink loss "
            "and jitter below are a floor rather than a measurement of this "
            "garage. Lower udp_load.uplink_bitrate towards what the robot "
            "really sends and walk it again if you need the real numbers.")
    coverage = (report.get("under_load") or {}).get("uplink_coverage_pct")
    expected = (report.get("under_load") or {}).get("uplink_expected_seconds") or 0
    if coverage is not None and coverage < 50 and expected >= 10:
        warnings.append(
            f"The uplink test covered only {up.get('seconds_measured', 0)}s of "
            f"the roughly {expected}s it should have, which is "
            f"{coverage:g}% of what the burst schedule delivers on a walk this "
            "long. It was failing rather than resting, so the uplink figures "
            "below describe a fraction of the walk. Check the event log for "
            "what the test said.")
    if quality.get("unsynced_clock_samples"):
        warnings.append(
            f"The clock was not synchronised for "
            f"{quality['unsynced_clock_samples']} of the samples. Video "
            "timestamps for those may not line up with the readings.")
    if quality.get("undervoltage_samples"):
        warnings.append(
            f"The Pi reported undervoltage during "
            f"{quality['undervoltage_samples']} samples. Readings are still "
            "valid, but check the power splice before the next walk.")
    notes = "".join(f'<div class="note">{_e(w)}</div>' for w in warnings)

    # -- tab one: the answer -------------------------------------------------
    stats = "".join([
        _stat("Walked", duration(report.get("walked_s"))),
        _stat("Judged", duration(summary.get("judged_s")))
        if summary.get("load_excluded_s") else "",
        _stat("Dead zones", str(summary.get("dead_zone_count", len(zones)))),
        _stat("Time dead", duration(summary.get("dead_s"))),
        _stat("Longest", duration(summary.get("longest_dead_zone_s"))),
    ])

    if zones:
        zone_table = (
            '<table><thead><tr><th class="num">#</th><th>Started</th>'
            '<th class="num">Lasted</th><th class="num">Worst loss</th>'
            '<th class="num">Worst latency</th><th>Footage</th></tr></thead>'
            f"<tbody>{_dead_zone_rows(zones, pre_roll, post_roll)}</tbody></table>"
            '<p class="sub">Times are the rig\'s local clock, the same one burned '
            'into the top of every video frame.</p>')
    else:
        zone_table = ('<div class="empty">No dead zones. The link stayed usable '
                      'for the whole walk.</div>')

    # -- tab two: the evidence ----------------------------------------------
    #
    # This page is written when the walk ends, and the full recording is
    # uploaded later — on request, and only when the rig is next idle. So the
    # page cannot know at the time it is built whether the video is there. It
    # carries both answers and asks for the file on load; without script, or
    # opened from a USB stick, it falls back to the offer, which is what it
    # showed before this existed.
    player = (f'<video controls preload="metadata"{"" if full_video else " "}'
              f'src="{FULL_VIDEO}"></video>'
              f'<p class="sub"><a href="{FULL_VIDEO}">Download the full '
              'recording</a></p>')
    offer = (
        '<div class="empty"><p>The full walk recording is still on the rig.</p>'
        '<p class="sub">Clips of each dead zone were uploaded automatically; '
        'the whole recording is several hundred megabytes and goes over the '
        'same cellular link the rig is there to measure, so it is fetched '
        'only when asked for.</p>'
        '<form method="post" action="request-video">'
        '<button class="btn" type="submit">Request the full video</button>'
        "</form>"
        '<p class="sub">The rig uploads it the next time it is powered on and '
        'not walking. It can take several minutes over a cellular link; this '
        'page shows the video as soon as it has arrived.</p></div>')
    if full_video:
        video_block = f'<div id="videoPlayer">{player}</div>'
    else:
        video_block = (f'<div id="videoPlayer" hidden>{player}</div>'
                       f'<div id="videoOffer">{offer}</div>')

    latency = "".join([
        _spread_row("Round trip", quality.get("rtt_ms"), " ms"),
        _spread_row("Jitter", quality.get("jitter_ms"), " ms"),
        _spread_row("DNS lookup", quality.get("dns_ms"), " ms"),
    ])
    load = report.get("under_load") or {}
    plot = chart.throughput_svg(report.get("timeline") or {})
    if plot:
        # The plot is the shape of the walk; the tables under it are the
        # numbers. Read together they answer "where did it fail" and "how
        # badly" without either having to carry both jobs.
        chart_block = (
            f'<div class="chartbox">{plot}</div>'
            '<p class="sub">Delivered rate against walked time, so a pause '
            'takes up no room on the axis &mdash; the dotted verticals are '
            'where the walk was paused. The dashed horizontal lines are the '
            'rates the rig <em>sent</em> at, which is the ceiling here: this '
            'is not a speed test, it is whether a teleop-sized stream got '
            'through. Columns shaded red are dead zones. Grey columns are '
            'seconds when nothing arrived at all &mdash; the link failed '
            'badly enough to take the test itself down, which is worse '
            'than a low reading, not missing data.</p>')
    else:
        chart_block = ""
    if load.get("seconds_measured") or plot:
        sections = []
        for name, heading, blurb in (
            ("uplink", "Uplink &mdash; the robot's video going out",
             "Usually the half that decides whether a spot is workable: cellular "
             "uplink is the weaker direction, and this is the heavy stream."),
            ("downlink", "Downlink &mdash; the operator's commands coming in",
             "The real command stream is tiny, and this is deliberately tested "
             "far above it, so a clean result here means the commands would "
             "get through with a great deal to spare. The dashed line on the "
             "chart is the rate actually sent."),
        ):
            half = load.get(name) or {}
            if not half.get("seconds_measured"):
                # Never silently. Uplink is the half that decides whether a
                # spot is workable, and an absent section reads as "fine".
                sections.append(
                    f"<h3>{heading}</h3><p class=\"sub\">{blurb}</p>"
                    '<div class="empty"><p>Not measured on this run.</p>'
                    f'<p class="sub">{_e(half.get("absent_reason") or "")}</p>'
                    "</div>")
                continue
            # A link that could not carry what it was offered was *full*, and
            # a full link loses packets because it is full. Saying so next to
            # the loss figure is the difference between a finding and an
            # artefact of the test's own making.
            short = half.get("saturated_seconds")
            if short and half.get("offered_mbps"):
                saturated = (
                    f'<p class="sub"><strong>{half["saturated_pct"]:g}% of these '
                    f"seconds ({short}s) delivered less than "
                    f'{half["below"]:g}&times; the '
                    f'{half["offered_mbps"]:g}&nbsp;Mbit/s offered</strong> '
                    f'(median delivered {half.get("delivered_median_mbps")}'
                    "&nbsp;Mbit/s). For those the link was full, so the loss "
                    "and jitter below are a floor, not a measurement &mdash; "
                    "and because a full buffer stops getting worse, a link "
                    "that was slightly short looks the same here as one that "
                    "was hopelessly short.</p>")
            else:
                saturated = ""
            sections.append(
                f"<h3>{heading}</h3><p class=\"sub\">{blurb}</p>"
                '<dl class="stats">' + "".join([
                    _stat("Seconds measured", str(half["seconds_measured"])),
                    _stat("Clean seconds", str(half.get("clean_seconds", 0))),
                    _stat("Mean loss", f"{half.get('mean_loss_pct')}%"
                          if half.get("mean_loss_pct") is not None else "—"),
                    _stat("No stream", f"{half.get('seconds_without_stream', 0)}s"),
                    _stat("Could not carry", f"{short}s") if short else "",
                ]) + "</dl>" + saturated +
                '<table><thead><tr><th>Measurement</th><th class="num">Best</th>'
                '<th class="num">Median</th><th class="num">95th</th>'
                '<th class="num">Worst</th></tr></thead><tbody>'
                + _spread_row("Jitter", half.get("jitter_ms"), " ms")
                + _spread_row("Packet loss", half.get("loss_pct"), "%")
                + "</tbody></table>")
        block_s = load.get("uplink_block_s") or 0
        idle_s = load.get("uplink_idle_s") or 0
        duty = (
            f" Uplink is sent in {block_s:g}-second bursts with {idle_s:g} "
            "seconds of silence between them, so the modem's buffer drains and "
            "ping spends most of the walk measuring the garage rather than "
            "queueing behind this test &mdash; so &ldquo;no stream&rdquo; for "
            "uplink counts only bursts that reached nobody, never the quiet "
            "between them. Downlink runs continuously; it does not compete "
            "for the uplink."
            if block_s and idle_s else "")
        load_block = (
            "<h2>Under a teleop-sized load</h2>"
            '<p class="sub">Measured with UDP streams at the bitrates a '
            "teleoperation session uses, rather than on an idle link. Seconds "
            "with <em>no stream at all</em> are the severe case, not missing "
            "data: the link failed badly enough that the test itself could not "
            f"stay up.{duty}</p>" + chart_block + "".join(sections))
    elif load.get("enabled"):
        # An absent section reads as "nothing to report", which is the
        # opposite of the truth: the test was switched on and produced
        # nothing. Somebody noticed this from the outside, having concluded
        # the section only appears when there is packet loss.
        load_block = (
            "<h2>Under a teleop-sized load</h2>"
            '<div class="empty"><p>The load test was switched on for this run '
            "and produced no readings at all, in either direction.</p>"
            f'<p class="sub">Uplink: {_e((load.get("uplink") or {}).get("absent_reason") or "")}</p>'
            f'<p class="sub">Downlink: {_e((load.get("downlink") or {}).get("absent_reason") or "")}</p>'
            '<p class="sub">This says nothing about the garage. Check the '
            "event log for what the test reported.</p></div>")
    else:
        load_block = ""

    # -- the combined score, and the half of it that is the problem ----------
    score = report.get("radio_score") or {}
    radio_plot = chart.radio_svg(report.get("radio_timeline") or {})
    if radio_plot and score.get("readings"):
        limiting = score.get("mostly_limited_by")
        if limiting == radio.STRENGTH:
            verdict = ("Mostly <strong>strength</strong>-limited: not enough of "
                       "the cell's signal reaches here. That is the kind a "
                       "better antenna, a different mounting position or a "
                       "repeater can move.")
        elif limiting == radio.QUALITY:
            verdict = ("Mostly <strong>quality</strong>-limited: the signal "
                       "arrives but too much of what arrives is noise and other "
                       "transmitters. No antenna fixes this one — it is "
                       "interference or a busy cell.")
        else:
            verdict = ""
        radio_block = (
            f'<div class="chartbox">{radio_plot}</div>'
            '<p class="sub"><strong>Height is how much signal arrives; colour '
            'is how much of it is usable.</strong> Height is RSRP in dBm, the '
            'number the modem itself reports &mdash; further down the chart '
            'means further from the cell, or more concrete in the way. Colour '
            'and thickness are SINR: how much of what arrives is the signal '
            'rather than noise and other transmitters.</p>'
            '<p class="sub"><strong>A line that stays high but turns red is the '
            'case worth finding.</strong> Plenty of signal, almost none of it '
            'usable &mdash; "full bars, nothing works". No antenna fixes that '
            'one; it is interference or a busy cell. A line that simply sinks '
            'is the opposite problem, a coverage hole, and that one a better '
            'antenna or a repeater can move.</p>'
            '<p class="sub">RSRQ and RSSI are in the table below rather than on '
            'the chart: they restate the relationship between those two rather '
            'than adding a third independent fact.</p>'
            + (f'<p class="sub">{verdict}</p>' if verdict else "")
            + '<dl class="stats">' + "".join([
                _stat("Median score", f"{score['median']:g} — {score['band']}"),
                _stat("Worst", f"{score['worst']:g}"),
                _stat("Limited by", (limiting or "—").title()),
                _stat("At its worst",
                      (score.get("worst_limited_by") or "—").title()),
            ]) + "</dl>"
            '<p class="sub">The score behind those figures is the <em>worse</em> '
            'of the two, never the average: a link fails from either end, and '
            'an average would let a strong signal hide a filthy one. The bands '
            '&mdash; and the dB ranges in the key above &mdash; are the '
            'conventional ones for LTE. They have not been checked against what '
            'this robot actually needs, so read the shape of the line and where '
            'it changes colour rather than the number on its own.</p>')
    else:
        radio_block = ""

    # Named for what it is, not "radio": the module of that name is used just
    # above, and a local of the same name makes it unbound for the whole
    # function.
    radio_rows = "".join([
        _radio_row("RSRP (signal strength)", signal.get("rsrp"), " dBm"),
        _radio_row("SINR (signal quality)", signal.get("sinr"), " dB"),
        _radio_row("RSRQ (signal quality)", signal.get("rsrq"), " dB"),
        _radio_row("RSSI (total received power)", signal.get("rssi"), " dBm"),
    ])

    if signal.get("readings"):
        radio_stats = "".join([
            _stat("Band" if len(signal.get("bands") or []) == 1 else "Bands",
                  ", ".join("B" + b for b in signal.get("bands") or []) or "—"),
            _stat("Cells seen", str(signal.get("distinct_cells", 0))),
            _stat("Handovers", str(signal.get("handovers", 0))),
            _stat("Readings", str(signal.get("readings", 0))),
        ])
        radio_note = (
            '<p class="sub">A handover is the modem changing cell mid-walk. '
            'Dead seconds that land at the same spot on every pass, with a '
            'different cell either side, are a handover rather than a coverage '
            'hole — and no amount of antenna work will fix those.</p>')
    else:
        radio_stats = ""
        radio_note = ('<p class="sub">No modem readings were collected on this '
                      'run. Set <code>router.client</code> on the rig to record '
                      'them.</p>')

    judged_count = quality.get("judged_count")
    unloaded_note = (
        '<p class="sub">These figures cover the '
        f'{duration(judged_count)} of the walk when the rig was not loading '
        "the uplink itself &mdash; the link a robot would find on arriving. "
        "What the link did with a teleop stream on it is the next section; "
        "the two are different questions, and averaging them gives a median "
        "round trip nobody ever experienced.</p>"
        if judged_count is not None and judged_count < quality.get("sample_count", 0)
        else "")
    quality_stats_row = "".join([
        _stat("Samples", str(quality.get("sample_count", 0))),
        _stat("Unloaded", str(judged_count)) if unloaded_note else "",
        _stat("Replies", str(quality.get("replies", 0))),
        _stat("Mean loss", f"{quality.get('mean_loss_pct')}%"
              if quality.get("mean_loss_pct") is not None else "—"),
        _stat("Lossy seconds", str(quality.get("seconds_with_any_loss", 0))),
    ])

    throughput = report.get("throughput") or []
    if throughput:
        rows = "".join(
            f'<tr><td class="mono">{_e(clock_time(t.get("ts")))}</td>'
            f'<td class="num">{_e(t.get("down_mbps"))}</td>'
            f'<td class="num">{_e(t.get("up_mbps"))}</td>'
            f'<td>{_e(t.get("error") or "")}</td></tr>' for t in throughput)
        throughput_block = (
            '<h2>Throughput</h2><table><thead><tr><th>Time</th>'
            '<th class="num">Down (Mbps)</th><th class="num">Up (Mbps)</th>'
            f"<th>Error</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        throughput_block = ""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(label)} — coverage survey</title>
<style>{STYLE}</style>
</head><body><div class="wrap">

<header>
  <h1>{_e(label)}</h1>
  <p class="sub">Cellular coverage survey · {_e(when)} · {_e(duration(report.get('elapsed_s')))} on site</p>
</header>

<div class="tabs" role="tablist">
  <button class="tab" data-tab="result" role="tab" aria-selected="true">Result</button>
  <button class="tab" data-tab="detail" role="tab" aria-selected="false">Detail &amp; video</button>
</div>

<section id="panel-result" role="tabpanel">
  {headline}
  {notes}
  <dl class="stats">{stats}</dl>
  <h2>Where the link failed</h2>
  {zone_table}
</section>

<section id="panel-detail" role="tabpanel" hidden>
  <h2>The whole walk</h2>
  {video_block}

  <h2>Latency and loss</h2>
  {unloaded_note}
  <dl class="stats">{quality_stats_row}</dl>
  <table><thead><tr><th>Measurement</th><th class="num">Best</th>
  <th class="num">Median</th><th class="num">95th</th><th class="num">Worst</th>
  </tr></thead><tbody>{latency}</tbody></table>

  {load_block}

  <h2>Radio</h2>
  {radio_block}
  <dl class="stats">{radio_stats}</dl>
  <table><thead><tr><th>Measurement</th><th class="num">Weakest</th>
  <th class="num">Median</th><th class="num">Strongest</th>
  </tr></thead><tbody>{radio_rows}</tbody></table>
  <p class="sub">Higher is better for all four: &minus;100 dBm is a stronger
  signal than &minus;104.</p>
  {radio_note}
  {throughput_block}
</section>

<footer>
  Run <span class="mono">{_e(run.get('id'))}</span> ·
  started {_e(iso_local(started))} ·
  measured through the wired uplink, not the rig's own Wi-Fi{
    f" · rig {_e(version)}" if version else ""}
</footer>

</div><script>{SCRIPT}</script></body></html>
"""
