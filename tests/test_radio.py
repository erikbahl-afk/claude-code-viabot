"""The combined radio score.

The whole value of this number is that it cannot be gamed by one good reading.
Most of these tests exist to keep it that way.
"""

from __future__ import annotations

from viabot_survey import chart, radio, report

BASE = 1_757_620_000.0


def test_a_strong_but_dirty_signal_scores_badly():
    """The case the score exists for. Plenty of signal arriving, almost none of
    it usable — "full bars, nothing works". An average of the two would call
    this fair and send nobody to look at it."""
    scored = radio.rate({"rsrp": -82.0, "sinr": -2.0})
    assert scored["band"] == "poor"
    assert scored["limited_by"] == radio.QUALITY
    # Explicitly not the mean, which would have been comfortably "fair".
    assert scored["score"] < (scored["strength"] + scored["quality"]) / 2


def test_a_weak_but_clean_signal_is_limited_by_strength():
    """Different problem, different remedy: this one an antenna can help."""
    scored = radio.rate({"rsrp": -114.0, "sinr": 18.0})
    assert scored["band"] == "poor"
    assert scored["limited_by"] == radio.STRENGTH


def test_both_good_scores_well():
    scored = radio.rate({"rsrp": -78.0, "sinr": 22.0})
    assert scored["band"] == "excellent"


def test_rsrq_stands_in_when_the_modem_reports_no_sinr():
    """Some firmwares do not report SINR. A report should not go blank over
    which firmware a router happens to be running."""
    assert radio.rate({"rsrp": -95.0, "sinr": None, "rsrq": -12.0})["score"]
    assert radio.rate({"rsrp": -95.0, "sinr": None})["limited_by"] == radio.STRENGTH


def test_nothing_measured_is_not_scored_as_zero():
    """A modem that said nothing is not a garage with no signal."""
    scored = radio.rate({"rsrp": None, "sinr": None})
    assert scored["score"] is None
    assert scored["band"] == "unknown"


def test_the_scale_is_clamped_at_both_ends():
    assert radio.strength_score(-40.0) == 100.0
    assert radio.strength_score(-140.0) == 0.0
    assert radio.quality_score(40.0) == 100.0
    assert radio.quality_score(-30.0) == 0.0


# ---- along the walk --------------------------------------------------------

def _walk(seconds: int = 120):
    rows = []
    for i in range(seconds):
        sinr = 14.0 - (20.0 if 60 <= i < 90 else 0.0)
        rows.append({"ts": BASE + i, "rsrp": -90.0, "sinr": sinr,
                     "rtt_ms": 40.0, "loss_pct": 0.0})
    return rows


def test_the_two_charts_share_an_axis_so_they_can_be_read_together():
    """The question worth asking of these plots is whether the link failed at
    the same moment the radio did. That only works if the columns line up."""
    rows = _walk()
    throughput = report.throughput_timeline(
        [dict(r, udp_up_mbps=3.0, udp_down_mbps=5.0) for r in rows], (), {})
    radio_line = report.radio_timeline(rows, ())
    assert radio_line["columns"] == throughput["columns"]
    assert radio_line["seconds_per_column"] == throughput["seconds_per_column"]


def test_the_summary_separates_the_usual_problem_from_the_worst_one():
    """A garage mostly limited by strength but whose worst moment was
    interference needs both facts; either alone points somewhere wrong."""
    rows = _walk()
    summary = report.radio_summary(rows)
    assert summary["readings"] == 120
    assert summary["mostly_limited_by"] == radio.STRENGTH
    assert summary["worst_limited_by"] == radio.QUALITY


def test_a_run_with_no_modem_readings_draws_nothing():
    rows = [{"ts": BASE + i, "rtt_ms": 40.0} for i in range(30)]
    assert report.radio_timeline(rows, ()) == {}
    assert chart.radio_svg({}) == ""


def test_the_plot_carries_two_facts_without_a_second_axis():
    """Height is strength in dBm; quality rides on colour and thickness. Two
    y-scales on one plot would invent a relationship that is not in the data."""
    svg = chart.radio_svg(report.radio_timeline(_walk(), ()))
    assert "Signal strength (dBm)" in svg
    assert svg.count("dBm)") == 1                      # one axis, named once
    # The dBm window is fixed rather than fitted, so two garages can be
    # compared against each other rather than each against itself.
    assert "-110" in svg and "-70" in svg


def test_quality_never_travels_as_colour_alone():
    """Red against green is the commonest colour-vision failure, and this
    report goes to customers. Each band is named with its dB range in the key,
    and the line thickens as quality falls, so the chart survives greyscale,
    photocopying and colourblindness."""
    svg = chart.radio_svg(report.radio_timeline(_walk(), ()))
    for band, description in radio.QUALITY_LEGEND:
        assert band.title() in svg
        assert description.split("&")[0].strip()[:12] in svg
    widths = {radio.QUALITY_STYLE[b][1] for b, _ in radio.QUALITY_LEGEND}
    assert len(widths) == len(radio.QUALITY_LEGEND)    # thickness differs too


def test_a_gap_in_the_readings_is_not_drawn_across():
    rows = _walk(60)
    for row in rows[20:30]:
        row["rsrp"] = None
    timeline = report.radio_timeline(rows, ())
    svg = chart.radio_svg(timeline)
    # Fewer segments than columns, because the hole is left as a hole.
    assert svg.count("<line x1=") < timeline["columns"] + 12


def test_the_report_renders_the_score_block():
    """Regression: a local variable named `radio` in render_html shadowed the
    module, and this whole branch raised UnboundLocalError. Every existing test
    passed, because none of them populated radio_score."""
    rows = _walk()
    built = {
        "run": {"id": "r", "label": "Level 3", "started_at": BASE},
        "summary": {}, "runnable_pct": 100.0, "walked_s": 120.0,
        "elapsed_s": 120.0, "dead_zones": [], "throughput": [],
        "signal": report.signal_stats(rows), "quality": report.quality_stats(rows),
        "under_load": report.load_stats(rows),
        "radio_timeline": report.radio_timeline(rows, ()),
        "radio_score": report.radio_summary(rows),
    }
    page = report.render_html(built)
    assert "turns red is the" in page
    assert "<em>worse</em> of the two" in page
    assert "Median score" in page
    # And it says the bands are conventions rather than requirements.
    assert "conventional ones for LTE" in page
