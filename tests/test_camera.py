import calendar
import os
import time
from pathlib import Path

from viabot_survey.workers.camera import (CameraWorker, format_utc_offset,
                                          segment_start_epoch, utc_offset_s)


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


def test_overlay_command_burns_in_a_pts_derived_clock(tmp_path, monkeypatch):
    monkeypatch.setattr("viabot_survey.workers.camera.find_font",
                        lambda: "/usr/share/fonts/x.ttf")
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


def test_capture_rate_is_left_to_the_driver_by_default():
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


def test_capture_rate_is_pinned_when_explicitly_configured():
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
