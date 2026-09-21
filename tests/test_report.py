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
    assert "Walk for at least" in page
    # The half that did work is still shown in full.
    assert "Downlink" in page


def test_a_long_walk_with_no_uplink_blames_the_server_not_the_operator():
    """Telling someone to walk for longer when they walked for ten minutes
    sends them to do the wrong thing."""
    stats = report.load_stats(_short_walk(600), {"uplink_block_s": 30})
    reason = stats["uplink"]["absent_reason"]
    assert "Walk for at least" not in reason
    assert "event log" in reason


def test_the_walk_has_to_outlast_the_whole_duty_cycle_not_just_the_burst():
    """The burst is short but the gap after it is not, and a walk that ends in
    the gap still reports nothing. Quoting only the burst length would send
    someone out for ten seconds and leave them with an empty section again."""
    stats = report.load_stats(_short_walk(25),
                              {"uplink_block_s": 10, "uplink_idle_s": 20})
    reason = stats["uplink"]["absent_reason"]
    assert "45s" in reason          # 1.5 whole cycles, not 1.5 bursts
    assert "drain" in reason


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


# ---- saturation: a full link is not a finding about the garage -------------

def _loaded_walk(seconds=60, delivered=1.2):
    """A walk where the uplink delivered less than it was offered."""
    return [{"ts": 1_700_000_000.0 + i, "rtt_ms": 1400.0, "loss_pct": 10.0,
             "udp_up_loss_pct": 22.0, "udp_up_jitter_ms": 40.0,
             "udp_up_mbps": delivered} for i in range(seconds)]


def test_a_link_that_could_not_carry_the_offered_rate_is_counted():
    stats = report.load_stats(_loaded_walk(), {
        "uplink_bitrate": "3M", "uplink_saturated_below": 0.85})
    half = stats["uplink"]
    assert half["offered_mbps"] == 3.0
    assert half["saturated_seconds"] == 60
    assert half["saturated_pct"] == 100.0
    assert half["delivered_median_mbps"] == 1.2


def test_a_link_that_carried_what_it_was_offered_is_not_flagged():
    """Otherwise every clean walk would carry a warning and the warning would
    stop meaning anything."""
    stats = report.load_stats(_loaded_walk(delivered=2.95), {
        "uplink_bitrate": "3M", "uplink_saturated_below": 0.85})
    assert stats["uplink"]["saturated_seconds"] == 0


def test_no_configured_rate_means_no_saturation_claim():
    """Better to say nothing than to compare against a number we invented."""
    stats = report.load_stats(_loaded_walk(), {})
    assert stats["uplink"]["offered_mbps"] is None
    assert "saturated_seconds" not in stats["uplink"]


def test_the_page_warns_that_a_saturated_reading_is_a_floor():
    """The whole point: a full link loses packets because it is full, so its
    loss figure describes the test rather than the garage. Without this the
    report presents the rig's own doing as a property of the place."""
    built = dict(REPORT, under_load=report.load_stats(
        _loaded_walk(), {"uplink_bitrate": "3M"}))
    page = report.render_html(built)
    assert "could not carry the rate it was offered" in page
    assert "floor rather than a measurement" in page
    assert "uplink_bitrate" in page


def test_a_clean_walk_gets_no_saturation_warning():
    built = dict(REPORT, under_load=report.load_stats(
        _loaded_walk(delivered=2.95), {"uplink_bitrate": "3M"}))
    assert "could not carry the rate it was offered" not in report.render_html(built)


def test_the_page_says_the_uplink_is_sent_in_bursts():
    """An operator reading 'constant UDP streams' would reasonably wonder why
    the uplink trace has holes in it every twenty seconds."""
    built = dict(REPORT, under_load=report.load_stats(
        _loaded_walk(), {"uplink_bitrate": "3M", "uplink_block_s": 10,
                         "uplink_idle_s": 20}))
    page = report.render_html(built)
    assert "10-second bursts" in page
    assert "20 seconds of silence" in page


def test_the_headline_says_what_it_was_judged_on_when_seconds_were_excluded():
    built = dict(REPORT, summary={**REPORT.get("summary", {}),
                                  "judged_s": 400.0, "load_excluded_s": 200.0})
    page = report.render_html(built)
    assert "not loading the uplink itself" in page


def test_the_silence_between_bursts_is_not_reported_as_a_failed_stream():
    """Uplink is sent in bursts, so most of a walk is deliberate silence.
    Counting it as 'no stream' would report two thirds of every walk as the
    link having failed badly enough to take the test down with it."""
    samples = []
    for i in range(60):
        sending = (i % 30) < 10
        samples.append({
            "ts": 1_700_000_000.0 + i,
            "uplink_loaded": 2 if sending else 0,
            "udp_up_loss_pct": 1.0 if sending else None,
            "udp_up_jitter_ms": 3.0 if sending else None,
            "udp_up_mbps": 2.9 if sending else None,
        })
    half = report.load_stats(samples, {"uplink_bitrate": "3M"})["uplink"]
    assert half["seconds_measured"] == 20
    assert half["seconds_without_stream"] == 0


def test_a_burst_that_reached_nobody_is_still_reported_as_no_stream():
    """The severe case has to survive the fix: the rig was sending and the far
    end heard nothing, which is worse than a low reading."""
    samples = []
    for i in range(60):
        sending = (i % 30) < 10
        got = sending and i < 30          # the second burst reached nobody
        samples.append({
            "ts": 1_700_000_000.0 + i,
            "uplink_loaded": 2 if sending else 0,
            "udp_up_loss_pct": 1.0 if got else None,
            "udp_up_jitter_ms": 3.0 if got else None,
            "udp_up_mbps": 2.9 if got else None,
        })
    half = report.load_stats(samples, {"uplink_bitrate": "3M"})["uplink"]
    assert half["seconds_without_stream"] == 10


def test_a_run_from_before_the_bursts_still_counts_silence_the_old_way():
    """Nothing marked those samples, and a continuous test really was sending
    for every second of the walk."""
    samples = [{"ts": 1_700_000_000.0 + i,
                "udp_up_loss_pct": 1.0 if i < 30 else None,
                "udp_up_jitter_ms": 3.0 if i < 30 else None,
                "udp_up_mbps": 2.9 if i < 30 else None} for i in range(60)]
    half = report.load_stats(samples, {"uplink_bitrate": "3M"})["uplink"]
    assert half["seconds_without_stream"] == 30


def test_downlink_switched_off_does_not_shade_the_whole_walk_grey():
    """Grey means the link failed badly enough to take the test down with it.
    With only uplink measured, the gaps between bursts would otherwise paint
    two thirds of the plot as the worst thing the report can say."""
    samples = []
    for i in range(120):
        sending = (i % 30) < 10
        samples.append({"ts": 1_700_000_000.0 + i,
                        "uplink_loaded": 2 if sending else 0,
                        "udp_up_mbps": 2.9 if sending else None})
    timeline = report.throughput_timeline(samples, load_config={"uplink_bitrate": "3M"})
    assert "downlink" not in timeline
    assert timeline["no_stream"] == []


def test_a_burst_that_delivered_nothing_is_still_shaded():
    samples = []
    for i in range(120):
        sending = (i % 30) < 10
        samples.append({"ts": 1_700_000_000.0 + i,
                        "uplink_loaded": 2 if sending else 0,
                        # the middle two bursts reached nobody
                        "udp_up_mbps": 2.9 if sending and not 30 <= i < 90 else None})
    timeline = report.throughput_timeline(samples, load_config={"uplink_bitrate": "3M"})
    assert timeline["no_stream"]


def test_latency_and_loss_describe_the_link_a_robot_would_find():
    """The load test's own seconds have a round trip of 1.8 seconds by
    construction. Folding them into the latency table produces a median nobody
    ever experienced, sitting directly above a 95% headline."""
    samples = ([{"ts": 1_700_000_000.0 + i, "rtt_ms": 1800.0, "loss_pct": 60.0,
                 "uplink_loaded": 2} for i in range(10)]
               + [{"ts": 1_700_000_010.0 + i, "rtt_ms": 50.0, "loss_pct": 0.0,
                   "uplink_loaded": 0} for i in range(20)])
    stats = report.quality_stats(samples)
    assert stats["sample_count"] == 30      # the whole walk, as walked
    assert stats["judged_count"] == 20
    assert stats["rtt_ms"]["max"] == 50.0
    assert stats["mean_loss_pct"] == 0.0


def test_the_latency_table_says_which_seconds_it_covers():
    samples = ([{"ts": 1_700_000_000.0 + i, "rtt_ms": 1800.0, "loss_pct": 60.0,
                 "uplink_loaded": 2} for i in range(10)]
               + [{"ts": 1_700_000_010.0 + i, "rtt_ms": 50.0, "loss_pct": 0.0}
                  for i in range(20)])
    page = report.render_html(dict(REPORT, quality=report.quality_stats(samples)))
    assert "not loading the uplink itself" in page


def test_a_run_with_no_load_test_gets_no_extra_caveat():
    """Most of the caveats in this report earn their place. One that fires on
    every run, including runs where it cannot apply, does not."""
    page = report.render_html(dict(REPORT, quality=report.quality_stats(SAMPLES)))
    assert "the link a robot would find on arriving" not in page


# ---- the section must never just disappear ---------------------------------

def _no_load_walk(seconds=300):
    return [{"ts": 1_700_000_000.0 + i, "rtt_ms": 45.0, "loss_pct": 0.0}
            for i in range(seconds)]


def test_a_load_test_that_produced_nothing_says_so_instead_of_vanishing():
    """An absent section reads as 'nothing to report', which is the opposite of
    the truth. Found from the outside by someone who concluded the section only
    appears when there is packet loss, having compared three reports where the
    one with no dead zones was also the one whose load test never ran."""
    built = dict(REPORT, under_load=report.load_stats(
        _no_load_walk(), {"enabled": True, "uplink_bitrate": "3M",
                          "uplink_block_s": 10, "uplink_idle_s": 20}))
    page = report.render_html(built)
    assert "Under a teleop-sized load" in page
    assert "produced no readings at all" in page


def test_a_run_with_the_load_test_switched_off_keeps_the_section_hidden():
    """The opposite failure: a section explaining an absence nobody asked for."""
    built = dict(REPORT, under_load=report.load_stats(
        _no_load_walk(), {"enabled": False}))
    assert "Under a teleop-sized load" not in report.render_html(built)


def test_the_page_says_when_the_bursts_covered_almost_none_of_the_walk():
    """Run aew-test-03 recorded 13 seconds of load in a nine-minute walk and
    the report presented what little it got without comment."""
    samples = []
    for i in range(600):
        sending = i < 10                  # one burst in a ten-minute walk
        samples.append({
            "ts": 1_700_000_000.0 + i, "rtt_ms": 45.0, "loss_pct": 0.0,
            "uplink_loaded": 2 if sending else 0,
            "udp_up_loss_pct": 1.0 if sending else None,
            "udp_up_jitter_ms": 3.0 if sending else None,
            "udp_up_mbps": 2.9 if sending else None,
        })
    stats = report.load_stats(samples, {"enabled": True, "uplink_bitrate": "3M",
                                        "uplink_block_s": 10, "uplink_idle_s": 20})
    assert stats["uplink_expected_seconds"] == 200
    assert stats["uplink_coverage_pct"] == 5.0

    page = report.render_html(dict(REPORT, under_load=stats))
    assert "covered only 10s" in page
    assert "failing rather than resting" in page


def test_a_healthy_duty_cycle_gets_no_coverage_warning():
    """It fires on a broken run or it means nothing."""
    samples = []
    for i in range(600):
        sending = (i % 30) < 10
        samples.append({
            "ts": 1_700_000_000.0 + i, "rtt_ms": 45.0, "loss_pct": 0.0,
            "uplink_loaded": 2 if sending else 0,
            "udp_up_loss_pct": 1.0 if sending else None,
            "udp_up_jitter_ms": 3.0 if sending else None,
            "udp_up_mbps": 2.9 if sending else None,
        })
    stats = report.load_stats(samples, {"enabled": True, "uplink_bitrate": "3M",
                                        "uplink_block_s": 10, "uplink_idle_s": 20})
    assert stats["uplink_coverage_pct"] == 100.0
    assert "failing rather than resting" not in report.render_html(
        dict(REPORT, under_load=stats))
