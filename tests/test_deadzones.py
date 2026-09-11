from pathlib import Path

from viabot_survey import deadzones
from viabot_survey.deadzones import DeadZone, find_dead_zones, plan_clip, summarise

CONFIG = {
    "loss_pct_at_least": 80,
    "or_rtt_ms_at_least": 1500,
    "min_duration_s": 5,
    "merge_gap_s": 10,
    "pre_roll_s": 10,
    "post_roll_s": 5,
}
BASE = 1_700_000_000.0


def walk(pattern, base=BASE, step=1.0):
    """Build samples from a compact description of a walk.

    'g' good, 'd' dead (total loss), 's' slow (usable-looking latency that is
    not usable in practice), '_' a gap in the timeline.
    """
    samples = []
    ts = base
    for char in pattern:
        if char == "_":
            ts += 60          # a pause: no sample written at all
            continue
        if char == "g":
            samples.append({"ts": ts, "rtt_ms": 45.0, "loss_pct": 0.0})
        elif char == "d":
            samples.append({"ts": ts, "rtt_ms": None, "loss_pct": 100.0})
        elif char == "s":
            samples.append({"ts": ts, "rtt_ms": 2400.0, "loss_pct": 0.0})
        ts += step
    return samples


# ---- detection -------------------------------------------------------------

def test_a_clean_walk_has_no_dead_zones():
    assert find_dead_zones(walk("g" * 60), CONFIG) == []


def test_a_sustained_outage_is_one_dead_zone():
    zones = find_dead_zones(walk("g" * 10 + "d" * 20 + "g" * 10), CONFIG)
    assert len(zones) == 1
    assert zones[0].duration_s == 20.0
    assert zones[0].worst_loss_pct == 100.0
    assert zones[0].sample_count == 20


def test_a_brief_blip_is_not_a_dead_zone():
    """Walking behind a pillar drops a packet or two. That is not a dead zone,
    and reporting it as one would bury the real ones."""
    assert find_dead_zones(walk("g" * 10 + "ddd" + "g" * 10), CONFIG) == []


def test_a_zone_exactly_at_the_minimum_counts():
    zones = find_dead_zones(walk("g" * 5 + "d" * 5 + "g" * 5), CONFIG)
    assert len(zones) == 1
    assert zones[0].duration_s == 5.0


def test_flickering_stretches_merge_into_one_zone():
    """A bad ramp flickers in and out. Without merging it lands in the report
    as six entries with six nearly identical clips."""
    zones = find_dead_zones(walk("g" * 5 + "ddddd" + "ggg" + "ddddd" + "g" * 5), CONFIG)
    assert len(zones) == 1
    assert zones[0].duration_s == 13.0     # spans the good seconds between them


def test_zones_far_apart_stay_separate():
    zones = find_dead_zones(
        walk("g" * 5 + "d" * 6 + "g" * 30 + "d" * 6 + "g" * 5), CONFIG)
    assert len(zones) == 2
    assert [z.index for z in zones] == [1, 2]


def test_crippling_latency_counts_even_with_no_packet_loss():
    """A link that answers every packet two seconds late is no more usable to
    a robot than one that answers none."""
    zones = find_dead_zones(walk("g" * 5 + "s" * 10 + "g" * 5), CONFIG)
    assert len(zones) == 1
    assert zones[0].worst_rtt_ms == 2400.0
    assert zones[0].worst_loss_pct == 0.0


def test_a_pause_never_joins_two_dead_zones():
    """The gap left by a pause is not walked ground, so a zone must not be
    stitched across it even when the samples either side are both bad."""
    samples = walk("g" * 5 + "d" * 6) + walk("d" * 6 + "g" * 5, base=BASE + 400)
    zones = find_dead_zones(samples, CONFIG)
    assert len(zones) == 2


def test_a_zone_running_to_the_end_of_the_walk_is_still_reported():
    zones = find_dead_zones(walk("g" * 10 + "d" * 10), CONFIG)
    assert len(zones) == 1
    assert zones[0].duration_s == 10.0


def test_samples_with_no_reading_do_not_invent_a_zone():
    samples = [{"ts": BASE + i, "rtt_ms": None, "loss_pct": None} for i in range(30)]
    assert find_dead_zones(samples, CONFIG) == []


# ---- summary ---------------------------------------------------------------

def test_summary_reports_the_runnable_percentage():
    samples = walk("g" * 80 + "d" * 20)
    zones = find_dead_zones(samples, CONFIG)
    summary = summarise(samples, zones)
    assert summary["runnable_pct"] == 80.0
    assert summary["walked_s"] == 100.0
    assert summary["dead_s"] == 20.0
    assert summary["dead_zone_count"] == 1
    assert summary["longest_dead_zone_s"] == 20.0


def test_summary_of_a_perfect_walk():
    samples = walk("g" * 50)
    summary = summarise(samples, [])
    assert summary["runnable_pct"] == 100.0
    assert summary["dead_zone_count"] == 0
    assert summary["longest_dead_zone_s"] == 0.0


def test_summary_says_the_percentage_is_time_based():
    """There is no indoor positioning, so the number must not be mistaken for
    a fraction of the garage's area."""
    assert "time" in summarise(walk("g" * 10), [])["measured_in"]


def test_summary_of_an_empty_run_does_not_divide_by_zero():
    assert summarise([], [])["runnable_pct"] is None


# ---- clip geometry ---------------------------------------------------------

def test_clip_window_follows_the_zone_length():
    """Clips are not a fixed 30 seconds: a ninety-second outage should produce
    a clip you can watch end to end."""
    zone = DeadZone(index=1, start_ts=BASE + 100, end_ts=BASE + 189)
    start, end = zone.clip_window(pre_roll_s=10, post_roll_s=5)
    assert start == BASE + 90
    assert end == BASE + 195
    assert round(end - start, 1) == zone.duration_s + 15


def test_plan_clip_picks_the_one_segment_that_covers_the_window(tmp_path):
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        (tmp_path / name).write_bytes(b"x")
    segments = [("a.mkv", BASE), ("b.mkv", BASE + 300), ("c.mkv", BASE + 600)]
    spans = plan_clip(segments, tmp_path, BASE + 350, BASE + 400, 300)
    assert [s.path.name for s in spans] == ["b.mkv"]
    assert spans[0].offset_s == 50


def test_plan_clip_spans_a_segment_boundary(tmp_path):
    """A dead zone near the end of a segment needs footage from two files."""
    for name in ("a.mkv", "b.mkv"):
        (tmp_path / name).write_bytes(b"x")
    segments = [("a.mkv", BASE), ("b.mkv", BASE + 300)]
    spans = plan_clip(segments, tmp_path, BASE + 290, BASE + 320, 300)
    assert [s.path.name for s in spans] == ["a.mkv", "b.mkv"]
    assert spans[0].offset_s == 290
    assert spans[1].offset_s == 0      # the second file is taken from its start


def test_plan_clip_ignores_segments_that_are_not_on_disk(tmp_path):
    segments = [("missing.mkv", BASE)]
    assert plan_clip(segments, tmp_path, BASE, BASE + 10, 300) == []


def test_extract_clip_reports_when_there_is_no_footage(tmp_path):
    zone = DeadZone(index=1, start_ts=BASE, end_ts=BASE + 10)
    result = deadzones.extract_clip(zone, [], tmp_path, tmp_path / "clips", CONFIG)
    assert result.clip_path is None
    assert result.clip_error


def test_zone_serialises_with_both_clocks():
    zone = DeadZone(index=2, start_ts=BASE, end_ts=BASE + 9)
    data = zone.as_dict()
    assert data["index"] == 2
    assert data["duration_s"] == 10.0
    assert data["start_utc"] == "2023-11-14T22:13:20Z"
    assert data["start_local"].startswith("2023-11-")
