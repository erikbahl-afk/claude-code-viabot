import time

from viabot_survey.runner import classify, slugify


THRESHOLDS = {"degraded_loss_pct": 5, "degraded_rtt_ms": 200,
              "bad_loss_pct": 20, "bad_rtt_ms": 500, "dead_after_s": 5}


def test_clean_link_is_good():
    assert classify(0.0, 48.0, THRESHOLDS) == "good"


def test_latency_alone_degrades():
    assert classify(0.0, 250.0, THRESHOLDS) == "degraded"
    assert classify(0.0, 600.0, THRESHOLDS) == "bad"


def test_loss_alone_degrades():
    assert classify(10.0, 50.0, THRESHOLDS) == "degraded"
    assert classify(30.0, 50.0, THRESHOLDS) == "bad"


def test_total_loss_becomes_dead_only_after_the_configured_streak():
    """A one-second blip while walking past a pillar is not a dead zone."""
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=1.0) == "bad"
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=4.0) == "bad"
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=5.0) == "dead"


def test_no_data_is_unknown_not_good():
    assert classify(None, None, THRESHOLDS) == "unknown"


def test_slugify_makes_safe_run_ids():
    assert slugify("Sunset Garage L2") == "sunset-garage-l2"
    assert slugify("  Ramp / Level 3!! ") == "ramp-level-3"
    assert slugify("") == ""
    assert len(slugify("x" * 100)) == 32


def test_sample_collection_records_signal_and_video_pointers(runner, storage, monkeypatch):
    monkeypatch.setattr("viabot_survey.runner.sysinfo.clock_synced", lambda: True)
    runner.ping.enabled = True
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 55.0, "loss_pct": 0.0, "jitter_ms": 2.0})
    runner.router.enabled = True
    monkeypatch.setattr(runner.router, "sample_fields",
                        lambda: {"rsrp": -92.0, "rsrq": None, "sinr": 9.0, "rssi": None,
                                 "band": "n41", "cell_id": "ABC", "tech": "5G"})
    monkeypatch.setattr(runner.camera, "locate", lambda ts: ("20260911-140000.mkv", 12.5))

    run = runner.start_run(label="test")
    runner.collect_sample()
    rows = list(storage.iter_samples(run["id"]))
    runner.stop_run()

    assert len(rows) == 1
    assert rows[0]["status"] == "good"
    assert rows[0]["rsrp"] == -92.0
    assert rows[0]["band"] == "n41"
    assert rows[0]["video_file"] == "20260911-140000.mkv"
    assert rows[0]["video_offset_s"] == 12.5
    assert rows[0]["clock_synced"] == 1


def test_dead_streak_accumulates_then_resets(runner, monkeypatch):
    runner.ping.enabled = True
    state = {"loss": 100.0}
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": None, "loss_pct": state["loss"],
                                          "jitter_ms": None})
    for _ in range(6):
        sample = runner.collect_sample()
    assert sample["status"] == "dead"
    state["loss"] = 0.0
    assert runner.collect_sample()["dead_streak_s"] == 0.0


def test_marks_require_a_run(runner):
    try:
        runner.add_mark(category="Ramp")
    except RuntimeError as exc:
        assert "no run" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")


def test_second_run_is_refused_while_one_is_active(runner):
    runner.start_run()
    try:
        runner.start_run()
    except RuntimeError as exc:
        assert "already in progress" in str(exc)
    finally:
        runner.stop_run()


def test_startup_closes_a_run_left_open_by_a_power_cut(config, storage):
    """A run still open in the database means the Pi lost power mid-walk.
    Reopening it would leave a gap in the timeline that reads like a dead zone."""
    from viabot_survey.runner import SurveyRunner

    storage.create_run("orphan", started_at=time.time() - 600)
    survey = SurveyRunner(config, storage)
    try:
        survey.start()
        assert storage.get_run("orphan")["ended_at"] is not None
        assert storage.active_run() is None
    finally:
        survey.shutdown()
