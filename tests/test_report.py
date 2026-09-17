"""The report page. Mostly about not lying to the reader."""

from __future__ import annotations

from viabot_survey import report

BASE = 1_757_620_000.0

SAMPLES = [
    {"rtt_ms": 45.0, "loss_pct": 0.0, "rsrp": -100.0, "sinr": 11.0,
     "band": "12", "cell_id": "1452806", "tech": "LTE", "clock_synced": 1},
    {"rtt_ms": 52.0, "loss_pct": 0.0, "rsrp": -104.0, "sinr": 9.0,
     "band": "12", "cell_id": "1452808", "tech": "LTE", "clock_synced": 1},
    {"rtt_ms": None, "loss_pct": 100.0, "rsrp": -118.0, "sinr": 2.0,
     "band": "12", "cell_id": "1452808", "tech": "LTE", "clock_synced": 0,
     "undervoltage": 1},
]

REPORT = {
    "run": {"id": "20250911-194640-level-2", "label": "Level 2, North Ramp",
            "started_at": BASE, "ended_at": BASE + 314},
    "summary": {"dead_zone_count": 1, "dead_s": 45.0, "longest_dead_zone_s": 45.0},
    "runnable_pct": 85.7, "walked_s": 314.0, "elapsed_s": 314.0,
    "dead_zones": [{"idx": 1, "start_ts": BASE + 126, "duration_s": 45.0,
                    "worst_loss_pct": 100.0, "worst_rtt_ms": None,
                    "clip_path": "/data/clips/r/deadzone-01-x-45s.mp4"}],
    "throughput": [],
    "signal": report.signal_stats(SAMPLES),
    "quality": report.quality_stats(SAMPLES),
}


def render(**kwargs) -> str:
    return report.render_html(REPORT, **kwargs)


# ---- statistics ------------------------------------------------------------

def test_a_change_of_cell_is_counted_as_a_handover():
    """Dead seconds at the same spot on every pass with a different cell either
    side are a handover, not a coverage hole, and no antenna fixes those."""
    assert REPORT["signal"]["handovers"] == 1
    assert REPORT["signal"]["distinct_cells"] == 2


def test_repeating_the_same_cell_is_not_a_handover():
    samples = [{"cell_id": "1"}, {"cell_id": "1"}, {"cell_id": "1"}]
    assert report.signal_stats(samples)["handovers"] == 0


def test_samples_with_no_radio_reading_are_not_counted_as_readings():
    assert report.signal_stats([{"rtt_ms": 1.0}])["readings"] == 0
    assert report.signal_stats([{"rtt_ms": 1.0}])["rsrp"] is None


def test_the_two_faults_that_invalidate_a_walk_are_counted():
    """An unsynchronised clock means the video cannot be lined up with the
    readings, which is the entire reason the camera is there."""
    assert REPORT["quality"]["unsynced_clock_samples"] == 1
    assert REPORT["quality"]["undervoltage_samples"] == 1


def test_durations_read_as_durations():
    assert report.duration(45) == "45s"
    assert report.duration(314) == "5m 14s"
    assert report.duration(3725) == "1h 02m"
    assert report.duration(None) == "—"


# ---- the page --------------------------------------------------------------

def test_the_page_carries_the_run_name_and_the_headline():
    page = render()
    assert "Level 2, North Ramp" in page
    assert "85.7%" in page


def test_every_dead_zone_links_to_its_own_clip():
    page = render()
    assert 'href="clips/deadzone-01-x-45s.mp4"' in page
    assert "10s before" in page and "5s after" in page


def test_radio_numbers_are_never_labelled_best_to_worst():
    """-104 dBm is a weaker signal than -100, so a table that calls the minimum
    'best' states the opposite of the truth to anyone skimming it."""
    page = render()
    radio = page[page.index("<h2>Radio</h2>"):]
    assert "Weakest" in radio and "Strongest" in radio
    assert "Best" not in radio
    # And in that order: weakest first.
    assert radio.index("-118.0 dBm") < radio.index("-100.0 dBm")


def test_provisional_thresholds_say_so_on_the_page():
    """They were invented before any real survey. A customer reading a dead
    zone count deserves to know the bar was set by guesswork."""
    page = render(deadzone_config={"provisional": True})
    assert "provisional" in page


def test_a_run_name_cannot_inject_markup():
    """Operators type the location on a phone, and it lands in a page that may
    be sent to a customer."""
    hostile = dict(REPORT, run=dict(REPORT["run"], label='<script>alert(1)</script>'))
    page = report.render_html(hostile)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_page_pulls_nothing_from_the_internet():
    """It has to open from a laptop with no network, off a USB stick, and in
    five years' time."""
    page = render()
    for marker in ("http://", "https://", "//cdn", "<link"):
        assert marker not in page, marker


def test_the_full_video_is_offered_rather_than_assumed():
    assert "Request the full video" in render()
    assert 'src="video/full.mp4"' in render(full_video=True)


def test_a_clean_walk_does_not_show_an_empty_table():
    page = report.render_html(dict(REPORT, dead_zones=[], runnable_pct=100.0,
                                   summary={"dead_zone_count": 0}))
    assert "No dead zones" in page


# ---- a direction that produced nothing -------------------------------------

def _short_walk(seconds: int = 23) -> list[dict]:
    """A walk too short for one uplink block: downlink streams, uplink cannot."""
    return [{"ts": BASE + i, "rtt_ms": 30.0, "loss_pct": 0.0,
             "udp_down_loss_pct": 0.0, "udp_down_jitter_ms": 1.6,
             "udp_down_mbps": 0.3} for i in range(seconds)]


def test_an_unmeasured_direction_is_never_silently_dropped():
    """Uplink decides whether a spot is workable. A section that simply is not
    there reads as 'nothing to report', which is the opposite of the truth."""
    stats = report.load_stats(_short_walk(), {"uplink_block_s": 30})
    assert stats["uplink"]["seconds_measured"] == 0
    assert "23s" in stats["uplink"]["absent_reason"]
    assert "30s" in stats["uplink"]["absent_reason"]


def test_the_page_says_why_uplink_is_missing():
    built = dict(REPORT, under_load=report.load_stats(
        _short_walk(), {"uplink_block_s": 30}))
    page = report.render_html(built)
    assert "Not measured on this run" in page
    assert "Walk for a few minutes" in page
    # The half that did work is still shown in full.
    assert "Downlink" in page


def test_a_long_walk_with_no_uplink_blames_the_server_not_the_operator():
    """Telling someone to walk for longer when they walked for ten minutes
    sends them to do the wrong thing."""
    stats = report.load_stats(_short_walk(600), {"uplink_block_s": 30})
    reason = stats["uplink"]["absent_reason"]
    assert "Walk for a few minutes" not in reason
    assert "event log" in reason


# ---- the full video, which arrives after this page was written -------------

def test_the_page_carries_both_answers_about_the_video():
    """The report is written when the walk ends; the full recording is
    uploaded later, on request. So the page cannot know at build time whether
    the video is there — it carries the player hidden, the offer visible, and
    asks for the file on load."""
    page = report.render_html(REPORT)
    assert 'id="videoPlayer" hidden' in page
    assert 'id="videoOffer"' in page
    assert "fetch('video/full.mp4', { method: 'HEAD' })" in page


def test_a_report_built_after_the_video_shows_it_outright():
    page = report.render_html(REPORT, full_video=True)
    assert 'id="videoPlayer" hidden' not in page
    assert 'id="videoOffer"' not in page


def test_the_page_still_fetches_nothing_from_anywhere_else():
    """Same-origin HEAD only. The report has to open from a USB stick years
    from now, and there it simply falls back to the request button."""
    page = report.render_html(REPORT)
    assert "http://" not in page.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "https://" not in page


def test_the_request_button_is_not_treated_as_a_tab():
    """It borrowed the tab class for its looks, and the tab script selected on
    that class — so t.dataset.tab was undefined, getElementById returned null,
    and show() threw on every report where the video had not been uploaded.
    The panels happened to be set before the throw, so the page looked fine
    while everything later in the script never ran."""
    page = report.render_html(REPORT)
    assert '<button class="btn" type="submit">' in page
    # Scoped to the tab bar, so nothing outside it can be mistaken for a tab.
    assert "document.querySelectorAll('.tabs [data-tab]')" in page
    # And a tab with no panel can no longer take the script down.
    assert "if (panel) panel.hidden = !on;" in page
