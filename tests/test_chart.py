"""The throughput plot.

Its whole risk is reading like a speed test when it is not one, and drawing a
confident line across the exact moment the link died. Both of those are tested
here.
"""

from __future__ import annotations

from viabot_survey import chart, report
from viabot_survey.workers.udpload import parse_bitrate_mbps

BASE = 1_757_620_000.0

LOAD = {"uplink_bitrate": "1.5M", "downlink_bitrate": "300k"}


def samples(values, *, start: float = BASE, step: float = 1.0):
    """One sample a second, ``values`` being (uplink, downlink) or None."""
    out = []
    ts = start
    for pair in values:
        up, down = pair if pair else (None, None)
        out.append({"ts": ts, "udp_up_mbps": up, "udp_down_mbps": down})
        ts += step
    return out


# ---- bitrate spelling ------------------------------------------------------

def test_the_configured_bitrate_is_read_in_iperf3s_own_spelling():
    assert parse_bitrate_mbps("1.5M") == 1.5
    assert parse_bitrate_mbps("300k") == 0.3
    assert parse_bitrate_mbps("750000") == 0.75
    assert parse_bitrate_mbps(None) is None
    assert parse_bitrate_mbps("as fast as it goes") is None


# ---- what gets plotted -----------------------------------------------------

def test_a_run_with_no_load_readings_has_no_plot():
    """Every walk taken before udp_load was switched on is this case, and the
    report has to render for those exactly as it did before."""
    timeline = report.throughput_timeline(samples([None] * 30), (), LOAD)
    assert timeline == {}
    assert chart.throughput_svg({}) == ""


def test_the_offered_rate_is_drawn_because_it_is_the_ceiling():
    """The rig sends a fixed teleop-sized stream, so the top of this plot is
    what was offered, not what the link could have carried. Without the
    reference line a flat trace reads as a speed limit the link imposed."""
    timeline = report.throughput_timeline(samples([(1.5, 0.3)] * 60), (), LOAD)
    assert timeline["uplink"]["offered_mbps"] == 1.5
    assert timeline["downlink"]["offered_mbps"] == 0.3
    svg = chart.throughput_svg(timeline)
    assert "sent 1.5 Mbit/s" in svg and "sent 0.3 Mbit/s" in svg


def test_a_gap_is_not_drawn_through():
    """A stretch with no readings is the link having failed hard enough to
    take the test down. Joining the two ends would draw a straight line across
    the worst moment of the walk."""
    values = [(1.5, 0.3)] * 20 + [None] * 10 + [(1.5, 0.3)] * 20
    timeline = report.throughput_timeline(samples(values), (), LOAD)
    svg = chart.throughput_svg(timeline)
    # Two polylines per direction: one either side of the hole.
    assert svg.count("<polyline") == 4


def test_a_dead_stretch_is_shaded_but_not_the_start_or_the_end():
    """The stream takes a moment to come up and is stopped when the walk ends.
    Neither is the garage's fault, so neither gets marked as a failure."""
    values = [None] * 5 + [(1.5, 0.3)] * 20 + [None] * 10 + [(1.5, 0.3)] * 20 \
        + [None] * 5
    timeline = report.throughput_timeline(samples(values), (), LOAD)
    assert timeline["no_stream"] == [[25, 34]]


def test_the_axis_is_walked_time_so_a_pause_takes_up_no_room():
    """Pause leaves no samples at all. On a wall clock a ten-minute coffee
    break would draw as a ten-minute outage, which is the opposite of what
    Pause means."""
    values = [(1.5, 0.3)] * 30
    rows = samples(values)
    for row in rows[15:]:            # the walk resumed ten minutes later
        row["ts"] += 600
    timeline = report.throughput_timeline(rows, (), LOAD)
    assert timeline["walked_s"] == 30
    assert timeline["pauses"] == [15]
    assert "stroke-dasharray=\"2 3\"" in chart.throughput_svg(timeline)


def test_a_long_walk_is_bucketed_rather_than_drawn_second_by_second():
    """An hour underground is 3,600 readings a direction, which is more than a
    900-pixel plot can show and more than belongs in a page uploaded over the
    link it describes."""
    timeline = report.throughput_timeline(
        samples([(1.5, 0.3)] * 3600), (), LOAD, columns=600)
    assert timeline["columns"] <= 600
    assert timeline["seconds_per_column"] == 6
    assert len(timeline["uplink"]["mid"]) == timeline["columns"]


def test_a_bucket_keeps_its_worst_second_rather_than_averaging_it_away():
    """A mean over six seconds hides the second the video would have dropped,
    which is the only second anyone is looking for."""
    values = [(1.5, 0.3)] * 5 + [(0.1, 0.02)] + [(1.5, 0.3)] * 6
    timeline = report.throughput_timeline(samples(values), (), LOAD, columns=2)
    assert min(v for v in timeline["uplink"]["lo"] if v is not None) == 0.1


def test_dead_zones_land_where_they_happened_in_walked_time():
    """Zones are recorded against the wall clock; the plot runs on walked
    time, so a zone after a pause has to be found by sample rather than by
    when it happened."""
    rows = samples([(1.5, 0.3)] * 40)
    for row in rows[20:]:
        row["ts"] += 300
    zone = {"start_ts": rows[30]["ts"], "end_ts": rows[33]["ts"]}
    timeline = report.throughput_timeline(rows, [zone], LOAD, columns=40)
    assert timeline["dead_zones"] == [[30, 33]]


def test_the_plot_carries_no_script_and_fetches_nothing():
    """The report opens from a USB stick years from now, and inside a report a
    reader may forward to a customer."""
    timeline = report.throughput_timeline(samples([(1.5, 0.3)] * 60), (), LOAD)
    svg = chart.throughput_svg(timeline)
    assert "<script" not in svg
    # The SVG namespace is a URL but nothing fetches it; nothing else may be.
    assert svg.count("http") == 1
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg


def test_the_plot_reaches_the_report_page():
    values = [(1.5, 0.3)] * 60
    rows = samples(values)
    for row in rows:
        row.update(udp_up_loss_pct=0.0, udp_down_loss_pct=0.0)
    built = {
        "run": {"id": "r", "label": "Level 2", "started_at": BASE},
        "summary": {}, "runnable_pct": 100.0, "walked_s": 60.0, "elapsed_s": 60.0,
        "dead_zones": [], "throughput": [],
        "signal": report.signal_stats(rows),
        "quality": report.quality_stats(rows),
        "under_load": report.load_stats(rows),
        "timeline": report.throughput_timeline(rows, (), LOAD),
    }
    page = report.render_html(built)
    assert 'class="chart"' in page
    assert "not a speed test" in page
