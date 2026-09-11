import time
from pathlib import Path

from viabot_survey.workers.camera import CameraWorker, segment_start_epoch


def test_segment_name_round_trips_to_wall_clock():
    epoch = segment_start_epoch("20260911-143005.mkv")
    assert epoch is not None
    assert time.strftime("%Y%m%d-%H%M%S", time.localtime(epoch)) == "20260911-143005"


def test_non_segment_names_are_ignored():
    for name in ("notes.txt", "20260911.mkv", "", "2026-09-11-143005.mkv"):
        assert segment_start_epoch(name) is None


def _worker_with_segments(tmp_path, names):
    worker = CameraWorker(output_dir=tmp_path, enabled=False)
    for name in names:
        (tmp_path / name).write_bytes(b"")
    return worker


def test_locate_maps_a_timestamp_into_the_right_segment(tmp_path):
    """This is the whole point of the rig: a bad sample has to resolve to a
    place in the video."""
    worker = _worker_with_segments(
        tmp_path, ["20260911-140000.mkv", "20260911-140500.mkv", "20260911-141000.mkv"])
    target = segment_start_epoch("20260911-140500.mkv") + 123
    name, offset = worker.locate(target)
    assert name == "20260911-140500.mkv"
    assert offset == 123.0


def test_locate_before_the_first_segment_returns_nothing(tmp_path):
    worker = _worker_with_segments(tmp_path, ["20260911-140000.mkv"])
    name, offset = worker.locate(segment_start_epoch("20260911-140000.mkv") - 60)
    assert name is None and offset is None


def test_locate_ignores_stray_files(tmp_path):
    worker = _worker_with_segments(tmp_path, ["20260911-140000.mkv", "README.txt"])
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
    assert "pts" in joined and "localtime" in joined and "1700000000" in joined
    assert "-strftime 1" in joined
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
