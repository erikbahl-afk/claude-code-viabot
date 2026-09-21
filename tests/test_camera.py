import calendar
import os
import shutil
import time
from pathlib import Path

from unittest import mock

import pytest

from viabot_survey.workers.camera import (CameraWorker, find_font,
                                          format_utc_offset,
                                          segment_start_epoch, utc_offset_s)


@pytest.fixture
def accepts_filters():
    """Pretend ffmpeg accepts whatever filtergraph it is handed."""
    with mock.patch("viabot_survey.workers.camera.find_font",
                    return_value="/usr/share/fonts/x.ttf"), \
         mock.patch("viabot_survey.workers.camera.filter_works",
                    return_value=(True, "")):
        yield


@pytest.fixture
def rejects_filters():
    with mock.patch("viabot_survey.workers.camera.find_font",
                    return_value="/usr/share/fonts/x.ttf"), \
         mock.patch("viabot_survey.workers.camera.filter_works",
                    return_value=(False, "No option name near 'localtime:...'")):
        yield


def test_segment_name_round_trips_through_utc():
    epoch = segment_start_epoch("20260911T143005Z.mkv")
    assert epoch is not None
    assert time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch)) == "20260911T143005Z"
    assert epoch == calendar.timegm((2026, 9, 11, 14, 30, 5, 0, 0, 0))


def test_segment_parsing_does_not_depend_on_the_host_timezone(monkeypatch):
    """The whole reason for UTC filenames: the Pi's timezone was never set
    during imaging, so setting it later must not shift old recordings."""
    readings = []
    for zone in ("UTC", "America/Los_Angeles", "Europe/Berlin", "Asia/Kolkata"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        readings.append(segment_start_epoch("20260911T143005Z.mkv"))
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()
    assert len(set(readings)) == 1


def test_non_segment_names_are_ignored():
    for name in ("notes.txt", "20260911.mkv", "", "2026-09-11-143005.mkv",
                 "20260911-143005.mkv", "manifest.json"):
        assert segment_start_epoch(name) is None


def test_utc_offset_formatting():
    assert format_utc_offset(0) == "+0000"
    assert format_utc_offset(-7 * 3600) == "-0700"
    assert format_utc_offset(5 * 3600 + 1800) == "+0530"


def _worker_with_segments(tmp_path, names):
    worker = CameraWorker(output_dir=tmp_path, enabled=False)
    for name in names:
        (tmp_path / name).write_bytes(b"")
    return worker


def test_locate_maps_a_timestamp_into_the_right_segment(tmp_path):
    """This is the whole point of the rig: a bad sample has to resolve to a
    place in the video."""
    worker = _worker_with_segments(
        tmp_path, ["20260911T140000Z.mkv", "20260911T140500Z.mkv",
                   "20260911T141000Z.mkv"])
    target = segment_start_epoch("20260911T140500Z.mkv") + 123
    name, offset = worker.locate(target)
    assert name == "20260911T140500Z.mkv"
    assert offset == 123.0


def test_locate_before_the_first_segment_returns_nothing(tmp_path):
    worker = _worker_with_segments(tmp_path, ["20260911T140000Z.mkv"])
    name, offset = worker.locate(segment_start_epoch("20260911T140000Z.mkv") - 60)
    assert name is None and offset is None


def test_locate_ignores_stray_files(tmp_path):
    worker = _worker_with_segments(
        tmp_path, ["20260911T140000Z.mkv", "README.txt", "manifest.json"])
    assert len(worker.segments()) == 1


def test_overlay_command_burns_in_a_pts_derived_clock(tmp_path, accepts_filters):
    worker = CameraWorker(mode="overlay", width=1280, height=720, fps=10, enabled=False)
    cmd = worker.build_command(tmp_path, 1_700_000_000)
    joined = " ".join(cmd)
    assert "-input_format mjpeg" in joined
    assert "1280x720" in joined
    assert "libx264" in joined
    # Derived from frame PTS, not render time, so the label cannot drift when
    # encoding falls behind capture.
    assert "pts" in joined and "localtime" in joined
    # ffmpeg runs under TZ=UTC, so the burned-in clock's base is shifted by the
    # local offset to render local time, and the offset is printed alongside.
    offset = utc_offset_s(1_700_000_000)
    assert str(1_700_000_000 + offset) in joined
    assert format_utc_offset(offset) in joined
    assert "-strftime 1" in joined
    assert "%Y%m%dT%H%M%SZ.mkv" in joined
    # Matroska survives an abrupt power cut; a truncated MP4 does not play.
    assert "-segment_format matroska" in joined


def test_copy_mode_skips_the_encoder(tmp_path):
    worker = CameraWorker(mode="copy", enabled=False)
    cmd = worker.build_command(tmp_path, 1_700_000_000)
    joined = " ".join(cmd)
    assert "-c:v copy" in joined
    assert "libx264" not in joined
    assert "drawtext" not in joined


def test_overlay_falls_back_to_copy_without_a_font(tmp_path, monkeypatch):
    monkeypatch.setattr("viabot_survey.workers.camera.find_font", lambda: None)
    events = []
    worker = CameraWorker(mode="overlay", enabled=False,
                          on_event=lambda level, msg: events.append((level, msg)))
    cmd = worker.build_command(tmp_path, 1_700_000_000)
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert any("font" in message for _, message in events)
    assert worker.effective_mode == "copy"


def test_overlay_colons_carry_two_backslashes():
    """Regression, and the reason for the odd-looking escaping.

    A filtergraph is unescaped twice on the way in — once by the graph parser,
    once by the filter's option parser — so one backslash is consumed before
    drawtext sees it and the colon then reads as an option separator. ffmpeg
    rejected the whole graph, exited before writing a frame, and the worker
    restarted it in a loop while the dashboard flickered a camera alert. A
    whole survey walk recorded nothing.

    Single-quoting the value is not the fix either: drawtext then mis-parses a
    %{...} holding a strftime format and reports "Both text and text file
    provided".
    """
    with mock.patch("viabot_survey.workers.camera.find_font", return_value="/f.ttf"), \
         mock.patch("viabot_survey.workers.camera.filter_works", return_value=(True, "")):
        graph = CameraWorker(mode="overlay", fps=10, enabled=False).overlay_filter(
            1_757_620_000)

    assert ":text=%{pts" in graph          # not quoted
    assert "text='" not in graph
    assert graph.count(r"\\:") == 3        # pts, localtime, and the offset
    assert r"%{pts\\:localtime\\:1757620000\\:%F %T}" in graph
    # The label states its zone, so footage is not ambiguous about which one.
    assert graph.split(":fontsize")[0].endswith(format_utc_offset(utc_offset_s(1_757_620_000)))


def test_a_rejected_overlay_still_records(tmp_path, rejects_filters):
    """Losing every frame of a survey because a text overlay would not parse is
    a far worse outcome than losing the clock."""
    events = []
    worker = CameraWorker(mode="overlay", enabled=False,
                          on_event=lambda level, msg: events.append((level, msg)))
    assert worker.overlay_filter(1_700_000_000) is None
    cmd = worker.build_command(tmp_path, 1_700_000_000)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert worker.effective_mode == "copy"
    assert any(level == "error" and "without it" in msg for level, msg in events)


def test_the_filter_is_only_validated_once(accepts_filters):
    """Validation spawns ffmpeg; the worker restarts on every failure, so doing
    this per restart would be its own problem."""
    worker = CameraWorker(mode="overlay", enabled=False)
    with mock.patch("viabot_survey.workers.camera.filter_works",
                    return_value=(True, "")) as check:
        worker.overlay_filter(1_700_000_000)
        worker.overlay_filter(1_700_000_100)
        worker.overlay_filter(1_700_000_200)
    assert check.call_count == 1


def test_snapshot_reports_the_effective_mode(rejects_filters):
    worker = CameraWorker(mode="overlay", enabled=False)
    worker.overlay_filter(1_700_000_000)
    snapshot = worker.snapshot()
    assert snapshot["configured_mode"] == "overlay"
    assert snapshot["mode"] == "copy"
    assert snapshot["overlay"] is False


def test_snapshot_survives_a_missing_output_dir():
    snapshot = CameraWorker(enabled=False).snapshot()
    assert snapshot["recording"] is False
    assert snapshot["segments"] == 0


def test_manifest_records_which_clock_is_which(tmp_path):
    """Six months later, on another machine, nothing about a directory of
    .mkv files says that the names are UTC but the picture shows local time."""
    import json

    worker = CameraWorker(enabled=False)
    worker.set_output_dir(tmp_path)
    worker._started_at = 1_700_000_000.0
    worker._write_manifest()

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["segment_filenames_are"] == "UTC"
    assert manifest["burned_in_clock_is"] == "local time"
    assert manifest["capture_started_epoch"] == 1_700_000_000.0
    assert manifest["capture_started_utc"] == "2023-11-14T22:13:20Z"
    assert "local_utc_offset" in manifest


def test_recording_environment_forces_utc(monkeypatch):
    """Segment names come from ffmpeg's strftime, which honours TZ."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    environment = {**os.environ, "TZ": "UTC"}
    assert environment["TZ"] == "UTC"


def test_capture_rate_is_left_to_the_driver_by_default(accepts_filters):
    """The rig's camera advertises 30 fps and only 30 fps. Asking a UVC device
    for a rate it does not offer makes it keep sending its own while ffmpeg
    believes otherwise, which skews every timestamp the project depends on."""
    cmd = CameraWorker(mode="overlay", fps=10, enabled=False).build_command(
        Path("/tmp"), 1_700_000_000)
    assert "-framerate" not in cmd
    joined = " ".join(cmd)
    # Decimation happens in the filter graph instead, before the encoder.
    assert "fps=10," in joined
    assert "-r 10" in joined


def test_capture_rate_is_pinned_when_explicitly_configured(accepts_filters):
    cmd = CameraWorker(mode="overlay", fps=10, capture_fps=30,
                       enabled=False).build_command(Path("/tmp"), 1_700_000_000)
    assert cmd[cmd.index("-framerate") + 1] == "30"


def test_copy_mode_never_asks_the_encoder_to_decimate():
    """Nothing can be dropped without re-encoding, so copy mode records at
    whatever rate the camera sends."""
    joined = " ".join(CameraWorker(mode="copy", fps=10, enabled=False)
                      .build_command(Path("/tmp"), 1_700_000_000))
    assert "-c:v copy" in joined
    assert "fps=" not in joined


# ---- against a real ffmpeg, when there is one ------------------------------
#
# These are the tests that would have caught the filtergraph bug. The container
# Claude develops in usually has no ffmpeg, so they skip there; on the rig, and
# anywhere ffmpeg is installed, they check the real thing rather than a mock.

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@requires_ffmpeg
def test_the_overlay_filtergraph_is_accepted_by_ffmpeg():
    """Regression for a bug that cost a whole survey walk: ffmpeg rejected the
    filtergraph, exited before writing a frame, and the worker restarted it in a
    loop while the dashboard flickered a camera alert."""
    from viabot_survey.workers.camera import filter_works

    if find_font() is None:
        pytest.skip("no font installed to draw with")
    worker = CameraWorker(mode="overlay", fps=10, enabled=False)
    worker._overlay_ok = True          # get the string without the cached check
    ok, error = filter_works(worker.overlay_filter(1_757_620_000))
    assert ok, f"ffmpeg rejected the overlay filter: {error}"


@requires_ffmpeg
def test_the_overlay_draws_the_expected_wall_clock(tmp_path):
    """The label must read the local time of the captured frame. Getting the
    escaping merely *parseable* is not enough — it also has to say the truth."""
    import subprocess

    if find_font() is None:
        pytest.skip("no font installed to draw with")
    worker = CameraWorker(mode="overlay", fps=10, enabled=False)
    worker._overlay_ok = True
    graph = worker.overlay_filter(1_757_620_000)

    frame = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "color=c=black:size=560x110:rate=1",
                    "-vf", graph, "-frames:v", "1", str(frame)],
                   check=True, capture_output=True)
    assert frame.exists() and frame.stat().st_size > 0


@requires_ffmpeg
def test_filter_works_rejects_a_broken_filtergraph():
    from viabot_survey.workers.camera import filter_works

    ok, error = filter_works("drawtext=this_is_not_an_option=1")
    assert ok is False
    assert error


# ---- rotation, for a camera mounted on its side ----------------------------

def test_rotation_turns_the_picture_before_the_clock_is_drawn():
    """Order matters. Rotating after the overlay would turn the burned-in
    clock on its side too, which is the one thing in the picture that has to
    stay readable."""
    worker = CameraWorker(mode="overlay", fps=10, rotate=90, enabled=False)
    worker._overlay_ok = True
    graph = worker.overlay_filter(1_700_000_000)
    assert graph.index("transpose=1") < graph.index("drawtext")


def test_the_transpose_direction_is_not_backwards():
    """ffmpeg counts transpose=1 as clockwise and transpose=2 as
    anticlockwise, which is easy to invert. A camera whose picture comes out
    with the scene's top on the left needs 90 to put it right."""
    def graph_for(degrees):
        worker = CameraWorker(mode="overlay", fps=10, rotate=degrees, enabled=False)
        worker._overlay_ok = True
        return worker.overlay_filter(1_700_000_000)

    assert "transpose=1," in graph_for(90)
    assert "transpose=2," in graph_for(270)
    assert "transpose=1,transpose=1," in graph_for(180)
    assert "transpose" not in graph_for(0)


def test_a_nonsense_rotation_is_ignored_rather_than_breaking_the_recording():
    """A filtergraph ffmpeg will not parse records nothing at all, so a typo in
    the config must not be able to produce one."""
    for bad in (45, 17, -1, 1000):
        assert CameraWorker(rotate=bad, enabled=False).rotate in (0, 90, 180, 270)


def test_copy_mode_says_it_cannot_rotate_instead_of_recording_sideways(tmp_path):
    """Rotating needs a filter and a filter needs a re-encode. Copy mode can do
    neither, and an operator who set `rotate` is entitled to be told that
    rather than finding out from the footage."""
    events = []
    worker = CameraWorker(mode="copy", rotate=90, enabled=False,
                          on_event=lambda level, msg: events.append((level, msg)))
    worker.build_command(tmp_path, 1_700_000_000)
    assert any("copy mode" in message for _, message in events)


def test_copy_mode_says_nothing_when_no_rotation_was_asked_for():
    """A warning that fires on every run stops being a warning."""
    events = []
    worker = CameraWorker(mode="copy", rotate=0, enabled=False,
                          on_event=lambda level, msg: events.append((level, msg)))
    worker.build_command(Path("/tmp"), 1_700_000_000)
    assert not any("rotate" in message for _, message in events)


@requires_ffmpeg
def test_every_rotation_produces_a_filtergraph_ffmpeg_accepts():
    """Same reasoning as the overlay regression above: a graph ffmpeg refuses
    means the rig records nothing at all, and no mock catches that."""
    from viabot_survey.workers.camera import filter_works

    if find_font() is None:
        pytest.skip("no font installed to draw with")
    for degrees in (0, 90, 180, 270):
        worker = CameraWorker(mode="overlay", fps=10, rotate=degrees, enabled=False)
        worker._overlay_ok = True
        ok, error = filter_works(worker.overlay_filter(1_757_620_000))
        assert ok, f"ffmpeg rejected rotate={degrees}: {error}"
