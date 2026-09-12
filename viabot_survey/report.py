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

import html
import json
import time
from typing import Any, Iterable, Sequence

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
    quietly invalidate a walk: an unsynchronised clock and undervoltage."""
    rtts = _numbers(samples, "rtt_ms")
    losses = _numbers(samples, "loss_pct")
    total = len(samples)

    return {
        "rtt_ms": _spread(rtts),
        "jitter_ms": _spread(_numbers(samples, "jitter_ms")),
        "dns_ms": _spread(_numbers(samples, "dns_ms")),
        "mean_loss_pct": round(sum(losses) / len(losses), 1) if losses else None,
        "seconds_with_any_loss": sum(1 for v in losses if v > 0),
        "replies": len(rtts),
        # A walk taken on an unsynchronised clock cannot be lined up with the
        # video, which is the entire point of recording it.
        "unsynced_clock_samples": sum(
            1 for s in samples if s.get("clock_synced") == 0),
        "undervoltage_samples": sum(
            1 for s in samples if s.get("undervoltage") == 1),
        "sample_count": total,
    }


def load_stats(samples: Sequence[dict]) -> dict[str, Any]:
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
    out: dict[str, Any] = {}
    for name, prefix in (("uplink", "udp_up_"), ("downlink", "udp_down_")):
        readings = [s for s in samples if s.get(prefix + "loss_pct") is not None]
        if not readings:
            out[name] = {"seconds_measured": 0}
            continue
        losses = _numbers(readings, prefix + "loss_pct")
        out[name] = {
            "seconds_measured": len(readings),
            "seconds_without_stream": len(samples) - len(readings),
            "jitter_ms": _spread(_numbers(readings, prefix + "jitter_ms")),
            "loss_pct": _spread(losses),
            "mbps": _spread(_numbers(readings, prefix + "mbps")),
            "mean_loss_pct": round(sum(losses) / len(losses), 1) if losses else None,
            "clean_seconds": sum(1 for v in losses if v == 0),
        }
    out["seconds_measured"] = max(out["uplink"]["seconds_measured"],
                                  out["downlink"]["seconds_measured"])
    return out


def build_report(storage: Any, run: dict) -> dict[str, Any]:
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
        "under_load": load_stats(samples),
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
  var tabs = document.querySelectorAll('.tab');
  function show(name) {
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      document.getElementById('panel-' + t.dataset.tab).hidden = !on;
    });
    if (history.replaceState) history.replaceState(null, '', '#' + name);
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { show(t.dataset.tab); });
  });
  show(location.hash === '#detail' ? 'detail' : 'result');
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
        headline = (
            f'<div class="headline"><div class="pct {_grade(pct)}">{pct:g}%</div>'
            '<p class="caption">of the time walked had a link the robot could '
            'use. This is a share of <strong>time</strong>, not of floor area — '
            'there is no positioning underground, so it holds only if the walk '
            'was at a steady pace and paused whenever standing still.</p></div>')

    warnings = []
    if config.get("provisional", False) or summary.get("thresholds_provisional"):
        warnings.append(
            "The thresholds that decide what counts as a dead zone "
            f"({config.get('loss_pct_at_least', 80):g}% packet loss or "
            f"{config.get('or_rtt_ms_at_least', 1500):g} ms latency, sustained for "
            f"{config.get('min_duration_s', 5):g}s) are provisional. They were "
            "chosen before any real survey and have not yet been checked against "
            "a garage anyone knows. Treat the count as indicative.")
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
    if full_video:
        video_block = (f'<video controls preload="metadata" src="{FULL_VIDEO}"></video>'
                       f'<p class="sub"><a href="{FULL_VIDEO}">Download the full '
                       'recording</a></p>')
    else:
        video_block = (
            '<div class="empty"><p>The full walk recording is still on the rig.</p>'
            '<p class="sub">Clips of each dead zone were uploaded automatically; '
            'the whole recording is several hundred megabytes and goes over the '
            'same cellular link the rig is there to measure, so it is fetched '
            'only when asked for.</p>'
            '<form method="post" action="request-video">'
            '<button class="tab" type="submit" style="border:1px solid var(--line);'
            'border-radius:8px;padding:8px 18px;color:var(--accent)">'
            'Request the full video</button></form>'
            '<p class="sub">The rig uploads it the next time it is powered on and '
            'not walking.</p></div>')

    latency = "".join([
        _spread_row("Round trip", quality.get("rtt_ms"), " ms"),
        _spread_row("Jitter", quality.get("jitter_ms"), " ms"),
        _spread_row("DNS lookup", quality.get("dns_ms"), " ms"),
    ])
    load = report.get("under_load") or {}
    if load.get("seconds_measured"):
        sections = []
        for name, heading, blurb in (
            ("uplink", "Uplink &mdash; the robot's video going out",
             "Usually the half that decides whether a spot is workable: cellular "
             "uplink is the weaker direction, and this is the heavy stream."),
            ("downlink", "Downlink &mdash; the operator's commands coming in",
             "Small, but if it collapses the robot stops taking orders."),
        ):
            half = load.get(name) or {}
            if not half.get("seconds_measured"):
                continue
            sections.append(
                f"<h3>{heading}</h3><p class=\"sub\">{blurb}</p>"
                '<dl class="stats">' + "".join([
                    _stat("Seconds measured", str(half["seconds_measured"])),
                    _stat("Clean seconds", str(half.get("clean_seconds", 0))),
                    _stat("Mean loss", f"{half.get('mean_loss_pct')}%"
                          if half.get("mean_loss_pct") is not None else "—"),
                    _stat("No stream", f"{half.get('seconds_without_stream', 0)}s"),
                ]) + "</dl>"
                '<table><thead><tr><th>Measurement</th><th class="num">Best</th>'
                '<th class="num">Median</th><th class="num">95th</th>'
                '<th class="num">Worst</th></tr></thead><tbody>'
                + _spread_row("Jitter", half.get("jitter_ms"), " ms")
                + _spread_row("Packet loss", half.get("loss_pct"), "%")
                + "</tbody></table>")
        load_block = (
            "<h2>Under a teleop-sized load</h2>"
            '<p class="sub">Measured with constant UDP streams at the bitrates a '
            "teleoperation session uses, rather than on an idle link. Seconds "
            "with <em>no stream at all</em> are the severe case, not missing "
            "data: the link failed badly enough that the test itself could not "
            "stay up.</p>" + "".join(sections))
    else:
        load_block = ""

    radio = "".join([
        _radio_row("RSRP (signal strength)", signal.get("rsrp"), " dBm"),
        _radio_row("SINR (signal quality)", signal.get("sinr"), " dB"),
        _radio_row("RSRQ (signal quality)", signal.get("rsrq"), " dB"),
        _radio_row("RSSI (total received power)", signal.get("rssi"), " dBm"),
    ])

    if signal.get("readings"):
        radio_summary = "".join([
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
        radio_summary = ""
        radio_note = ('<p class="sub">No modem readings were collected on this '
                      'run. Set <code>router.client</code> on the rig to record '
                      'them.</p>')

    quality_stats_row = "".join([
        _stat("Samples", str(quality.get("sample_count", 0))),
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
  <dl class="stats">{quality_stats_row}</dl>
  <table><thead><tr><th>Measurement</th><th class="num">Best</th>
  <th class="num">Median</th><th class="num">95th</th><th class="num">Worst</th>
  </tr></thead><tbody>{latency}</tbody></table>

  {load_block}

  <h2>Radio</h2>
  <dl class="stats">{radio_summary}</dl>
  <table><thead><tr><th>Measurement</th><th class="num">Weakest</th>
  <th class="num">Median</th><th class="num">Strongest</th>
  </tr></thead><tbody>{radio}</tbody></table>
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
